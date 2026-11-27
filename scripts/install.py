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

from doctor import (
    default_antigravity_config_dir,
    default_antigravity_gemini_dir,
    default_hermes_home,
    run_doctor,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _default_target_mcp_dir() -> Path:
    env = (os.environ.get("HERMES_SWARM_MCP_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "hermes-swarm" / "mcp-server"


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
    subprocess.run([str(venv_py), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([str(venv_py), "-m", "pip", "install", "-e", str(mcp_dst)], check=True)


def _patch_mcp_config(
    gemini_dir: Path,
    mcp_dst: Path,
    hermes_home: Path,
    *,
    dry_run: bool,
    actions: list[str],
) -> Path:
    cfg_path = gemini_dir / "mcp_config.json"
    venv_py = (mcp_dst / ".venv" / "Scripts" / "python.exe") if sys.platform == "win32" else (mcp_dst / ".venv" / "bin" / "python")
    actions.append(f"register hermes-a2a in {cfg_path}")
    if dry_run:
        return cfg_path
    gemini_dir.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if cfg_path.is_file():
        raw = cfg_path.read_text(encoding="utf-8", errors="replace").strip()
        if raw:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = cfg_path.with_name(f"mcp_config.json.bak.{stamp}")
            shutil.copy2(cfg_path, backup)
            existing = json.loads(raw)
    servers = existing.setdefault("mcpServers", {})
    servers["hermes-a2a"] = {
        "command": str(venv_py if venv_py.is_file() else sys.executable),
        "args": [str(mcp_dst / "server.py")],
        "env": {
            "HERMES_HOME": str(hermes_home),
            "HERMES_A2A_STATE_DIR": str(mcp_dst / "state"),
        },
        "disabled": False,
    }
    cfg_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return cfg_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Hermes Swarm (MCP server + Antigravity plugin + Hermes bridge).")
    parser.add_argument("--mcp-dir", type=Path, default=_default_target_mcp_dir(), help="Target directory for mcp-server")
    parser.add_argument("--antigravity-config-dir", type=Path, default=default_antigravity_config_dir(), help="Antigravity config dir (~/.gemini/config)")
    parser.add_argument("--gemini-dir", type=Path, default=default_antigravity_gemini_dir(), help="Antigravity gemini root (~/.gemini/antigravity)")
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
    ag_dst = args.antigravity_config_dir.expanduser().resolve() / "plugins" / "hermes-swarm"
    bridge_dst = args.hermes_dir.expanduser().resolve() / "plugins" / "hermes-swarm-bridge"

    _copy_tree(mcp_src, mcp_dst, ignore_names={"__pycache__", ".venv", "state", "dist", "build", "*.pyc"}, dry_run=args.dry_run, actions=actions)
    _ensure_venv_and_install(mcp_dst, dry_run=args.dry_run, skip_venv=args.skip_venv, actions=actions)
    _copy_tree(ag_src, ag_dst, ignore_names={"node_modules", "__pycache__", ".DS_Store"}, dry_run=args.dry_run, actions=actions)
    _copy_tree(bridge_src, bridge_dst, ignore_names={"__pycache__", "*.pyc"}, dry_run=args.dry_run, actions=actions)
    _patch_mcp_config(args.gemini_dir.expanduser().resolve(), mcp_dst, args.hermes_dir.expanduser().resolve(), dry_run=args.dry_run, actions=actions)

    doctor_report = None if args.dry_run else run_doctor(
        mcp_dir=mcp_dst,
        antigravity_config_dir=args.antigravity_config_dir.expanduser().resolve(),
        gemini_dir=args.gemini_dir.expanduser().resolve(),
        hermes_home=args.hermes_dir.expanduser().resolve(),
    )

    result = {
        "ok": True if args.dry_run else bool(doctor_report and doctor_report.get("ok")),
        "dryRun": bool(args.dry_run),
        "paths": {
            "mcpDir": str(mcp_dst),
            "antigravityPluginDir": str(ag_dst),
            "hermesBridgeDir": str(bridge_dst),
            "mcpConfig": str(args.gemini_dir.expanduser().resolve() / "mcp_config.json"),
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
        print("\nNext step: Reload Antigravity and run `hermes gateway restart` if Hermes Gateway is running.")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
