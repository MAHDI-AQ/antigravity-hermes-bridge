# Architecture Reference

**Hermes Swarm** implements a **1:1 conversation-bound, 3-tier multi-agent architecture** uniting **Antigravity IDE** (`primary` + `coordinator`), a Python **MCP + A2A control plane** (`packages/mcp-server`), and **Hermes Agent** workers + leaf subagents (`packages/hermes-bridge`).

---

## 1. Three-Tier Topology

| Tier | Component | Role | Capabilities |
|---|---|---|---|
| **Tier 1** | User + Primary IDE Agent (`primary`) | Goal owner | Initializes session's 1:1 swarm, spawns **one** Coordinator (`invoke_subagent`), binds the verbatim `coordinatorConversationId`, displays `open_board`, handles `@primary` escalations & final delivery |
| **Tier 2a** | Coordinator Subagent (`coordinator`) | Planner & Reviewer | Publishes `set_plan`, manages topic channels, spawns/resumes/hot-swaps/kills workers, reviews diffs & tests, escalates blockers & `[done]` to `@primary`; opens in an in-pane read-only Agent Tab backed by its local Antigravity transcript (`transcript_full.jsonl` / `transcript.jsonl`) |
| **Tier 2b** | Hermes A2A Workers (`worker \| verifier \| researcher \| monitor \| narrator`) | Execution | Run asynchronously via Hermes CLI subprocesses with `HERMES_SWARM_*` env injected; talk via native `swarm_*` tools; coordinate via `@<peer>`, `@#channel`, `@all`, `@coordinator` |
| **Tier 3** | Hermes Leaf Subagents (`subagent`) | Bounded helpers | Spawned by Tier-2 workers via `delegate_task`; tracked in telemetry and History Tabs without waking Tier 1 or Tier 2 directly |

---

## 2. Core Data & Storage Layout (`<state_dir>/swarms/<swarm_id>/`)

- `meta.json` — Canonical swarm metadata (`swarmId`, `title`, `goal`, `mission`, `workspace`, `primaryConversationId`, `coordinatorConversationId`, `wakeSeq`, `archived`).
- `board.json` — Thread-locked + file-locked JSON state storing roster members, active `plan` (`{goal, tasks, threads}`), topic channels, `409` edit scope claims, and latest `MAX_BOARD_MESSAGES` (`1000`) posts.
- `board_archive.jsonl` — Append-only archive receiving messages rolled off `board.json` (both server-side in `board.py` and disk-fallback in `swarm_tools.py`). Read seamlessly via `include_archive=True` and `/board/raw`.
- `tasks.json` — Persisted A2A task records, worker config, billable token metrics, live tool activity, and subagent rollups.
- `logs/*.jsonl` — Capped (`<= 5 MiB`, rotated `.1`) structured JSONL logs (`board.jsonl`, `wakeup.jsonl`, `events.jsonl`, `bridge.jsonl`) for offline diagnostics without polluting `board.json`.

---

## 3. MCP Control Plane (`packages/mcp-server/server.py`)

Exposes exactly 7 MCP tools:
1. `swarm_catalog` — Discovers available Hermes models, providers, profiles, toolsets, and skills from live Hermes CLI/config.
2. `swarm_workspace` — `get_workdir | set_workdir | git_status | git_diff | prune_worktrees` (accepts `get | set` aliases; `git_status` / `git_diff` / `prune_worktrees` resolve `meta["workspace"]` via `swarm_id` / `conversation_id` when `workdir` is omitted).
3. `swarm_manage` — `init` (`primary_conversation_id` / `conversation_id` alias) | `title` | `bind_coordinator` (`coordinator_conversation_id` copied verbatim from `invoke_subagent` + optional `primary_conversation_id`, `model`, `provider`; validated against local Antigravity brain storage with transcript model/provider resolution fallback) | `set_plan` | `status` | `clear_finished` | `channel`.
4. `swarm_worker` — `spawn | resume | switch_model | status | diff | kill` (supports `n`, `tasks`, `fork`, `worktree=False` default; `diff` accepts optional `name` or omits `name` to aggregate all workers).
5. `swarm_board_post` — Posts messages and emits `@mention` / `@#channel` wakeups.
6. `swarm_board_read` — Reads messages (`limit=100` default, `since=0` tail, `include_archive`, `before_seq`, `search`, `full`) and manages scope claims (`claim` / `release`).
7. `swarm_wait` — Blocks reactively until matching board events arrive (Coordinator MCP caps `timeout_seconds <= 150` under the `180s` Antigravity MCP deadline; worker-side `hermes-swarm-bridge` `swarm_wait` supports up to `600s`).

---

## 4. Billable Token Economy & Context Limits

Every LLM API call in a Hermes agent resends the full active context plus receives output tokens. To reflect real token volume accurately:

- `pureInput` = uncached input tokens (`input` excluding `cacheRead`)
- `cachedInput` = prompt cache hits (`cacheRead`)
- `totalProcessed` (`total`) = `pureInput + cachedInput + cacheWrite + output`
- `cacheHitRate` = `cachedInput / (pureInput + cachedInput)`
- **Turn-Delta TPS:** On resumed turns, `prior_output_tokens` from `state.db` is subtracted so TPS measures output per active second in the *current* wake (`llmActiveMs`), eliminating inflated `500+ t/s` spikes. During streaming before final counts arrive, characters are converted (`streamed_chars // 4`) with `tpsEstimated=True` (`~t/s` in UI).
- **Context Window Resolution:** Both `runtime.py` and `hermes-agent`'s `model_metadata.py` strip `custom:` provider prefixes and inspect user `config.yaml` (`custom_providers` / `model.providers`), preventing 1M-context custom models from prematurely compacting around `256k`.

---

## 5. Process Lifecycle & Anti-Zombie Termination

On platforms where Hermes CLI delegates execution to `tui_gateway`, terminating only the CLI wrapper process triggers `ws_disconnect` while leaving the turn running inside the gateway unless interrupted explicitly.

`Runtime.cancel_task()` and `kill_worker()` enforce a 4-layer stop:
1. Send `session.interrupt` + `session.close` RPC to the local Hermes gateway for the worker's `hermesSessionId`.
2. Kill the tracked subprocess tree (`taskkill /T /F` + Win32 `TerminateProcess` / `os.killpg(SIGKILL)`).
3. Sweep orphaned launcher/helper processes matching `prompt-<task_id>`, `board_cli`, or `worktree_path`.
4. Enforce `PermissionError` -> HTTP **`403 Forbidden`** in `BoardService.post_message` and `POST /internal/board/post` so any stale worker process cannot write to the board after being killed.
