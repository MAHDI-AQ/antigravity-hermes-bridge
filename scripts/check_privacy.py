#!/usr/bin/env python3
"""Repository privacy, portability, and cleanliness checker for Hermes Swarm."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "state",
    "dist",
    "build",
    ".pytest_cache",
}

TEXT_SUFFIXES = {
    ".py",
    ".mjs",
    ".js",
    ".json",
    ".md",
    ".txt",
    ".css",
    ".html",
    ".svg",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
    ".ps1",
}

# Patterns that must never appear in the public open-source tree.
# Note: URLs containing the public repo owner slug (github.com/MAHDI-AQ/hermes-swarm)
# are explicitly stripped before checking personal-identifier patterns.
ALLOWLIST_SUBSTRINGS = (
    "github.com/MAHDI-AQ/hermes-swarm",
    "https://github.com/MAHDI-AQ/hermes-swarm",
    "github.com/MAHDI-AQ/antigravity-hermes-bridge",
    "https://github.com/MAHDI-AQ/antigravity-hermes-bridge",
)

FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("personal_name", re.compile(r"\b(mahdi|almiskin)\b", re.IGNORECASE)),
    ("company_name", re.compile(r"\bsaliya\b", re.IGNORECASE)),
    ("private_project", re.compile(r"\b(local-agent-lab|personal-hermes|strata-agx)\b", re.IGNORECASE)),
    ("private_host_or_user", re.compile(r"\b(ai-server|mhd67)\b", re.IGNORECASE)),
    ("private_model_or_provider", re.compile(r"\b(hy4-flash-free|opencode-go)\b", re.IGNORECASE)),
    ("private_project_helper", re.compile(r"(?:build_swarm_brief|@neon\b)", re.IGNORECASE)),
    ("windows_user_path", re.compile(r"[A-Za-z]:[/\\]Users[/\\][A-Za-z0-9._-]+")),
    ("posix_home_path", re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+/")),
    ("private_lan_ip", re.compile(r"\b192\.168\.\d+\.\d+\b")),
    ("secret_token_literal", re.compile(r"(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("forbidden_role_term", re.compile(r"\b(owner|tier1|cto|company|senior|junior|queen)\b", re.IGNORECASE)),
]

# Files where role-vocabulary words like "owner" or "company" are legitimate prose (not UI/agent role hierarchy).
ROLE_TERM_EXEMPT_PREFIXES = (
    ".github/",
    "docs/",
    "scripts/",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "AGENTS.md",
    "SECURITY.md",
    "llms.txt",
)


def scan_repo(root: Path = REPO_ROOT) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    self_path = Path(__file__).resolve()
    for p in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if not p.is_file() or p.resolve() == self_path:
            continue
        if p.suffix.lower() not in TEXT_SUFFIXES or p.stat().st_size > 2_000_000:
            continue
        rel = p.relative_to(root).as_posix()
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            findings.append({"file": rel, "line": 0, "rule": "read_error", "match": str(exc)})
            continue

        skip_role_term = rel in ROLE_TERM_EXEMPT_PREFIXES or rel.startswith(ROLE_TERM_EXEMPT_PREFIXES)
        for lineno, raw_line in enumerate(lines, 1):
            line = raw_line
            for allowed in ALLOWLIST_SUBSTRINGS:
                line = line.replace(allowed, "")
            # Ignore XML namespace URLs like http://www.w3.org/2000/svg
            line = line.replace("http://www.w3.org/", "http://www.w3.invalid/")
            for rule_name, regex in FORBIDDEN_PATTERNS:
                if rule_name == "forbidden_role_term" and (
                    skip_role_term or rel.endswith("README.md") or "/tests/" in rel or rel.startswith("tests/")
                ):
                    continue
                m = regex.search(line)
                if m:
                    findings.append({
                        "file": rel,
                        "line": lineno,
                        "rule": rule_name,
                        "match": m.group(0),
                    })
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Hermes Swarm repository for PII, hardcoded paths, or secrets.")
    parser.add_argument("--root", default=str(REPO_ROOT), help="Repository root to scan")
    args = parser.parse_args()
    findings = scan_repo(Path(args.root).resolve())
    if findings:
        for item in findings:
            print(f"[FAIL] {item['file']}:{item['line']} [{item['rule']}] matched {item['match']!r}")
        print(f"\nPrivacy/cleanliness check FAILED with {len(findings)} finding(s).")
        return 1
    print("Privacy/cleanliness check PASSED (0 findings).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
