#!/usr/bin/env python3
"""Prepare and verify a NousResearch/hermes-agent plugin-catalog entry.

Runs NousResearch's exact pre-submission checks (`hermes plugins validate
--install-deps`, plus `validate_plugin_catalog.py` and `scan_plugin_payloads.py`
when a local hermes-agent checkout is present) and optionally stamps a target
40-character commit SHA and maintainer handle into the catalog YAML.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIR = REPO_ROOT / "packages" / "hermes-bridge"
DEFAULT_CATALOG_YAML = REPO_ROOT / "docs" / "marketplace" / "antigravity-hermes-bridge.yaml"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _find_hermes_repo(explicit: str | None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        candidates.append(Path(env_home).expanduser() / "hermes-agent")
    candidates.extend([
        Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent",
        Path.home() / ".hermes" / "hermes-agent",
    ])
    for cand in candidates:
        if (cand / "scripts" / "validate_plugin_catalog.py").is_file():
            return cand
    return None


def _hermes_venv_python(hermes_repo: Path) -> str:
    for rel in ("venv/Scripts/python.exe", "venv/bin/python"):
        py = hermes_repo / rel
        if py.is_file():
            return str(py)
    return sys.executable


def _run(cmd: list[str], *, cwd: Path = REPO_ROOT) -> int:
    print(f"+ {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(cwd), check=False)
    return int(proc.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare and validate Hermes Marketplace catalog entry.")
    parser.add_argument("--sha", default="", help="40-character lowercase hex commit SHA to pin")
    parser.add_argument("--maintainer", default="", help="Maintainer GitHub handle to stamp")
    parser.add_argument("--out", default="", help="Output path for stamped YAML (defaults to stdout/in-place only if provided)")
    parser.add_argument("--hermes-repo", default="", help="Path to local NousResearch/hermes-agent checkout")
    parser.add_argument("--check-only", action="store_true", help="Run all validators without writing files")
    args = parser.parse_args(argv)

    catalog_path = DEFAULT_CATALOG_YAML
    if not catalog_path.is_file():
        print(f"ERROR: missing catalog template at {catalog_path}", file=sys.stderr)
        return 1

    text = catalog_path.read_text(encoding="utf-8")
    target_yaml = catalog_path

    if args.sha:
        sha = args.sha.strip().lower()
        if not SHA_RE.fullmatch(sha):
            print(f"ERROR: --sha must be 40 lowercase hex characters, got {args.sha!r}", file=sys.stderr)
            return 2
        text = re.sub(r'(?m)^sha:\s*"?[0-9a-f]{40}"?\s*$', f'sha: "{sha}"', text)
        text = re.sub(
            r'(?m)^(docs_url:\s*https://github\.com/[^/]+/[^/]+/blob/)[0-9a-f]{40}(/.*)$',
            rf'\g<1>{sha}\g<2>',
            text,
        )

    if args.maintainer:
        maint = args.maintainer.strip()
        text = re.sub(r'(?m)^maintainer:\s*.+$', f'maintainer: {maint}', text)

    if args.out and not args.check_only:
        out_path = Path(args.out).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        target_yaml = out_path
        print(f"Wrote stamped catalog entry -> {out_path}")
    elif (args.sha or args.maintainer) and not args.check_only:
        catalog_path.write_text(text, encoding="utf-8")
        print(f"Updated catalog entry -> {catalog_path}")

    hermes_cli = shutil.which("hermes")
    if hermes_cli:
        rc = _run([hermes_cli, "plugins", "validate", str(BRIDGE_DIR), "--install-deps"])
        if rc != 0:
            return rc
    else:
        print("WARN: `hermes` CLI not found on PATH; skipping `hermes plugins validate`")

    hermes_repo = _find_hermes_repo(args.hermes_repo or None)
    if hermes_repo is None:
        print("INFO: no local hermes-agent checkout found; skipping upstream CI scripts")
        return 0

    py = _hermes_venv_python(hermes_repo)
    scanner = hermes_repo / "scripts" / "scan_plugin_payloads.py"
    if scanner.is_file():
        rc = _run([py, str(scanner), str(BRIDGE_DIR)])
        if rc != 0:
            return rc

    cat_validator = hermes_repo / "scripts" / "validate_plugin_catalog.py"
    if cat_validator.is_file():
        rc = _run([py, str(cat_validator), str(target_yaml)])
        if rc != 0:
            return rc

    print("ALL HERMES MARKETPLACE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
