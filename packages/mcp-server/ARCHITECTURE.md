# Hermes A2A Swarm Bridge (`hermes-a2a`) — Architecture

Package Root: `packages/mcp-server/`  
Python Environment: `.venv` (deps: `mcp`, `httpx`, `pyyaml`, `starlette`, `uvicorn`)

## 1. Module Map

| File | Responsibility |
|---|---|
| `models.py` | Canonical A2A v1.0 & Swarm v2 dataclasses and builders (`TaskState`, `Role`, `MemberState`, `MessageType`, `WorkerConfig`, `TaskView`, `build_hermes_agent_card`). |
| `role_policy.py` | Local user/workspace role-and-model policy loader (`~/.config/hermes-swarm/role_policy.json`, `<ws>/.hermes-swarm/role_policy.json`, `HERMES_SWARM_ROLE_POLICY`), separating IDE frontier roles (`invoke_subagent`) from Hermes execution workers (`worker`/`implementer`, `monitor`, `narrator`). |
| `store.py` | 1:1 session-bound Swarm v2 filesystem store (`meta.json`, `board.json`, `tasks.json`, `registry.json`), Windows-safe `atomic_file_lock`, `atomic_write_json`, path containment (`is_path_within`), `resolve_swarm_for_conversation`, and PID-only server registry (`SwarmStore`). |
| `catalog.py` | Secret-free dynamic introspection of Hermes CLI, profiles, providers, models, aliases, toolsets, skills, git status/diff, and custom A2A peers. |
| `worktree.py` | Isolated git worktree provisioning, diff inspection, removal, and pruning (`create_worktree`, `inspect_git_diff`, `prune_worktrees`). |
| `win_launcher.py` | Windows wrapper for Hermes CLI subprocesses fixing stdlib `_fallback_socketpair` ProactorEventLoop deadlocks plus hidden-window process-tree termination. |
| `a2a_http_client.py` | Outbound A2A v1.0 HTTP/JSON-RPC client for external peers. |
| `board.py` | Unified event-driven Swarm v2 `BoardService`: monotonic `seq`, `@name`/`@#channel`/`@all` extraction, channels (`create`/`subscribe`/`unsubscribe`/`list`/`archive`), scope claims (`holder`), coordinator `plan`, `pendingMentions`, Tier-3 `subagents` persistence, and zero-cost `wait_for_update`. |
| `wakeup.py` | `MentionRouter` + 500ms background disk watcher: dispatches `@<peer>`, `@#<channel>`, and `@all` wakeups, queues `pendingMentions` for busy workers, and emits `coordinator_wake` / `primary_wake` events strictly by tier. |
| `board_cli.py` | Windows-terminal-safe CLI (`post`, `read`, `channels`, `claim`, `release`, `status`, `wait`) using pre-bound `--swarm` / `--conversation-id` flags, preferring `POST /internal/board/post` with locked `board.py` fallback. |
| `runtime.py` | `SwarmRuntime` and `SwarmManager`: resolves runtimes 1:1 by `conversation_id` or explicit `swarm_id`, spawns Tier-2 Hermes workers (`worker\|verifier\|researcher\|monitor\|narrator`), parses `stream-json`, tracks Tier-3 `delegate_task` subagents, supports `tasks`/`n` sharding, `fork`, `switch_model` (preserving `contextId`), and replays `pendingMentions` on turn completion. |
| `http_server.py` | Embedded/standalone Starlette server: A2A v1.0 (`/.well-known/agent-card.json`, JSON-RPC 2.0, `/v1/*`), 1:1 session-bound read-only observer endpoints (`GET /board?conversationId=<conv>&etag=`, `GET /board/events?conversationId=<conv>` SSE, `GET /board/frame?conversationId=<conv>&path=`), and loopback Bearer-gated `POST /internal/board/post`. |
| `server.py` | MCP stdio entrypoint exposing 7 tools (`swarm_catalog`, `swarm_workspace`, `swarm_manage`, `swarm_worker`, `swarm_board_post`, `swarm_board_read`, `swarm_wait`). |

## 2. 1:1 Session Isolation & 3-Tier Wakeup Routing

1. **1:1 Conversation-Bound Swarms:** Each Antigravity conversation maps 1:1 to a single swarm (`derive_swarm_id(conversation_id=...)` -> `sanitize_swarm_id(conversation_id)`). There is no global `activeSwarmId`, no `GET /board/swarms` switcher, and no workspace fallback when resolving a conversation. Parallel sessions in the same workspace stay strictly isolated.
2. **Tier 1 (`primary` — User + Primary Antigravity session):** Initializes the conversation's swarm (`swarm_manage(action="init", primary_conversation_id=...)`), surfaces `open_board`, spawns one Tier-2 Coordinator subagent via `invoke_subagent`, binds `coordinatorConversationId`, and waits for `@primary` escalations.
3. **Tier 2 (`coordinator` + Hermes A2A workers):** Coordinator plans lanes (`set_plan`), manages channels/claims, and spawns/resumes/switches/kills Hermes workers (`swarm_worker`). Workers coordinate on one unified board (`board.json` + `board_cli.py`). Worker posts to `@coordinator` or `question`/`escalation`/`handoff`/`[done]`/`[stall]` emit `coordinator_wake` only; Coordinator escalations to `@primary` emit `primary_wake`.
4. **Tier 3 (`subagent` — Hermes `delegate_task` children):** Spawned by Tier-2 Hermes workers; tracked read-only under `board.json.subagents` and displayed nested under their parent worker in the observer UI. Tier-3 records never post to the board and never emit wake events.

## 3. Hermes Gateway Pre-Bound CLI Invariant

`hermes.exe chat` talks over WebSocket RPC to the long-running `hermes gateway` daemon whose shell `terminal` tool does not inherit env vars from thin clients. `runtime.py` therefore embeds literal pre-bound `board_cli.py` commands (`--board-dir`, `--swarm`, `--agent`, `--http-url`, `--token-file`) into worker preambles without exposing bearer tokens in prompt text.

## 4. Verification

```bash
python -m py_compile *.py
python -m unittest discover -s tests -v
```
