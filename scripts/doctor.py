#!/usr/bin/env python3
"""Diagnostic verifier for an installed Hermes Swarm setup (human or `--json` for agents)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


def default_antigravity_config_dir() -> Path:
    env = (os.environ.get("ANTIGRAVITY_CONFIG_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".gemini" / "config"


def default_antigravity_gemini_dir() -> Path:
    env = (os.environ.get("ANTIGRAVITY_GEMINI_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".gemini" / "antigravity"


def default_hermes_home() -> Path:
    env = (os.environ.get("HERMES_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        local_app = (os.environ.get("LOCALAPPDATA") or "").strip()
        if local_app:
            cand = Path(local_app) / "hermes"
            if cand.exists() or not (Path.home() / ".hermes").exists():
                return cand
    return Path.home() / ".hermes"


def default_mcp_install_dir() -> Path:
    env = (os.environ.get("HERMES_SWARM_MCP_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "hermes-swarm" / "mcp-server"


def _inspect_mcp_config_file(cfg_path: Path) -> tuple[bool, str, Path | None]:
    if not cfg_path.is_file():
        return False, f"Missing {cfg_path}", None
    try:
        doc = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            return False, f"Invalid JSON object in {cfg_path}", None
        servers = doc.get("mcpServers")
        srv = servers.get("hermes-a2a") if isinstance(servers, dict) else None
        ok = isinstance(srv, dict) and bool(srv.get("command")) and not bool(srv.get("disabled"))
        detected_mcp_dir: Path | None = None
        if isinstance(srv, dict) and isinstance(srv.get("args"), list) and srv["args"]:
            first_arg = Path(str(srv["args"][0])).expanduser()
            if first_arg.name == "server.py" and first_arg.is_file():
                detected_mcp_dir = first_arg.parent.resolve()
        detail = f"configured in {cfg_path}" if ok else f"hermes-a2a missing or disabled in {cfg_path}"
        return ok, detail, detected_mcp_dir
    except Exception as exc:
        return False, f"Invalid JSON in {cfg_path}: {exc}", None


def _resolve_antigravity_mcp_config(ag_cfg: Path, ag_gemini: Path) -> tuple[bool, str, Path | None]:
    primary_cfg = ag_cfg / "mcp_config.json"
    ok, detail, detected_dir = _inspect_mcp_config_file(primary_cfg)
    if ok:
        return True, detail, detected_dir

    legacy_candidates: list[Path] = []
    for cand in (ag_gemini / "mcp_config.json", Path.home() / ".gemini" / "antigravity" / "mcp_config.json"):
        resolved = cand.resolve()
        if resolved != primary_cfg.resolve() and resolved not in legacy_candidates:
            legacy_candidates.append(resolved)

    for legacy_cfg in legacy_candidates:
        leg_ok, _, leg_dir = _inspect_mcp_config_file(legacy_cfg)
        if leg_ok:
            return True, f"configured in legacy {legacy_cfg} (recommended: {primary_cfg})", leg_dir

    return False, detail, detected_dir


def check_node_version() -> tuple[bool, str]:
    node = shutil.which("node")
    if not node:
        return False, "node binary not found in PATH (Node.js >= 20 required for Antigravity sidecar)"
    try:
        res = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5, check=False)
        ver = res.stdout.strip()
        major = int(ver.lstrip("v").split(".")[0]) if ver else 0
        ok = res.returncode == 0 and major >= 20
        return ok, f"{node} ({ver})" if ok else f"Node.js >= 20 required, found {ver}"
    except Exception as exc:
        return False, f"Failed checking node: {exc}"


def run_doctor(
    *,
    mcp_dir: Path | None = None,
    antigravity_config_dir: Path | None = None,
    gemini_dir: Path | None = None,
    hermes_home: Path | None = None,
) -> dict[str, Any]:
    ag_gemini = (gemini_dir or default_antigravity_gemini_dir()).expanduser().resolve()
    if antigravity_config_dir is not None:
        ag_cfg = antigravity_config_dir.expanduser().resolve()
    elif gemini_dir is not None:
        cand_cfg = (ag_gemini.parent / "config") if ag_gemini.name == "antigravity" else (ag_gemini / "config")
        if (cand_cfg / "plugins" / "hermes-swarm").is_dir() or (cand_cfg / "mcp_config.json").is_file():
            ag_cfg = cand_cfg
        else:
            ag_cfg = default_antigravity_config_dir().expanduser().resolve()
    else:
        ag_cfg = default_antigravity_config_dir().expanduser().resolve()
    h_home = (hermes_home or default_hermes_home()).resolve()

    mcp_cfg_ok, mcp_cfg_detail, configured_mcp_dir = _resolve_antigravity_mcp_config(ag_cfg, ag_gemini)

    if mcp_dir is not None:
        mcp_root = mcp_dir.expanduser().resolve()
    else:
        mcp_root = default_mcp_install_dir().resolve()
        if not (mcp_root / "server.py").is_file():
            if configured_mcp_dir is not None and (configured_mcp_dir / "server.py").is_file():
                mcp_root = configured_mcp_dir
            else:
                legacy_mcp = (Path.home() / "Tools" / "hermes-a2a-mcp").resolve()
                if (legacy_mcp / "server.py").is_file():
                    mcp_root = legacy_mcp

    checks: list[dict[str, Any]] = []

    py_ok = sys.version_info >= (3, 10)
    checks.append({
        "name": "python",
        "ok": py_ok,
        "detail": f"{sys.executable} ({sys.version.split()[0]})",
    })

    node_ok, node_detail = check_node_version()
    checks.append({"name": "node", "ok": node_ok, "detail": node_detail})

    # MCP server files & venv
    venv_py = (mcp_root / ".venv" / "Scripts" / "python.exe") if sys.platform == "win32" else (mcp_root / ".venv" / "bin" / "python")
    server_py = mcp_root / "server.py"
    mcp_import_ok = False
    mcp_detail = f"server={server_py.is_file()}, venv={venv_py.is_file()}"
    if server_py.is_file() and venv_py.is_file():
        try:
            res = subprocess.run(
                [str(venv_py), "-c", "import mcp, starlette, uvicorn, httpx; print('IMPORTS_OK')"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            mcp_import_ok = res.returncode == 0 and "IMPORTS_OK" in res.stdout
            mcp_detail = f"OK ({venv_py})" if mcp_import_ok else f"Import error: {(res.stderr or res.stdout).strip()[:160]}"
        except Exception as exc:
            mcp_detail = f"Error checking venv: {exc}"
    checks.append({"name": "mcp_server", "ok": server_py.is_file() and mcp_import_ok, "detail": mcp_detail})

    # Antigravity plugin
    ag_plugin_dir = ag_cfg / "plugins" / "hermes-swarm"
    plugin_json = ag_plugin_dir / "plugin.json"
    sidecar_dir = ag_plugin_dir / "sidecars" / "swarm_board"
    sidecar_main = sidecar_dir / "main.mjs"
    sidecar_wakeup = sidecar_dir / "wakeup_bridge.mjs"
    ag_ok = plugin_json.is_file() and sidecar_main.is_file() and sidecar_wakeup.is_file()
    ag_detail = str(ag_plugin_dir)
    if not ag_ok:
        missing = [
            p.name
            for p in (plugin_json, sidecar_main, sidecar_wakeup)
            if not p.is_file()
        ]
        ag_detail = f"{ag_plugin_dir} (missing: {', '.join(missing)})"
    elif node_ok:
        for js_file in (sidecar_main, sidecar_wakeup):
            chk = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True, timeout=5, check=False)
            if chk.returncode != 0:
                ag_ok = False
                err_txt = (chk.stderr or chk.stdout).strip()[:160]
                ag_detail = f"{ag_plugin_dir} (node --check failed for {js_file.name}: {err_txt})"
                break
    checks.append({
        "name": "antigravity_plugin",
        "ok": ag_ok,
        "detail": ag_detail,
    })

    # Antigravity MCP config
    checks.append({"name": "antigravity_mcp_config", "ok": mcp_cfg_ok, "detail": mcp_cfg_detail})

    # Hermes bridge plugin
    bridge_dir = h_home / "plugins" / "hermes-swarm-bridge"
    bridge_ok = (bridge_dir / "plugin.yaml").is_file() and (bridge_dir / "swarm_tools.py").is_file() and (bridge_dir / "__init__.py").is_file()
    if bridge_ok:
        chk_b = subprocess.run(
            [sys.executable, "-m", "py_compile", str(bridge_dir / "__init__.py"), str(bridge_dir / "swarm_tools.py")],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        bridge_ok = chk_b.returncode == 0
    checks.append({
        "name": "hermes_bridge_plugin",
        "ok": bridge_ok,
        "detail": str(bridge_dir),
    })

    # Local role policy (optional — default policy is valid when no override file exists)
    rp_ok = True
    rp_detail = "default (enforcement=warn)"
    try:
        sys.path.insert(0, str(mcp_root))
        import role_policy as _rp_mod  # type: ignore[import-not-found]
        rp = _rp_mod.load_role_policy()
        if rp.get("load_error"):
            rp_ok = False
            rp_detail = str(rp["load_error"])
        else:
            rp_detail = f"{rp.get('source', 'default')} (enforcement={rp.get('enforcement', 'warn')})"
    except Exception as exc:
        rp_detail = f"default (role_policy module unavailable: {exc})"
    checks.append({
        "name": "role_policy",
        "ok": rp_ok,
        "detail": rp_detail,
    })

    overall = all(bool(c["ok"]) for c in checks)
    return {"ok": overall, "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Hermes Swarm installation across MCP, Antigravity, and Hermes.")
    parser.add_argument("--mcp-dir", type=Path, default=None)
    parser.add_argument("--antigravity-config-dir", type=Path, default=None)
    parser.add_argument("--gemini-dir", type=Path, default=None)
    parser.add_argument("--hermes-dir", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="Print structured JSON output")
    args = parser.parse_args()

    report = run_doctor(
        mcp_dir=args.mcp_dir,
        antigravity_config_dir=args.antigravity_config_dir,
        gemini_dir=args.gemini_dir,
        hermes_home=args.hermes_dir,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for item in report["checks"]:
            mark = "PASS" if item["ok"] else "FAIL"
            print(f"[{mark}] {item['name']}: {item['detail']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
