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
    return Path.home() / "Tools" / "hermes-a2a-mcp" if (Path.home() / "Tools" / "hermes-a2a-mcp").exists() else (Path.home() / ".local" / "share" / "hermes-swarm" / "mcp-server")


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
    mcp_root = (mcp_dir or default_mcp_install_dir()).resolve()
    ag_cfg = (antigravity_config_dir or default_antigravity_config_dir()).resolve()
    ag_gemini = (gemini_dir or default_antigravity_gemini_dir()).resolve()
    h_home = (hermes_home or default_hermes_home()).resolve()

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
    sidecar_main = ag_plugin_dir / "sidecars" / "swarm_board" / "main.mjs"
    ag_ok = plugin_json.is_file() and sidecar_main.is_file()
    if ag_ok and node_ok:
        chk = subprocess.run(["node", "--check", str(sidecar_main)], capture_output=True, text=True, timeout=5, check=False)
        ag_ok = chk.returncode == 0
    checks.append({
        "name": "antigravity_plugin",
        "ok": ag_ok,
        "detail": str(ag_plugin_dir),
    })

    # Antigravity MCP config
    mcp_cfg_path = ag_gemini / "mcp_config.json"
    mcp_cfg_ok = False
    mcp_cfg_detail = f"Missing {mcp_cfg_path}"
    if mcp_cfg_path.is_file():
        try:
            doc = json.loads(mcp_cfg_path.read_text(encoding="utf-8"))
            srv = (doc.get("mcpServers") or {}).get("hermes-a2a")
            mcp_cfg_ok = isinstance(srv, dict) and bool(srv.get("command")) and not bool(srv.get("disabled"))
            mcp_cfg_detail = f"configured in {mcp_cfg_path}" if mcp_cfg_ok else f"hermes-a2a missing or disabled in {mcp_cfg_path}"
        except Exception as exc:
            mcp_cfg_detail = f"Invalid JSON in {mcp_cfg_path}: {exc}"
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
