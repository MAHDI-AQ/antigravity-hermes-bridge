#!/usr/bin/env python3
"""Cross-platform installer for Hermes Swarm (human-friendly & deterministic `--non-interactive --json` for AI agents)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

try:
    from doctor import (
        default_antigravity_config_dir,
        default_antigravity_gemini_dir,
        default_hermes_home,
        default_mcp_install_dir,
        run_doctor,
    )
except ImportError:
    from scripts.doctor import (
        default_antigravity_config_dir,
        default_antigravity_gemini_dir,
        default_hermes_home,
        default_mcp_install_dir,
        run_doctor,
    )

REPO_ROOT = Path(__file__).resolve().parent.parent


def _copy_tree(src: Path, dst: Path, *, ignore_names: set[str], dry_run: bool, actions: list[str]) -> None:
    actions.append(f"copy {src} -> {dst}")
    if dry_run:
        return
    if dst.exists():
        # Preserve runtime state/venvs inside target if upgrading in place
        for root_dir, dirnames, filenames in os.walk(src):
            rel_root = Path(root_dir).relative_to(src)
            dirnames[:] = [d for d in dirnames if d not in ignore_names]
            (dst / rel_root).mkdir(parents=True, exist_ok=True)
            for fname in filenames:
                if fname in ignore_names:
                    continue
                shutil.copy2(Path(root_dir) / fname, dst / rel_root / fname)
    else:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*sorted(ignore_names)))


def _ensure_venv_and_install(mcp_dst: Path, *, dry_run: bool, skip_venv: bool, actions: list[str]) -> None:
    if skip_venv:
        actions.append("skip venv creation (--skip-venv)")
        return
    venv_dir = mcp_dst / ".venv"
    venv_py = (venv_dir / "Scripts" / "python.exe") if sys.platform == "win32" else (venv_dir / "bin" / "python")
    actions.append(f"ensure venv + editable install in {venv_dir}")
    if dry_run:
        return
    if not venv_py.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    pip_chk = subprocess.run([str(venv_py), "-m", "pip", "--version"], capture_output=True, text=True, check=False)
    if pip_chk.returncode != 0:
        subprocess.run([str(venv_py), "-m", "ensurepip", "--upgrade"], check=True)
    install_res = subprocess.run([str(venv_py), "-m", "pip", "install", "-e", str(mcp_dst)], check=False)
    if install_res.returncode != 0:
        subprocess.run(
            [str(venv_py), "-m", "pip", "install", "--no-build-isolation", "-e", str(mcp_dst)],
            check=True,
        )


def _patch_mcp_config(
    antigravity_config_dir: Path,
    mcp_dst: Path,
    hermes_home: Path,
    *,
    dry_run: bool,
    actions: list[str],
) -> Path:
    cfg_path = antigravity_config_dir / "mcp_config.json"
    venv_py = (mcp_dst / ".venv" / "Scripts" / "python.exe") if sys.platform == "win32" else (mcp_dst / ".venv" / "bin" / "python")
    actions.append(f"register hermes-a2a in {cfg_path}")
    if dry_run:
        return cfg_path
    antigravity_config_dir.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    raw = ""
    if cfg_path.is_file():
        raw = cfg_path.read_text(encoding="utf-8", errors="replace").strip()
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError(f"{cfg_path} must contain a JSON object")
            existing = parsed
    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        existing["mcpServers"] = servers
    new_entry = {
        "command": str(venv_py if venv_py.is_file() else sys.executable),
        "args": [str(mcp_dst / "server.py")],
        "env": {
            "HERMES_HOME": str(hermes_home),
            "HERMES_A2A_STATE_DIR": str(mcp_dst / "state"),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        },
        "disabled": False,
    }
    if servers.get("hermes-a2a") == new_entry:
        actions.append(f"unchanged {cfg_path} (skipped backup & write)")
        return cfg_path
    if cfg_path.is_file() and raw:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = cfg_path.with_name(f"mcp_config.json.bak.{stamp}")
        shutil.copy2(cfg_path, backup)
    servers["hermes-a2a"] = new_entry
    cfg_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return cfg_path


def main() -> int:
    if sys.version_info < (3, 10):
        msg = f"Python >= 3.10 is required (found {sys.version.split()[0]} at {sys.executable})"
        if "--json" in sys.argv:
            print(json.dumps({"ok": False, "error": msg}, indent=2))
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(description="Install Hermes Swarm (MCP server + Antigravity plugin + Hermes bridge).")
    parser.add_argument("--mcp-dir", type=Path, default=default_mcp_install_dir(), help="Target directory for mcp-server")
    parser.add_argument("--antigravity-config-dir", type=Path, default=None, help="Antigravity config dir (default ~/.gemini/config)")
    parser.add_argument("--gemini-dir", type=Path, default=None, help="Legacy Antigravity gemini root (derives <dir>/config when --antigravity-config-dir is omitted)")
    parser.add_argument("--hermes-dir", type=Path, default=default_hermes_home(), help="Hermes Agent home directory")
    parser.add_argument("--skip-venv", action="store_true", help="Do not create .venv or pip install mcp-server")
    parser.add_argument("--dry-run", action="store_true", help="Plan installation steps without modifying disk")
    parser.add_argument("--non-interactive", action="store_true", help="Run non-interactively (safe for CI and AI agents)")
    parser.add_argument("--json", action="store_true", help="Emit JSON result report")
    args = parser.parse_args()

    actions: list[str] = []
    mcp_src = REPO_ROOT / "packages" / "mcp-server"
    ag_src = REPO_ROOT / "packages" / "antigravity-plugin"
    bridge_src = REPO_ROOT / "packages" / "hermes-bridge"

    mcp_dst = args.mcp_dir.expanduser().resolve()
    if args.antigravity_config_dir is not None:
        ag_cfg_dir = args.antigravity_config_dir.expanduser().resolve()
        gemini_dir = (args.gemini_dir or default_antigravity_gemini_dir()).expanduser().resolve()
    elif args.gemini_dir is not None:
        gemini_dir = args.gemini_dir.expanduser().resolve()
        ag_cfg_dir = (gemini_dir.parent / "config") if gemini_dir.name == "antigravity" else (gemini_dir / "config")
    else:
        ag_cfg_dir = default_antigravity_config_dir().expanduser().resolve()
        gemini_dir = default_antigravity_gemini_dir().expanduser().resolve()
    ag_dst = ag_cfg_dir / "plugins" / "hermes-swarm"
    bridge_dst = args.hermes_dir.expanduser().resolve() / "plugins" / "hermes-swarm-bridge"

    _copy_tree(mcp_src, mcp_dst, ignore_names={"__pycache__", ".venv", "state", "dist", "build", "*.pyc"}, dry_run=args.dry_run, actions=actions)
    _ensure_venv_and_install(mcp_dst, dry_run=args.dry_run, skip_venv=args.skip_venv, actions=actions)
    _copy_tree(ag_src, ag_dst, ignore_names={"node_modules", "__pycache__", ".DS_Store"}, dry_run=args.dry_run, actions=actions)
    _copy_tree(bridge_src, bridge_dst, ignore_names={"__pycache__", "*.pyc"}, dry_run=args.dry_run, actions=actions)
    mcp_cfg_path = _patch_mcp_config(ag_cfg_dir, mcp_dst, args.hermes_dir.expanduser().resolve(), dry_run=args.dry_run, actions=actions)

    doctor_report = None if args.dry_run else run_doctor(
        mcp_dir=mcp_dst,
        antigravity_config_dir=ag_cfg_dir,
        gemini_dir=gemini_dir,
        hermes_home=args.hermes_dir.expanduser().resolve(),
    )

    result = {
        "ok": True if args.dry_run else bool(doctor_report and doctor_report.get("ok")),
        "dryRun": bool(args.dry_run),
        "paths": {
            "mcpDir": str(mcp_dst),
            "antigravityPluginDir": str(ag_dst),
            "hermesBridgeDir": str(bridge_dst),
            "mcpConfig": str(mcp_cfg_path),
        },
        "actions": actions,
        "doctor": doctor_report,
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for act in actions:
            print(f"-> {act}")
        if doctor_report:
            print("\nDoctor Verification:")
            for chk in doctor_report["checks"]:
                print(f"  [{'PASS' if chk['ok'] else 'FAIL'}] {chk['name']}: {chk['detail']}")
        print("\nNext step: Reload Antigravity Window and run `hermes gateway restart` (without `--yes` if a live session is active) if Hermes Gateway is running.")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
