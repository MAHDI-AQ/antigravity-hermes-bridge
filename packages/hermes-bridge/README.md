# hermes-swarm-bridge

Antigravity <-> Hermes interoperability plugin exposing first-class Swarm Board tools and compaction-safe lifecycle hooks for Hermes Agent workers collaborating with Antigravity IDE sessions.

> **Companion Requirement:** This Hermes-side plugin pairs with **Antigravity IDE**, the companion Antigravity plugin (`packages/antigravity-plugin`), and the local MCP + HTTP bridge (`packages/mcp-server`) from [antigravity-hermes-bridge](https://github.com/MAHDI-AQ/antigravity-hermes-bridge). Outside a swarm session (when `HERMES_SWARM_ID` is unset), tools register cleanly for gateway/per-turn routing while hooks stay dormant.

## Disclosure (Privacy, Network, Storage & Execution)

- **Network:** Communicates strictly over loopback (`127.0.0.1`) with the local Swarm HTTP server configured via `HERMES_SWARM_HTTP_URL`, using the per-swarm ephemeral Bearer token from `HERMES_SWARM_HTTP_TOKEN` or `HERMES_SWARM_TOKEN_FILE`. Makes zero external internet requests and sends zero prompts or telemetry off-machine.
- **Credentials:** Never reads `.env` files, Hermes `auth.json`, browser profiles, keychains, or any third-party credential store.
- **Local Storage:** When HTTP is unavailable or unconfigured, falls back atomically using `board.lock` and JSON replacement on `board.json` and `board_archive.jsonl` inside `HERMES_SWARM_BOARD_DIR`. Best-effort structured JSONL diagnostics rotate at `1 MiB` inside `<swarm_dir>/logs/bridge.jsonl`.
- **Dependencies & Execution:** Pure Python standard library (`zero` pip dependencies, `zero` compiled binaries, `zero` self-updating code). Does not spawn background daemons, modify Hermes core modules at runtime, or auto-approve/bypass Hermes permission gates (`swarm_wait` sleeps in-process via `threading.Event.wait`).

## Registration & Activation Gate

`register(ctx)` always registers the 7 native `swarm_*` tools so per-turn `_context` / `session_info` routing works across both dedicated workers and gateway turns:

- `swarm_post(summary, details="", kind="post", channel="", files=None, reply_to=None, to=None, thread_id=None)`
- `swarm_read(since=0, channel="", limit=100, seqs=None, status=False, mention=None, sender=None, kind=None, mark_read_by=None, include_archive=False, before_seq=None, search=None, full=False)`
- `swarm_claim(scope, notes="")`
- `swarm_release(scope="")`
- `swarm_status()`
- `swarm_wait(since=0, mentions_only=True, timeout_seconds=120)`
- `swarm_channel(action="list", channel="", topic="", members=None)`

When `HERMES_SWARM_ID` is set, `register(ctx)` also registers:
- The `hermes-swarm-bridge.worker` system prompt section (`max_chars=700`) informing the worker of its `agent`, `role`, `swarm_id`, active channel/claims, and `lastReadSeq`.
- Optional lifecycle hooks (when `ctx.register_hook` is available):
  - `post_compaction` / `pre_llm_call`: preserves and re-injects the active swarm briefing (`swarm_id`, `agent`, `channel`, active `claims`, `lastReadSeq`) across context compactions.
  - `pre_verify`: warns once per turn if active scope claims remain unreleased.
  - `post_tool_call`: updates throttled `lastHeartbeatAt` on disk.

## Read, Archive & Logging

- `swarm_read` defaults to `limit=100` (max `500`) and supports `include_archive`, `before_seq`, `search`, and `full` on both HTTP (`POST /internal/board/read`) and disk fallback.
- Disk-fallback `swarm_post` and `swarm_channel` append messages trimmed past `MAX_BOARD_MESSAGES` (`1000`) to `board_archive.jsonl` in the swarm directory (zero silent loss), which `swarm_read(..., include_archive=True)` merges cleanly by `seq`.
- Structured JSONL logs for tool calls, HTTP 4xx/5xx retries, disk fallbacks, and compaction events are written best-effort (capped at 1 MiB with `.1` rotation) to `<swarm_dir>/logs/bridge.jsonl`.

## Environment Contract

| Variable | Purpose |
|---|---|
| `HERMES_SWARM_ID` | Active swarm identifier (gates prompt section & hooks) |
| `HERMES_SWARM_AGENT` | Worker name on the board |
| `HERMES_SWARM_ROLE` | Worker role (defaults to `worker`) |
| `HERMES_SWARM_HTTP_URL` | Base URL for Swarm HTTP server (e.g. `http://127.0.0.1:9910`) |
| `HERMES_SWARM_HTTP_TOKEN` | Explicit Bearer token override |
| `HERMES_SWARM_TOKEN_FILE` | Path to Bearer token file (used when `HERMES_SWARM_HTTP_TOKEN` is unset) |
| `HERMES_SWARM_BOARD_DIR` | Folder holding `board.json`, `board_archive.jsonl`, and `board.lock` for disk fallback |
| `HERMES_SWARM_CONVERSATION_ID` | Bound conversation ID for `GET /board?conversationId=...` |
| `HERMES_SWARM_TASK_ID` | Optional active task ID metadata |
| `HERMES_SWARM_CONTEXT_ID` | Optional active context ID metadata |

## Transport & Dependencies

- Uses **Python stdlib only** (`urllib.request`, `json`, `os`, `pathlib`, `re`, `threading`, `time`).
- `swarm_post`, `swarm_read`, `swarm_claim`, `swarm_release`, `swarm_status`, and `swarm_channel` try the HTTP endpoint first and fall back atomically to `board.lock` + `board.json` replacement on disk (4xx client rejections except 404 do not silently fall back).
- `swarm_wait` runs purely in-process via `threading.Event().wait(...)` without spawning child shell processes.
