# AI Agent Installation Playbook

> Deterministic, idempotent installation workflow designed for AI coding agents (Antigravity, Hermes Agent, Claude Code, Codex, Cursor).

## Step 1 — Pre-Flight Check

Verify prerequisites cleanly without interactive prompts:

```bash
python --version   # require >= 3.10 (or python3 --version)
node --version     # require >= 20
```

## Step 2 — Preview Plan (`--dry-run`)

Inspect exact resolved OS paths before mutating any config file:

```bash
python scripts/install.py --dry-run --non-interactive --json
```

Pass custom overrides (`--mcp-dir`, `--hermes-dir`, `--antigravity-config-dir`, `--gemini-dir`) if the target environment uses non-default locations.

## Step 3 — Execute Non-Interactive Install

```bash
python scripts/install.py --non-interactive --json
```

Confirm:
- Exit code is `0`
- `"ok": true` in stdout JSON
- Every check in `"doctor.checks"` (`python`, `node`, `mcp_server`, `antigravity_plugin`, `antigravity_mcp_config`, `hermes_bridge_plugin`) reports `"ok": true`.

## Step 4 — Standalone Verification (`doctor.py`)

```bash
python scripts/doctor.py --json
```

## Step 5 — Activation Handoff Rules for Agents

- **Do NOT force-kill an active user Hermes session** (`hermes gateway restart --yes`). Run `hermes gateway restart` without `--yes`; if a live session would be interrupted, instruct the user to run `hermes gateway restart` once that session finishes.
- Instruct the user to **Reload Antigravity Window** so the newly registered `hermes-a2a` MCP server and `hermes-swarm` sidecar activate.
