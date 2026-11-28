# Configuration Guide

Hermes Swarm works with zero manual configuration after running `scripts/install.py`. All tunables below are optional environment variables or Hermes `config.yaml` entries.

---

## MCP Server (`packages/mcp-server`)

| Variable | Default | Description |
|---|---|---|
| `HERMES_HOME` | `%LOCALAPPDATA%\hermes` (Win) / `~/.hermes` (Unix) | Hermes Agent root containing `config.yaml`, `state.db`, and `plugins/` |
| `HERMES_A2A_STATE_DIR` | `<mcp_dir>/state` | Swarm state, `registry.json`, `internal_token`, and per-swarm JSONL logs |
| `HERMES_A2A_HTTP_HOST` | `127.0.0.1` | Loopback bind address for observer & internal HTTP/SSE APIs |
| `HERMES_A2A_HTTP_PORT` | `0` (ephemeral, `9910` preferred when free) | Port for HTTP/SSE API; discovered automatically by sidecar via `registry.json` |
| `HERMES_A2A_HTTP_TOKEN` | `<stateDir>/internal_token` | Shared Bearer token protecting `/internal/*` endpoints |
| `HERMES_A2A_DEFAULT_WORKDIR` | Workspace resolved from MCP | Fallback working directory when `set_workdir` has not been called |
| `HERMES_SWARM_ROLE_POLICY` | `<ws>/.hermes-swarm/role_policy.json` -> `~/.config/hermes-swarm/role_policy.json` -> `~/.hermes-swarm.json` | Optional local JSON policy separating IDE frontier roles from Hermes execution roles (`enforcement`: `"warn"` / `"reject"` / `"off"`) and setting local `hermes_worker_defaults` |
| `HERMES_SWARM_URL_HOSTS` | `url_hosts.json` | Optional extra allowlisted hostnames inside observer link detection |

## Local Role & Model Policy (`~/.config/hermes-swarm/role_policy.json`)

Keep personal IDE tier preferences and Hermes worker model defaults external to any project repo via `~/.config/hermes-swarm/role_policy.json` (or `<workspace>/.hermes-swarm/role_policy.json` / `HERMES_SWARM_ROLE_POLICY`). Example:

```json
{
  "version": 1,
  "enforcement": "reject",
  "ide_only_roles": [
    "researcher", "research", "architect", "design",
    "planner", "plan", "consult", "consultant",
    "verifier", "verify", "reviewer", "auditor"
  ],
  "hermes_execution_roles": ["worker", "implementer", "monitor", "narrator"],
  "ide_subagent_preferences": {
    "tool": "invoke_subagent",
    "type_name": "self"
  },
  "hermes_worker_defaults": {
    "model": null,
    "provider": null
  },
  "hub_and_spoke": true
}
```

- When `enforcement` is `"reject"`, `swarm_worker(action="spawn")` blocks spawns matching `ide_only_roles` unless `allow_ide_only_role=True` is passed explicitly. Default (when no file exists) is `"warn"`.
- `swarm_manage(action="init"|"status")` and `swarm_catalog()` return the active `role_policy` so Coordinators always see local preferences at runtime.

---

## Context Window Resolution (`config.yaml`)

When using custom OpenAI-compatible providers in Hermes (`custom:<provider>`), declare `context_length` in Hermes `config.yaml` under `model` or `custom_providers` so both Hermes compaction and Swarm Telemetry display accurate `used / limit (% CTX)`:

```yaml
model:
  default: my-long-context-model
  context_length: 1048576

custom_providers:
  - name: my-provider
    models:
      my-long-context-model:
        context_length: 1048576
```

---

## Hermes Bridge (`packages/hermes-bridge`)

Workers receive these variables automatically when spawned by `packages/mcp-server`:

| Variable | Purpose |
|---|---|
| `HERMES_SWARM_ID` | Active 1:1 swarm identifier |
| `HERMES_SWARM_AGENT` | Worker's roster name on the board |
| `HERMES_SWARM_ROLE` | Role (`worker`, `verifier`, `researcher`, `monitor`, `narrator`) |
| `HERMES_SWARM_HTTP_URL` | Loopback HTTP base URL (`http://127.0.0.1:<port>`) |
| `HERMES_SWARM_TOKEN_FILE` | Path to Bearer token file (`internal_token`) |
| `HERMES_SWARM_BOARD_DIR` | Fallback directory (`<stateDir>/swarms/<swarmId>`) |
| `HERMES_SWARM_CONVERSATION_ID` | Bound primary/coordinator conversation ID |
| `HERMES_SWARM_TASK_ID` / `HERMES_SWARM_CONTEXT_ID` | Tracked A2A task and resume context IDs |

### Bridge Lifecycle Hooks
Registered automatically via `ctx.register_hook(...)` when supported by Hermes:
- `post_compaction`: Snapshot current swarm/claims/`lastReadSeq` before/after summary flush and log `logs/bridge.jsonl`.
- `pre_llm_call`: Inject compact swarm briefing back into context immediately following compaction.
- `pre_verify`: Warn once if unreleased `swarm_claim` locks remain held prior to finishing.
- `post_tool_call`: Throttled (`15s`) heartbeat update (`lastHeartbeatAt`) on `board.json`.

---

## Antigravity Sidecar (`packages/antigravity-plugin/sidecars/swarm_board/sidecar.json`)

| Env / Plugin Flag | Default | Purpose |
|---|---|---|
| `SWARM_OVERVIEW` | `${flag:enable-swarm-overview}` | Show Overview threads & workstream canvas |
| `SWARM_GRAPH_TAB` | `${flag:enable-swarm-graph-tab}` | Show `@mention` & team Hive Graph tab |
| `SWARM_RAW_TAB` | `${flag:enable-swarm-raw-tab}` | Show Raw JSON / `board_archive.jsonl` inspector tab |
| `HERMES_SWARM_SIDECAR_DIR` | plugin path | Override sidecar directory in tests/CI |
