"""Secret-free dynamic introspection of local Hermes CLI, profiles, models, skills, and A2A peers."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

import yaml

import store
import win_launcher

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CACHE_LOCK = threading.Lock()
_CATALOG_CACHE: dict[tuple[str, str, str, bool], tuple[float, dict[str, Any]]] = {}
_SKILLS_SCAN_CACHE: dict[str, tuple[float, list[str]]] = {}


def _default_ttl_seconds() -> float:
    raw = os.environ.get("HERMES_A2A_CATALOG_TTL_S", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return 60.0


def _hermes_exe() -> Path:
    return store.default_hermes_exe()


def _hermes_home() -> Path:
    return store.default_hermes_home()


def _peers_file() -> Path:
    return store.get_state_dir() / "peers.json"


def _read_yaml_safe(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_provider_models_cache(hermes_home: Path) -> dict[str, list[str]]:
    """Read non-secret cached model lists from `provider_models_cache.json` and `ollama_cloud_models_cache.json`."""
    out: dict[str, list[str]] = {}
    cache_path = hermes_home / "provider_models_cache.json"
    raw = store.read_json_safe(cache_path, None)
    if isinstance(raw, dict):
        for key, entry in raw.items():
            if isinstance(entry, dict) and isinstance(entry.get("models"), list):
                out[str(key)] = [str(m) for m in entry["models"] if str(m).strip()]
            elif isinstance(entry, list):
                out[str(key)] = [str(m) for m in entry if str(m).strip()]

    ollama_cache = hermes_home / "ollama_cloud_models_cache.json"
    raw_ol = store.read_json_safe(ollama_cache, None)
    if isinstance(raw_ol, dict) and isinstance(raw_ol.get("models"), list):
        out["ollama-cloud"] = [str(m) for m in raw_ol["models"] if str(m).strip()]
    elif isinstance(raw_ol, list):
        out["ollama-cloud"] = [str(m) for m in raw_ol if str(m).strip()]
    return out


def _match_cached_models(
    provider_name: str,
    base_url: str | None,
    models_cache: dict[str, list[str]],
) -> list[str]:
    if provider_name in models_cache:
        return models_cache[provider_name]
    if base_url:
        clean_base = str(base_url).rstrip("/").lower()
        for ckey, cmodels in models_cache.items():
            if not ckey.lower().startswith("custom:"):
                continue
            rest = ckey[len("custom:"):]
            url_part = rest.split("#", 1)[0].rstrip("/").lower()
            if url_part == clean_base:
                return cmodels
    return []


def resolve_profile_home(profile: str = "default") -> Path:
    hhome = _hermes_home()
    if not profile or profile == "default":
        return hhome
    return hhome / "profiles" / profile


def list_profiles() -> list[str]:
    profiles = ["default"]
    profiles_dir = _hermes_home() / "profiles"
    if profiles_dir.is_dir():
        for entry in sorted(profiles_dir.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                profiles.append(entry.name)
    return profiles


def load_custom_peers() -> dict[str, dict[str, Any]]:
    pfile = _peers_file()
    if not pfile.is_file():
        return {}
    data = store.read_json_safe(pfile, {})
    return data if isinstance(data, dict) else {}


def save_custom_peers(peers: dict[str, dict[str, Any]]) -> None:
    pfile = _peers_file()
    with store.atomic_file_lock(pfile.with_suffix(".lock")):
        store.atomic_write_json(pfile, peers)


def _sanitize_peers(raw_peers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Strip inline bearer/api tokens from peer metadata returned to callers."""
    out: dict[str, dict[str, Any]] = {}
    for name, cfg in raw_peers.items():
        if not isinstance(cfg, dict) or not cfg.get("url"):
            continue
        entry: dict[str, Any] = {
            "url": str(cfg["url"]).rstrip("/"),
            "timeout": cfg.get("timeout", 120),
            "capabilities": list(cfg.get("capabilities") or []),
            "description": cfg.get("description", ""),
        }
        auth = cfg.get("auth")
        if isinstance(auth, dict) and auth.get("type"):
            entry["auth"] = {
                "type": auth.get("type"),
                "configured": bool(auth.get("token") or auth.get("token_env")),
                **({"token_env": auth["token_env"]} if auth.get("token_env") else {}),
            }
        out[str(name)] = entry
    return out


def clear_catalog_cache() -> None:
    """Clear all cached Hermes catalog snapshots."""
    with _CACHE_LOCK:
        _CATALOG_CACHE.clear()
        _SKILLS_SCAN_CACHE.clear()


def _resolve_project_skill_dirs(workdir: str | None) -> tuple[str, list[Path]]:
    """Return ``(normalized_workdir, existing_skill_dirs)`` for project-local ``.agents/skills`` and ``.hermes/skills``."""
    raw = str(workdir or "").strip()
    if not raw or raw.lower() == "auto":
        return "", []
    norm = store.normalize_local_path(raw) or raw
    try:
        wd = Path(norm).expanduser().resolve()
    except OSError:
        return norm, []
    if not wd.is_dir():
        return str(wd), []

    home = Path.home().resolve()
    roots: list[Path] = [wd]
    if wd != home and not (wd / ".git").exists():
        for parent in wd.parents:
            if parent == home:
                break
            if (parent / ".git").exists():
                roots.append(parent)
                break

    skill_dirs: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for sub in (".agents", ".hermes"):
            cand = root / sub / "skills"
            try:
                if cand.is_dir():
                    resolved = cand.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        skill_dirs.append(resolved)
            except OSError:
                continue
    return str(wd), skill_dirs


def _scan_skills_dirs(skills_dirs: list[Path], now_ts: float) -> set[str]:
    """Scan ``SKILL.md`` directories with TTL caching, excluding hidden/archive/vendor dirs."""
    if not skills_dirs:
        return set()
    cache_key = "|".join(str(p) for p in skills_dirs)
    with _CACHE_LOCK:
        cached_skills = _SKILLS_SCAN_CACHE.get(cache_key)
    if cached_skills and (now_ts - cached_skills[0]) < 60.0:
        return set(cached_skills[1])

    excluded = {".archive", ".git", ".venv", "venv", "node_modules", "__pycache__"}
    found: set[str] = set()
    for sdir in skills_dirs:
        if not sdir.is_dir():
            continue
        for md in sdir.rglob("SKILL.md"):
            if any(part in excluded for part in md.parts):
                continue
            found.add(md.parent.name)
    with _CACHE_LOCK:
        _SKILLS_SCAN_CACHE[cache_key] = (now_ts, sorted(found))
    return found


def _build_static_catalog(profile: str = "default", *, workdir: str | None = None) -> dict[str, Any]:
    hhome = _hermes_home()
    profile_home = resolve_profile_home(profile)
    cfg = _read_yaml_safe(profile_home / "config.yaml")

    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    providers_raw = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    models_cache = _read_provider_models_cache(profile_home)
    if profile_home != hhome:
        root_cache = _read_provider_models_cache(hhome)
        for k, v in root_cache.items():
            models_cache.setdefault(k, v)

    providers_clean: dict[str, Any] = {}
    for pname, pcfg in providers_raw.items():
        if not isinstance(pcfg, dict):
            continue
        base_url = pcfg.get("base_url")
        models_map = pcfg.get("models") if isinstance(pcfg.get("models"), dict) else {}
        merged_models = list(models_map.keys())
        for cached_m in _match_cached_models(str(pname), str(base_url) if base_url else None, models_cache):
            if cached_m not in merged_models:
                merged_models.append(cached_m)
        providers_clean[str(pname)] = {
            "name": pcfg.get("name", pname),
            "base_url": base_url,
            "default_model": pcfg.get("default_model") or pcfg.get("model"),
            "models": merged_models,
        }

    # Include discovered cache-only providers (without exposing any credentials).
    for ckey, cmodels in models_cache.items():
        if ckey.startswith("custom:") or ckey in providers_clean:
            continue
        providers_clean[ckey] = {
            "name": ckey,
            "base_url": None,
            "default_model": None,
            "models": list(cmodels),
        }

    known_toolsets = cfg.get("known_plugin_toolsets") if isinstance(cfg.get("known_plugin_toolsets"), dict) else {}
    platform_toolsets = cfg.get("platform_toolsets") if isinstance(cfg.get("platform_toolsets"), dict) else {}
    cli_toolsets = sorted({
        *[str(x) for x in (platform_toolsets.get("cli") or [])],
        *[str(x) for x in (known_toolsets.get("cli") or [])],
    })

    skills_cfg = cfg.get("skills") if isinstance(cfg.get("skills"), dict) else {}
    disabled_skills = {
        str(s).strip()
        for s in (skills_cfg.get("disabled") or [])
        if str(s).strip()
    }

    now_ts = time.time()
    global_skills_dirs = [profile_home / "skills", hhome / "hermes-agent" / "skills"]
    discovered_skills = _scan_skills_dirs(global_skills_dirs, now_ts)
    norm_wd, project_skills_dirs = _resolve_project_skill_dirs(workdir)
    project_skills = _scan_skills_dirs(project_skills_dirs, now_ts)
    enabled_skills = sorted((discovered_skills | project_skills) - disabled_skills)
    project_skills_clean = sorted(project_skills - disabled_skills)

    config_a2a_peers = cfg.get("a2a_agents") if isinstance(cfg.get("a2a_agents"), dict) else {}
    merged_peers = {**_sanitize_peers(config_a2a_peers), **_sanitize_peers(load_custom_peers())}

    gw_a2a = (
        (cfg.get("gateway") or {}).get("platforms", {}).get("a2a", {})
        if isinstance(cfg.get("gateway"), dict)
        else {}
    )
    gw_a2a_enabled = bool(isinstance(gw_a2a, dict) and gw_a2a.get("enabled"))
    gw_a2a_port = int(((gw_a2a.get("extra") or {}).get("port") or 9900)) if isinstance(gw_a2a, dict) else 9900
    if gw_a2a_enabled and "hermes-gateway-live" not in merged_peers:
        merged_peers["hermes-gateway-live"] = {
            "url": f"http://127.0.0.1:{gw_a2a_port}",
            "description": "Hermes live gateway A2A platform adapter",
            "capabilities": ["live-session"],
        }

    agent_cfg = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}

    return {
        "hermes_exe": str(_hermes_exe()),
        "hermes_home": str(profile_home),
        "profile": profile,
        "profiles": list_profiles(),
        "default_model": model_cfg.get("default") or "",
        "default_provider": model_cfg.get("provider") or "",
        "aliases": dict(model_cfg.get("aliases") or {}),
        "providers": providers_clean,
        "cli_toolsets": cli_toolsets,
        "enabled_skills": enabled_skills,
        "project_skills": project_skills_clean,
        "active_workdir": norm_wd or None,
        "max_turns_default": agent_cfg.get("max_turns", 1500),
        "gateway_a2a": {"enabled": gw_a2a_enabled, "port": gw_a2a_port},
        "a2a_peers": merged_peers,
        "cached_at": time.time(),
    }


def _parse_profile_list_output(stdout: str) -> list[dict[str, str]]:
    """Parse `hermes profile list` table lines into structured profile descriptors."""
    rows: list[dict[str, str]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("Profile", "─", "-")):
            continue
        is_active = line.startswith("◆") or line.startswith("*")
        cleaned = line.lstrip("◆* ").strip()
        parts = re.split(r"\s{2,}", cleaned)
        if not parts:
            continue
        name = parts[0].strip()
        if not name:
            continue
        rows.append({
            "name": name,
            "active": "true" if is_active else "false",
            "model": parts[1].strip() if len(parts) > 1 and parts[1].strip() != "—" else "",
            "gateway": parts[2].strip() if len(parts) > 2 and parts[2].strip() != "—" else "",
            "alias": parts[3].strip() if len(parts) > 3 and parts[3].strip() != "—" else "",
        })
    return rows


def get_hermes_catalog(
    profile: str = "default",
    *,
    workdir: str | None = None,
    refresh: bool = False,
    ttl_seconds: float | None = None,
) -> dict[str, Any]:
    """Read secret-free model, provider, profile, toolset, skill, and peer catalog with TTL cache."""
    ttl = _default_ttl_seconds() if ttl_seconds is None else max(0.0, float(ttl_seconds))
    norm_wd, _ = _resolve_project_skill_dirs(workdir)
    cache_key = (str(_hermes_home()), profile or "default", norm_wd, False)
    now = time.monotonic()

    if not refresh and ttl > 0:
        with _CACHE_LOCK:
            cached = _CATALOG_CACHE.get(cache_key)
            if cached is not None and (now - cached[0]) < ttl:
                return copy.deepcopy(cached[1])

    catalog = _build_static_catalog(profile=profile, workdir=workdir)
    with _CACHE_LOCK:
        _CATALOG_CACHE[cache_key] = (now, copy.deepcopy(catalog))
    return catalog


async def discover_hermes_catalog(
    profile: str = "default",
    *,
    workdir: str | None = None,
    refresh: bool = False,
    query_cli: bool = True,
    ttl_seconds: float | None = None,
    cli_timeout: float = 10.0,
) -> dict[str, Any]:
    """Asynchronously discover live Hermes catalog via CLI (`profile list`, `--version`) + config caches."""
    ttl = _default_ttl_seconds() if ttl_seconds is None else max(0.0, float(ttl_seconds))
    norm_wd, _ = _resolve_project_skill_dirs(workdir)
    cache_key = (str(_hermes_home()), profile or "default", norm_wd, bool(query_cli))
    now = time.monotonic()

    if not refresh and ttl > 0:
        with _CACHE_LOCK:
            cached = _CATALOG_CACHE.get(cache_key)
            if cached is not None and (now - cached[0]) < ttl:
                return copy.deepcopy(cached[1])

    catalog = await asyncio.to_thread(_build_static_catalog, profile=profile, workdir=workdir)
    if query_cli and _hermes_exe().is_file():
        ver_res, prof_res = await asyncio.gather(
            run_hermes_cli(["--version"], profile="default", timeout=cli_timeout),
            run_hermes_cli(["profile", "list"], profile="default", timeout=cli_timeout),
        )
        if ver_res.get("ok") and ver_res.get("stdout"):
            first_line = ver_res["stdout"].splitlines()[0].strip()
            catalog["hermes_version"] = first_line
        if prof_res.get("ok") and prof_res.get("stdout"):
            parsed_profiles = _parse_profile_list_output(prof_res["stdout"])
            if parsed_profiles:
                catalog["profile_details"] = parsed_profiles
                merged_names = list(catalog.get("profiles") or [])
                for row in parsed_profiles:
                    if row["name"] not in merged_names:
                        merged_names.append(row["name"])
                catalog["profiles"] = merged_names

    with _CACHE_LOCK:
        _CATALOG_CACHE[cache_key] = (time.monotonic(), copy.deepcopy(catalog))
    return catalog


def resolve_peer_raw(name_or_url: str, profile: str = "default") -> dict[str, Any] | None:
    """Resolve raw peer config (including auth lookup) by peer name or URL."""
    if name_or_url.startswith(("http://", "https://")):
        return {"url": name_or_url.rstrip("/"), "timeout": 120}

    custom = load_custom_peers()
    if name_or_url in custom:
        return custom[name_or_url]

    cfg = _read_yaml_safe(resolve_profile_home(profile) / "config.yaml")
    a2a_agents = cfg.get("a2a_agents") if isinstance(cfg.get("a2a_agents"), dict) else {}
    if name_or_url in a2a_agents and isinstance(a2a_agents[name_or_url], dict):
        return a2a_agents[name_or_url]

    if name_or_url == "hermes-gateway-live":
        gw_a2a = (cfg.get("gateway") or {}).get("platforms", {}).get("a2a", {})
        port = int(((gw_a2a.get("extra") or {}).get("port") or 9900)) if isinstance(gw_a2a, dict) else 9900
        return {"url": f"http://127.0.0.1:{port}", "timeout": 300}

    return None


async def run_hermes_cli(
    args: list[str],
    *,
    profile: str = "default",
    cwd: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Execute a short hermes CLI management/query command safely with bounded timeout kill."""
    cmd = [str(_hermes_exe())]
    if profile and profile != "default":
        cmd.extend(["-p", profile])
    cmd.extend(args)

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd or None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        **win_launcher.hidden_subprocess_kwargs(new_process_group=True),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        await win_launcher.kill_process_tree(proc)
        raise
    except (asyncio.TimeoutError, TimeoutError):
        await win_launcher.kill_process_tree(proc)
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": f"Timed out after {timeout}s"}

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": _ANSI_RE.sub("", stdout).strip(),
        "stderr": _ANSI_RE.sub("", stderr).strip(),
    }


async def run_git_inspect(
    workdir: str,
    action: str = "status",
    extra_args: list[str] | None = None,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Run read-only git status/diff/worktree inspection in a workspace with timeout kill."""
    norm_wd = store.normalize_local_path(workdir)
    if not norm_wd or not Path(norm_wd).exists():
        return {"ok": False, "error": f"Directory does not exist: {workdir}"}
    if action == "status":
        git_args = ["status", "--short", "--branch"]
    elif action == "diff":
        git_args = ["diff", *(extra_args or [])]
    elif action == "diff_stat":
        git_args = ["diff", "--stat", *(extra_args or [])]
    elif action == "worktree_list":
        git_args = ["worktree", "list", "--porcelain"]
    else:
        return {"ok": False, "error": f"Unsupported git inspect action: {action}"}

    proc = await asyncio.create_subprocess_exec(
        "git",
        *git_args,
        cwd=norm_wd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **win_launcher.hidden_subprocess_kwargs(new_process_group=True),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        await win_launcher.kill_process_tree(proc)
        raise
    except (asyncio.TimeoutError, TimeoutError):
        await win_launcher.kill_process_tree(proc)
        return {
            "ok": False,
            "exit_code": -1,
            "workdir": norm_wd,
            "stdout": "",
            "stderr": f"git {action} timed out after {timeout}s",
        }

    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "workdir": norm_wd,
        "stdout": stdout_b.decode("utf-8", errors="replace").strip(),
        "stderr": stderr_b.decode("utf-8", errors="replace").strip(),
    }
