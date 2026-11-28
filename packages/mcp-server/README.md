# Hermes A2A Swarm Bridge (`hermes-a2a`)

A clean 3-tier **Agent2Agent (A2A) v1.0** bridge, unified event-driven Swarm Board, and 1:1 session-bound read-only observer backend connecting **Antigravity** (`primary` -> `coordinator` + IDE frontier subagents) with local **Hermes Agent** execution workers (`worker`, `implementer`, `monitor`, `narrator`).

## MCP Tools (7)

1. `swarm_catalog(profile?)` — Dynamic discovery of Hermes models, providers, profiles, toolsets, skills, and A2A peers.
2. `swarm_workspace(action, workdir?, swarm_id?, conversation_id?)` — Active workspace resolution (`get|set|prune_worktrees|git_status|git_diff`).
3. `swarm_manage(action, ...)` — 1:1 session-bound swarm lifecycle (`init|title|bind_coordinator|set_plan|status|clear_finished|channel`), returning `open_board` (`create` and `rename` remain supported as idempotent aliases).
4. `swarm_worker(action, ...)` — Tier-2 Hermes worker lifecycle (`spawn|resume|switch_model|status|diff|kill`) with `tasks`/`n` sharding, `fork`, `worktree`, `channels`, `model`, `provider`, and `reasoning`.
5. `swarm_board_post(...)` — Post messages to the Swarm Board with automatic `@peer`, `@#channel`, and `@all` wakeups.
6. `swarm_board_read(...)` — Query board messages by `since`/`channel`/`mention` and manage scope claims (`claim|release|list`).
7. `swarm_wait(...)` — Block zero-cost until matching board updates arrive after `since`.

## Observer HTTP & SSE Endpoints (1:1 Session-Bound)

- `GET /board?conversationId=<conv>&etag=<etag>` — Read-only v2 snapshot for that conversation's single swarm (returns `{ "conversationId": conv, "swarm": null, "member": null }` when unbound, or `{ "unchanged": true, "swarm": id, "etag": ... }` on ETag match).
- `GET /board/events?conversationId=<conv>` — SSE stream emitting `snapshot`, `update`, `coordinator_wake`, and `primary_wake` (stays open cleanly if connected before `swarm_manage(action="init")` and emits `snapshot` once created; also accepts `?swarm=<id>` for internal/CLI use).
- `GET /board/frame?conversationId=<conv>&path=<path>` — Image frame delivery strictly contained within `<stateDir>/swarms/<id>/` and `<appData>/brain/` (`<= 8 MiB`, image extensions only, zero `workspace` query param).
- `POST /internal/board/post` — Loopback + Bearer-gated internal hook used by `board_cli.py` (accepts `swarmId` / `conversationId`).
- A2A v1.0 endpoints: `GET /.well-known/agent-card.json`, `POST /`, `POST /v1/message:send`, `GET /v1/tasks*`.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for module responsibilities and invariants.
