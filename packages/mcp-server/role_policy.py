"""Local role-and-model policy loader and spawn gate for Hermes Swarm.

Keeps personal engineering preferences (IDE frontier models, forbidden Hermes
delegation roles, execution worker model defaults) strictly local on the user's
machine (e.g. `~/.config/hermes-swarm/role_policy.json`) instead of embedding
them inside the public plugin repository.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
from typing import Any

DEFAULT_IDE_ONLY_ROLES: tuple[str, ...] = (
    "researcher",
    "research",
    "architect",
    "design",
    "planner",
    "plan",
    "consult",
    "consultant",
    "verifier",
    "verify",
    "reviewer",
    "auditor",
)

DEFAULT_HERMES_EXECUTION_ROLES: tuple[str, ...] = (
    "worker",
    "implementer",
    "monitor",
    "narrator",
)

DEFAULT_IDE_GUIDANCE = (
    "IDE frontier subagents (`invoke_subagent`, `TypeName='self'`) own all thinking and "
    "judgment work: research (history, docs, web), architecture/design, planning, specialist "
    "consultations, and adversarial verification. Reserve Hermes `swarm_worker` processes "
    "strictly for execution (`worker`/`implementer`, `monitor`, `narrator`): implementing "
    "pre-designed specs, running builds, SSH/profiling/benchmark loops, and git/CI mechanics."
)

DEFAULT_ROLE_POLICY: dict[str, Any] = {
    "version": 1,
    "enforcement": "warn",
    "ide_only_roles": list(DEFAULT_IDE_ONLY_ROLES),
    "hermes_execution_roles": list(DEFAULT_HERMES_EXECUTION_ROLES),
    "ide_subagent_preferences": {
        "tool": "invoke_subagent",
        "type_name": "self",
        "guidance": DEFAULT_IDE_GUIDANCE,
    },
    "hermes_worker_defaults": {
        "model": None,
        "provider": None,
        "base_url": None,
        "reasoning": None,
        "profile": None,
    },
    "hub_and_spoke": True,
}

_NAME_SEP_RE = re.compile(r"[-_]+")


def default_role_policy_path() -> Path:
    """Return the primary user-level role policy path (`~/.config/hermes-swarm/role_policy.json`)."""
    return Path.home() / ".config" / "hermes-swarm" / "role_policy.json"


def candidate_role_policy_paths(workspace: str | None = None) -> list[Path]:
    """Return ordered local policy candidate paths without reading network/repo secrets."""
    candidates: list[Path] = []
    env_val = str(os.environ.get("HERMES_SWARM_ROLE_POLICY") or "").strip()
    if env_val and env_val.lower() not in ("default", "none", "off"):
        candidates.append(Path(env_val).expanduser())
    if workspace and str(workspace).strip():
        ws = Path(str(workspace).strip()).expanduser()
        candidates.append(ws / ".hermes-swarm" / "role_policy.json")
    candidates.append(default_role_policy_path())
    candidates.append(Path.home() / ".hermes-swarm.json")
    return candidates


def _deep_merge_policy(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, val in override.items():
        if key in ("ide_only_roles", "hermes_execution_roles") and isinstance(val, list):
            out[key] = [str(item).strip().lower() for item in val if str(item).strip()]
        elif isinstance(val, dict) and isinstance(out.get(key), dict):
            merged_sub = dict(out[key])
            for sub_k, sub_v in val.items():
                if isinstance(sub_v, dict) and isinstance(merged_sub.get(sub_k), dict):
                    merged_sub[sub_k] = {**merged_sub[sub_k], **sub_v}
                else:
                    merged_sub[sub_k] = sub_v
            out[key] = merged_sub
        elif key == "enforcement":
            enf = str(val or "warn").strip().lower()
            out[key] = enf if enf in ("off", "warn", "reject") else "warn"
        else:
            out[key] = val
    return out


def load_role_policy(workspace: str | None = None) -> dict[str, Any]:
    """Load effective local role policy, falling back cleanly to public defaults."""
    policy = copy.deepcopy(DEFAULT_ROLE_POLICY)
    policy["source"] = "default"
    policy["configured"] = False

    env_val = str(os.environ.get("HERMES_SWARM_ROLE_POLICY") or "").strip()
    if env_val.lower() in ("default", "none"):
        return policy
    if env_val.lower() == "off":
        policy["enforcement"] = "off"
        return policy

    chosen: Path | None = None
    if env_val:
        explicit = Path(env_val).expanduser()
        if explicit.is_file():
            chosen = explicit
        else:
            policy["source"] = str(explicit)
            policy["load_error"] = f"Configured HERMES_SWARM_ROLE_POLICY file not found: {explicit}"
            return policy
    else:
        for cand in candidate_role_policy_paths(workspace):
            try:
                if cand.is_file():
                    chosen = cand
                    break
            except OSError:
                continue

    if chosen is None:
        return policy

    try:
        raw = json.loads(chosen.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("role_policy JSON root must be an object")
        policy = _deep_merge_policy(policy, raw)
        policy["source"] = str(chosen)
        policy["configured"] = True
    except Exception as exc:
        policy["source"] = str(chosen)
        policy["load_error"] = f"Failed to parse role policy {chosen}: {exc}"
    return policy


def _matching_ide_role(
    raw_role: str | None,
    *,
    worker_name: str | None,
    ide_only_roles: set[str],
) -> tuple[str | None, str]:
    role_norm = str(raw_role or "").strip().lower()
    if role_norm and role_norm in ide_only_roles:
        return role_norm, "role"
    clean_name = str(worker_name or "").strip().lower()
    if clean_name:
        if clean_name in ide_only_roles:
            return clean_name, "name"
        head = _NAME_SEP_RE.split(clean_name, maxsplit=1)[0]
        if head and head in ide_only_roles:
            return head, "name"
    return None, ""


def evaluate_worker_spawn_policy(
    raw_role: str | None,
    *,
    worker_name: str | None = None,
    policy: dict[str, Any] | None = None,
    allow_ide_only_role: bool = False,
) -> tuple[bool, str | None]:
    """Check whether spawning a Hermes `swarm_worker` violates IDE-only role rules.

    Returns `(allowed, message)` where `allowed=False` applies when
    `enforcement == 'reject'` unless `allow_ide_only_role=True`.
    """
    pol = policy if isinstance(policy, dict) else DEFAULT_ROLE_POLICY
    if allow_ide_only_role:
        return True, None
    enf = str(pol.get("enforcement") or "warn").strip().lower()
    if enf == "off":
        return True, None

    ide_roles = {str(r).strip().lower() for r in (pol.get("ide_only_roles") or []) if str(r).strip()}
    matched, matched_via = _matching_ide_role(raw_role, worker_name=worker_name, ide_only_roles=ide_roles)
    if not matched:
        return True, None

    ide_prefs = pol.get("ide_subagent_preferences") if isinstance(pol.get("ide_subagent_preferences"), dict) else {}
    guidance = str(ide_prefs.get("guidance") or DEFAULT_IDE_GUIDANCE).strip()
    tiers = ide_prefs.get("preferred_tiers")
    tier_note = ""
    if isinstance(tiers, dict) and tiers:
        tier_pairs = ", ".join(f"{k}={v}" for k, v in tiers.items())
        tier_note = f" Preferred IDE tiers: {tier_pairs}."
    exec_roles = ", ".join(str(r) for r in (pol.get("hermes_execution_roles") or DEFAULT_HERMES_EXECUTION_ROLES))
    src = str(pol.get("source") or "default")

    if enf == "reject":
        msg = (
            f"Blocked `swarm_worker(action='spawn')`: {matched_via}={matched!r} is restricted to IDE subagents "
            f"under active role policy ({src}, enforcement='reject'). {guidance}{tier_note} "
            f"Use `invoke_subagent(TypeName='self', Role=...)` instead of `swarm_worker`, reserve Hermes workers "
            f"for execution roles ({exec_roles}), or pass `allow_ide_only_role=True` only for an explicit override."
        )
        return False, msg

    warn = (
        f"Role policy advisory ({src}, enforcement='warn'): {matched_via}={matched!r} matches `ide_only_roles`. "
        f"{guidance} Prefer `invoke_subagent(TypeName='self', Role=...)` for research, design, planning, and verification."
    )
    return True, warn


def apply_worker_defaults(
    policy: dict[str, Any] | None,
    *,
    model: str | None,
    provider: str | None,
    base_url: str | None,
    reasoning: str | None,
    profile: str | None,
) -> dict[str, str | None]:
    """Fill omitted `swarm_worker` model/provider settings from local `hermes_worker_defaults`."""
    pol = policy if isinstance(policy, dict) else DEFAULT_ROLE_POLICY
    defaults = pol.get("hermes_worker_defaults")
    if not isinstance(defaults, dict):
        defaults = {}

    def _pick(explicit: str | None, key: str, *, treat_default_as_unset: bool = False) -> str | None:
        val = str(explicit or "").strip()
        if val and not (treat_default_as_unset and val == "default"):
            return val
        d_val = str(defaults.get(key) or "").strip()
        if d_val:
            return d_val
        return val or None

    return {
        "model": _pick(model, "model"),
        "provider": _pick(provider, "provider"),
        "base_url": _pick(base_url, "base_url"),
        "reasoning": _pick(reasoning, "reasoning"),
        "profile": _pick(profile, "profile", treat_default_as_unset=True) or "default",
    }
