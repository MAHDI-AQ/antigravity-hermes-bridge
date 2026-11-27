# Contributing to Hermes Swarm

Thank you for helping improve Hermes Swarm! Both human contributors and AI coding agents are welcome — see [AGENTS.md](AGENTS.md) for machine-readable conventions and test commands.

## Repository Layout

- `packages/mcp-server/` — Python MCP server, HTTP/SSE observer API, A2A worker runtime, SQLite read-only session history reader, and browser `board_ui/`.
- `packages/antigravity-plugin/` — Antigravity IDE plugin manifest, Web UI sidecar + wakeup bridge, standing rules, and skills.
- `packages/hermes-bridge/` — Zero-dependency stdlib-only Hermes plugin exposing native `swarm_*` tools and lifecycle hooks.
- `scripts/` — Cross-platform installer (`install.py`, `install.ps1`, `install.sh`), `doctor.py`, and `check_privacy.py`.
- `docs/` — Human and agent installation, architecture, configuration, and UI documentation.

## Development Setup

```bash
# 1. Create virtualenv for mcp-server
cd packages/mcp-server
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows:     .venv\Scripts\activate
pip install -e ".[dev]"

# 2. Run all verification gates from repo root
python scripts/check_privacy.py
python -m unittest discover -s packages/hermes-bridge/tests -p "test_*.py" -v
python -m unittest discover -s packages/mcp-server/tests -p "test_hermes_a2a.py" -v
node --test --test-timeout=30000 packages/mcp-server/tests/test_sidecar.mjs
```

## Pull Request Checklist

- [ ] `python scripts/check_privacy.py` exits `0` (zero personal info, hardcoded home paths, private IPs, or secrets).
- [ ] `packages/hermes-bridge` stays **100% Python stdlib-only** (`test_manifest_and_stdlib_only` passes).
- [ ] All unit and sidecar tests pass with bounded timeouts (`<= 90s`).
- [ ] Documentation (`docs/`, `README.md`, `AGENTS.md`, `llms.txt`) is updated if behavior, flags, or tools changed.
- [ ] Single-purpose commit message using Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
