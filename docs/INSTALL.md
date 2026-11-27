# Installation Guide

Install **Hermes Swarm** in under two minutes across **Windows**, **macOS**, and **Linux / WSL2**.

## Prerequisites

1. **Python `>= 3.10`** (`python3 --version` or `python --version`)
2. **Node.js `>= 20`** (`node --version` — runs the Antigravity sidecar & wakeup bridge)
3. **Antigravity IDE** installed (using `~/.gemini/antigravity` and `~/.gemini/config`)
4. **Hermes Agent (`>= 0.21.4`)** installed (uses `%LOCALAPPDATA%\hermes` on Windows or `~/.hermes` on macOS/Linux)

---

## Automatic 1-Command Install

### macOS / Linux / WSL2
```bash
git clone https://github.com/MAHDI-AQ/hermes-swarm.git
cd hermes-swarm
./scripts/install.sh
```

### Windows (PowerShell)
```powershell
git clone https://github.com/MAHDI-AQ/hermes-swarm.git
cd hermes-swarm
.\scripts\install.ps1
```

### What `install.py` Does Automatically
1. Copies `packages/mcp-server` into `~/.local/share/hermes-swarm/mcp-server` (or `$HERMES_SWARM_MCP_HOME`), preserving any existing `state/` directory on upgrades.
2. Creates `.venv` inside the MCP server directory and installs `packages/mcp-server` via `pip install -e`.
3. Copies `packages/antigravity-plugin` to `~/.gemini/config/plugins/hermes-swarm`.
4. Copies `packages/hermes-bridge` to `<HERMES_HOME>/plugins/hermes-swarm-bridge` (100% Python stdlib — zero pip packages added to Hermes).
5. Backs up `~/.gemini/antigravity/mcp_config.json` (`mcp_config.json.bak.<timestamp>`) and registers `"hermes-a2a"`.
6. Runs `scripts/doctor.py` and prints a PASS/FAIL verification report.

---

## Custom Paths & Flags

| Flag | Default | Purpose |
|---|---|---|
| `--mcp-dir <path>` | `~/.local/share/hermes-swarm/mcp-server` | Where the MCP server & its `.venv` live |
| `--antigravity-config-dir <path>` | `~/.gemini/config` | Antigravity plugins directory parent |
| `--gemini-dir <path>` | `~/.gemini/antigravity` | Location of `mcp_config.json` |
| `--hermes-dir <path>` | `%LOCALAPPDATA%\hermes` (Win) / `~/.hermes` (Unix) | Hermes Agent root (`HERMES_HOME`) |
| `--dry-run` | off | Print planned actions without touching disk |
| `--non-interactive` | off | Safe zero-prompt mode for automation & AI agents |
| `--json` | off | Structured JSON output |

---

## Alternative: Standalone `uvx` / `pipx` MCP Server

If you want to install just the Python MCP server from a local checkout:
```bash
uv tool install ./packages/mcp-server
# or
pip install -e ./packages/mcp-server
```

---

## Post-Install Activation

1. **Reload Antigravity IDE** (`Developer: Reload Window`) so Antigravity loads the `hermes-swarm` sidecar, skills, rules, and `hermes-a2a` MCP server.
2. **Restart Hermes Gateway** if running so active Hermes worker sessions load `hermes-swarm-bridge`:
   ```bash
   hermes gateway restart
   ```
3. Run a sanity check anytime:
   ```bash
   python scripts/doctor.py
   ```
