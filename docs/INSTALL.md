# Installation Guide

Install **Antigravity ↔ Hermes Bridge** (`hermes-swarm`) in under two minutes across **Windows**, **macOS**, and **Linux / WSL2**.

## Prerequisites

1. **Python `>= 3.10`** (`python3 --version` or `python --version`)
2. **Node.js `>= 20`** (`node --version` — runs the Antigravity sidecar & wakeup bridge)
3. **Antigravity IDE** installed (using `~/.gemini/antigravity` and `~/.gemini/config`)
4. **Hermes Agent (`>= 0.21.4`)** installed (uses `%LOCALAPPDATA%\hermes` on Windows or `~/.hermes` on macOS/Linux)

---

## Automatic 1-Command Install (Recommended Full Stack)

### macOS / Linux / WSL2
```bash
git clone https://github.com/MAHDI-AQ/antigravity-hermes-bridge.git
cd antigravity-hermes-bridge
./scripts/install.sh
```

### Windows (PowerShell)
```powershell
git clone https://github.com/MAHDI-AQ/antigravity-hermes-bridge.git
cd antigravity-hermes-bridge
.\scripts\install.ps1
```

## Preferred Native Ecosystem Commands

If you prefer installing each side through its own CLI after cloning:

- **Antigravity IDE side:**
  ```bash
  agy plugin install ./packages/antigravity-plugin
  ```
  *(Still register `packages/mcp-server` in `~/.gemini/config/mcp_config.json`, or let `./scripts/install.sh` / `install.py` set up the MCP `.venv` and config for you.)*
- **Hermes Agent side:**
  ```bash
  hermes plugins install https://github.com/MAHDI-AQ/antigravity-hermes-bridge --yes-deps --enable
  ```
  *(Note: installing the Hermes plugin alone adds `swarm_*` tools and lifecycle hooks inside Hermes; pairing it with Antigravity IDE + `packages/mcp-server` from this repo provides the full bridge.)*

### What `install.py` Does Automatically
1. Copies `packages/mcp-server` into `~/.local/share/hermes-swarm/mcp-server` (or `$HERMES_SWARM_MCP_HOME`), preserving any existing `state/` directory on upgrades.
2. Creates `.venv` inside the MCP server directory and installs `packages/mcp-server` via `pip install -e`.
3. Copies `packages/antigravity-plugin` to `~/.gemini/config/plugins/hermes-swarm`.
4. Copies `packages/hermes-bridge` to `<HERMES_HOME>/plugins/hermes-swarm-bridge` (100% Python stdlib — zero pip packages added to Hermes).
5. Backs up `~/.gemini/config/mcp_config.json` (`mcp_config.json.bak.<timestamp>`, only when the entry changes) and registers `"hermes-a2a"` with `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`.
6. Runs `scripts/doctor.py` and prints a PASS/FAIL verification report.

---

## Custom Paths & Flags

| Flag | Default | Purpose |
|---|---|---|
| `--mcp-dir <path>` | `~/.local/share/hermes-swarm/mcp-server` | Where the MCP server & its `.venv` live |
| `--antigravity-config-dir <path>` | `~/.gemini/config` | Antigravity config & plugins root (`mcp_config.json` and `plugins/`) |
| `--gemini-dir <path>` | `~/.gemini/antigravity` | Legacy Antigravity root (derives config dir when `--antigravity-config-dir` is omitted; legacy fallback in `doctor.py`) |
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
