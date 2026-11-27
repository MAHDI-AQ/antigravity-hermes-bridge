# AGENTS.md — Agent Guide for `hermes-swarm`

> Universal context and verification instructions for AI coding agents working in or installing `hermes-swarm`.

## Repository Structure

```text
packages/
├── mcp-server/           # Python MCP server (7 tools), HTTP/SSE observer API, A2A runtime, board_ui/, tests/
├── antigravity-plugin/   # Antigravity IDE plugin: plugin.json, sidecars/swarm_board/, rules/AGENTS.md, skills/
└── hermes-bridge/        # Stdlib-only Hermes Agent plugin: swarm_* native tools & compaction/verify hooks
scripts/
├── install.py            # Cross-platform installer (Win/macOS/Linux; supports --non-interactive --json --dry-run)
├── doctor.py             # Post-install checker (--json supported)
└── check_privacy.py      # Privacy, hardcoded-path, secret, and forbidden-term gate
docs/
├── INSTALL.md            # Human setup guide
├── AGENT_INSTALL.md      # Deterministic agent installation playbook
├── ARCHITECTURE.md       # Protocol, endpoints, 3-tier topology, token economy
├── CONFIGURATION.md      # Env vars, ports, model catalog & limits
└── UI_GUIDE.md           # Board UI, Sticky Header, Token Telemetry & Live Agent History Tabs
```

## Exact Build, Test & Verification Commands

Run all checks from the repository root with bounded timeouts:

1. **Privacy & portability gate (mandatory before any commit):**
   ```bash
   python scripts/check_privacy.py
   ```
2. **Hermes Bridge tests (stdlib-only, zero pip deps):**
   ```bash
   python -m unittest discover -s packages/hermes-bridge/tests -p "test_*.py" -v
   ```
3. **MCP server Python tests (`test_hermes_a2a.py`):**
   ```bash
   # Linux/macOS:
   PYTHONPATH=packages/mcp-server packages/mcp-server/.venv/bin/python -m unittest discover -s packages/mcp-server/tests -p "test_hermes_a2a.py" -v
   # Windows PowerShell:
   $env:PYTHONPATH = "packages/mcp-server"; packages\mcp-server\.venv\Scripts\python.exe -m unittest discover -s packages/mcp-server/tests -p "test_hermes_a2a.py" -v
   ```
4. **Sidecar & UI syntax + Node tests:**
   ```bash
   node --check packages/antigravity-plugin/sidecars/swarm_board/main.mjs
   node --check packages/antigravity-plugin/sidecars/swarm_board/wakeup_bridge.mjs
   node --check packages/mcp-server/board_ui/app.js
   node --check packages/mcp-server/board_ui/hive.js
   node --test --test-timeout=30000 packages/mcp-server/tests/test_sidecar.mjs
   ```

## Architectural Invariants

1. **Exact 7-Tool MCP Surface (`packages/mcp-server/server.py`):** Do not add extra top-level MCP tool names without updating tests and documentation (`swarm_catalog`, `swarm_workspace`, `swarm_manage`, `swarm_worker`, `swarm_board_post`, `swarm_board_read`, `swarm_wait`).
2. **Zero External Dependencies in `packages/hermes-bridge`:** Keep `packages/hermes-bridge/` 100% Python standard library so any Hermes Agent install loads it cleanly without `pip install` (`test_manifest_and_stdlib_only` enforces this).
3. **1:1 Session-Bound Scopes:** Every primary conversation maps 1:1 to a single swarm (`swarmId`), enforced across `http_server.py` and `sidecars/swarm_board/main.mjs`. Never expose a multi-swarm switcher in the Observer UI.
4. **Loopback-Only Control Plane:** Bind HTTP on `127.0.0.1` only, require Bearer `internal_token` for `/internal/*`, and keep `state.db` access strictly read-only (`?mode=ro`).
5. **Process Hygiene & Anti-Zombie Worker Termination:** Always keep `interrupt_hermes_gateway_session` + `prompt-<task_id>` orphan sweeping + `403` killed-member post rejection aligned across `win_launcher.py`, `runtime.py`, `board.py`, and `http_server.py`.

## Installing via Agent

Follow [docs/AGENT_INSTALL.md](docs/AGENT_INSTALL.md) using `python scripts/install.py --non-interactive --json` and confirm `python scripts/doctor.py --json` reports `"ok": true`.
