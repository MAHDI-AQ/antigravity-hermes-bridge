---
name: hermes-swarm
description: >-
  Run a 1:1 session-bound, 3-tier AI engineering swarm where Primary Antigravity
  delegates to a dedicated Tier-2 Coordinator subagent and Hermes A2A workers
  collaborating on a unified event-driven board. Use when work splits into
  independent parallel lanes, research threads, or independent verification.
disable-model-invocation: true
metadata:
  icon: hub
  logo: logo.svg
---

# 1:1 Session-Bound 3-Tier Swarm Architecture

Every Antigravity conversation owns **one** isolated swarm (`1 Antigravity conversation = 1 swarm`, with zero swarm switcher dropdown and zero `select`/`archive` switching). A Hermes swarm separates high-level direction from day-to-day coordination and leaf execution:

- **Tier 1 — User + Primary Antigravity (`primary`):** Talks to the User and the Tier-2 `coordinator`. Resolves workspace, initializes the conversation's swarm (`swarm_manage(action="init")`), spawns **one** dedicated Coordinator subagent via `invoke_subagent`, binds `coordinatorConversationId`, surfaces `open_board`, and waits for `@primary` escalations or final `[done]`.
- **Tier 2 — Coordinator Subagent (`coordinator`) + Hermes A2A Workers (`worker | verifier | researcher | monitor | narrator`):**
  - **Coordinator:** Antigravity subagent bound to `coordinatorConversationId`. Publishes `set_plan`, manages channels, spawns/resumes/switches/kills Hermes A2A workers (`swarm_worker`), locks scopes (`claim`/`release`), reviews `diff` + tests, and escalates to `@primary` only for user decisions or final `[done]`.
  - **Hermes A2A Workers:** Spawned via `swarm_worker` (`hermes_win_launcher`) with `HERMES_SWARM_*` environment variables injected automatically so the `hermes-swarm-bridge` plugin exposes native `swarm_*` tools (`swarm_post`, `swarm_read`, `swarm_claim`, `swarm_release`, `swarm_status`, `swarm_wait`, `swarm_channel`, with pre-bound `board_cli.py` kept only as fallback). Collaborate on one unified board (`board.json`) via `@<peer>`, `@#channel`, `@all`, or `@coordinator`. Mentioning an idle peer automatically resumes its `context_id`.
- **Tier 3 — Hermes Leaf Subagents (`subagent`):** Spawned exclusively by Tier-2 Hermes workers via native Hermes `delegate_task`. Never post to the board and never wake Tier 1 or Tier 2 directly.

| Tool | Purpose |
|---|---|
| `invoke_subagent` | **Tier 1 only:** Spawn the single Tier-2 Coordinator subagent (`TypeName="self"`, `Role="Swarm Coordinator"`) |
| `swarm_catalog` | Discover live Hermes models, providers, profiles, toolsets, and skills (`profile?`) |
| `swarm_workspace` | `get_workdir \| set_workdir \| git_status \| git_diff \| prune_worktrees` (also accepts `get \| set`; `git_status`/`git_diff`/`prune_worktrees` resolve `meta["workspace"]` via `swarm_id`/`conversation_id` when `workdir` is omitted) |
| `swarm_manage` | `init` (`primary_conversation_id` / `conversation_id`, `title`, `mission`) \| `title` \| `bind_coordinator` (`coordinator_conversation_id`, `primary_conversation_id?`, `model?`, `provider?`) \| `set_plan` \| `status` \| `clear_finished` \| `channel` (1:1 conversation-bound; zero `select` or `archive`); returns `open_board`, `observer_ui_url` (`http://127.0.0.1:<port>/board/ui?conversationId=<id>`), and live `telemetry` rollup + per-worker/subagent tools/tokens/TPS on `status` |
| `swarm_worker` | `spawn \| resume \| switch_model \| status \| diff \| kill` (`role`, `tasks`, `n`, `fork`, `worktree`, `channels`, `model`, `provider`, `reasoning`; `diff` accepts optional `name` for one worker/task ID or omits `name` to aggregate all workers); `status` exposes live worker & Tier-3 subagent `steps`, `toolCalls`, `currentTool`, `recentTools`, `tokens`, `tps`, `elapsedMs`, `costUsd` |
| `swarm_board_post` | Post to the board (`channel?`, `sender`, `kind`, `summary`, `details?`, `files?`) with automatic `@mention` / `@#channel` wakeup |
| `swarm_board_read` | Read board posts (`since?`, `seqs?`, `channel?`, `limit?`, `status?`) and manage scope locks (`scope_action="claim"\|"release"`, `scope?`, `holder?`) |
| `swarm_wait` | Block zero-cost until new board activity arrives (`since`, `mentions_only?`, `timeout_seconds?` — keep `<= 150` on Coordinator MCP due to the `180s` Antigravity client deadline; worker `hermes-swarm-bridge` `swarm_wait` supports up to `600s`) |

## Do not do the work

- **Primary (Tier 1):** Never spawn Hermes workers directly, never police worker chatter, and never write multi-file code inline. Spawn one Coordinator subagent, bind it, surface `open_board`, and wait.
- **Coordinator (Tier 2):** Never write multi-file code inline while workers wait. Split work, publish `set_plan`, review diffs and test outputs, answer `@coordinator` posts, and escalate to `@primary` (`kind="escalation"`, `@primary`) only for real user decisions or verified `[done]`.

## Model & role choice

1. **Discover first:** Call `swarm_catalog()` before spawning workers. Never hardcode or guess model slugs.
2. **Match model to role (Hybrid Antigravity + Hermes Split):**
   - **Hermes A2A workers (`swarm_worker`):** Use for heavy SSH, build, profiling, and benchmark loops (default or `swarm_catalog()`-selected model). Leave `toolsets` unrestricted on coding workers so Tier-3 `delegate_task` remains available.
   - **Antigravity Gemini specialists (`invoke_subagent`, `TypeName="self"` read-only):** When a project protocol routes History/Docs/Web Research, Architecture/Design, Neon Consultants, or Adversarial Verification to Antigravity Gemini, Coordinator spawns them via `invoke_subagent(TypeName="self", ...)` (NOT `TypeName="research"`, which lacks `search_web` & `hermes-a2a`), passes compact subsystem briefs (`build_swarm_brief.py` if present) + `#seq` ranges, and enforces **Hub-and-Spoke addressing** (Hermes workers `@coordinator` only, never `@neon`/`@verifier` directly since `wakeup_bridge` only wakes `primary` and `coordinator`).
3. **Never kill to swap models on quota:** Use `swarm_worker(action="switch_model", ...)` to preserve warm `context_id` across quota or capacity errors.

## Start it (Tier 1 -> Tier 2 Handoff)

1. **Primary initializes and binds the session's 1:1 swarm:**
   - Verify workspace via `swarm_workspace(action="get_workdir")` (alias `get`; use `set_workdir` / `set` if needed) and resolve the project's Obsidian vault path from the workspace's `AGENTS.md` if declared.
   - Call `swarm_manage(action="init", primary_conversation_id="<primary_conv_id>", title="<concise title>", mission="<one-line goal>")` (`conversation_id` is also accepted as an alias for `primary_conversation_id`). Note the returned `swarm_id`, `open_board`, and `observer_ui_url` (`http://127.0.0.1:<port>/board/ui?conversationId=<id>`). For Playwright/browser inspection, use `observer_ui_url` directly from `swarm_manage(init|status)` — `SidecarApp` has no TCP port, so never shell-probe for a sidecar port on Hop E.
   - Spawn one Coordinator via `invoke_subagent(TypeName="self", Role="Swarm Coordinator", Prompt=...)`, passing `primary_conversation_id` (`conversation_id`), `swarm_id`, `workdir`, the active project's Obsidian vault path (if any), user goal, constraints, and instructions to act as `@coordinator` (never `bind_coordinator` from inside the subagent if Primary binds it below).
   - **Verbatim `conversationId` rule:** Copy and pass the **exact, verbatim `conversationId`** returned by `invoke_subagent` — **never hand-type, reconstruct, or splice UUID segments** from `primary_conversation_id` (bind-time validation rejects nonexistent local brain UUIDs, and spliced IDs break wakeups and `@coordinator` tab history).
   - Immediately bind the returned subagent conversation ID:
     `swarm_manage(action="bind_coordinator", primary_conversation_id="<primary_conv_id>", coordinator_conversation_id="<subagent_conv_id>", model="<optional_model>", provider="<optional_provider>")` (passing `primary_conversation_id` keeps `conversation_id` unambiguous; `model` and `provider` are optional and fall back to resolving from the Coordinator's local transcript).
   - End the swarm-creation turn with `open_board` on its own line (never invent a manual board link or command) and wait for `@primary` escalations.

2. **Coordinator launches workers cleanly:**
   - Size leanly: one worker per independent file/module/question, plus one `verifier` per 2–4 authors.
   - When the active project declares an Obsidian vault, read `Handoffs/current.md` and relevant entries in `Lessons/lessons.md` / `Decisions/` before publishing `set_plan`, and include relevant vault pointers in worker briefs when workers need project context.
   - Publish `swarm_manage(action="set_plan", ...)` per `hermes-swarm-pane` (use `swarm_manage(action="title", title=...)` to rename the session swarm if needed).
   - Spawn workers via `swarm_worker(action="spawn", name=..., role=..., worktree=False, channels=...)` using strictly disjoint file/directory ownership + `claim_scope` / `release_scope` `409` locks in a normal repo on the active branch (`worktree=True` only when explicitly requested). Use `tasks` or `n` for sharded worker pools (`<name>-1`..`<name>-k`), or `fork` when branching from an existing worker's warm context.
   - **The brief is everything the worker sees:** state 1) assigned files, 2) untouched files (verifiers report and authors fix—a verifier never edits an author's files, and two workers never edit the same file), 3) exact verification commands, 4) cite posts by `#seq`, 5) use native `hermes-swarm-bridge` tools FIRST (`swarm_post`, `swarm_read`, `swarm_claim`, `swarm_release`, `swarm_status`, `swarm_wait`, `swarm_channel`; call `swarm_post` directly rather than wrapping it inside batched `tool_call(calls=...)` JSON strings, never send live diagnostic/probe posts to the board, only include `[done]` in `summary` once the final verified report is ready, and use pre-bound `board_cli.py` only as fallback; `@coordinator` for questions/handoffs, `@<peer>` to wake a peer), and 6) pass any result image or artifact paths to `@coordinator` via `files`/summary so Coordinator verifies and hands them to `@primary` for chat delivery (never render deliverable images inside Board Overview).
   - **Live telemetry & visibility:** Both `swarm_manage(action="status")` and `swarm_worker(action="status")` return the swarm `telemetry` rollup (`activeWorkers`, `waitingWorkers`, `killedWorkers`, `subagentCount`, `totalSteps`, `totalToolCalls`, `totalTokens`, `avgTps`, `liveTps`, `totalCostUsd`) plus per-worker and per-Tier-3-subagent `steps`, `toolCalls`, `currentTool`, `recentTools`, `tokens`, `tps`, and `elapsedMs`, matching the Observer UI.
   - **Optional role workers:** `monitor` (`role="monitor"`, `hermes-swarm-monitor`) for long async runs; `narrator` (`role="narrator"`, `hermes-swarm-narrator`) only when explicitly requested by the user (`narrator` writes `view.json`/`view.html` directly and never posts on the board unless explicitly escalated).

## Keep it going (Coordinator Loop)

1. **Wait reactively:** Call `swarm_wait(since=LAST, timeout_seconds=150)` (keep `<= 150s` because the Antigravity MCP client deadline is `180s`) instead of polling in a loop. Pass the highest `seq` you have read as `since`, never the board head. In host-local mode, `wakeup_bridge` runs from sidecar boot—even when the UI pane is closed—and automatically wakes local `coordinatorConversationId` and `primaryConversationId` across new conversations via `app.sendMessage`, complementing `swarm_wait`.
   - **No Hop E/F double-delivery:** When `wakeup_bridge` is running, do NOT send BOTH `swarm_board_post(@primary/@coordinator)` AND a manual `send_message` on the same hop—use the board `@mention` + `swarm_wait` (or wait for the wakeup bridge) to avoid duplicate delivery.
   - **Internal wake / health probe (never guess ports):** `http://127.0.0.1:9910` (or the port returned in `observer_ui_url`) is `hermes-a2a-mcp` (NOT the sidecar web server). Its `internal_token` lives at `<stateDir>/internal_token` (for example `~/.local/share/hermes-swarm/mcp-server/state/internal_token`), the auth header is `Authorization: Bearer <token>`, and the wake health probe is `GET /internal/wake/swarms`.
2. **Coordinate by `#seq`:** Refer to earlier posts as `#4` instead of copy-pasting reports. Mentioning `@<worker>` automatically wakes or resumes that worker.
3. **Escalate to Primary only when required:** When user input is required or final verified delivery is ready, call `swarm_board_post(sender="coordinator", kind="escalation", summary="@primary ...")` and rely on `wakeup_bridge` rather than also firing a duplicate `send_message` for the same hop.
4. **Loop recovery:** If a worker is stuck looping, `swarm_worker(action="kill", ...)` it and hand its work to a fresh worker.
5. **Quota-recovery resume:** Never kill-and-respawn on quota or capacity errors. Use `swarm_worker(action="switch_model", ...)` to preserve warm `context_id`; after any `switch_model` or quota reset, check `swarm_worker(action="status")` / `swarm_manage(action="status")` and resume any stalled work.

## Channels & Addressing

- Keep small swarms (`<= 6` workers) in `main`. For larger swarms, use `swarm_manage(action="channel", op="create"|"subscribe"|"unsubscribe"|"list"|"archive", ...)`.
- **Never invent `#seq` channels:** Do NOT pass `--channel "#seq"` or reference a non-existent `#seq` channel. Create real topic channels explicitly first (`swarm_manage(action="channel", op="create", ...)` / `swarm_channel`), and quote post references like `#4` in message text rather than `--channel "#4"`.
- `@#channel` wakes every subscriber of that channel; `@name` wakes one worker (or numbered group `<name>-1..k`); `@all` is strictly for swarm-wide decisions.
- **`@name` is strictly a wakeup doorbell, never a CC (Anti-Ping-Pong Rule):** NEVER `@mention` a peer or `@coordinator` merely to say accepted, confirmed, thanks, acknowledged, or already-answered — `@name` must ONLY be used when the target must perform NEW work. Workers tag only the next acting peer, or `@coordinator` on final delivery or blockers.

## Clean Closeout & Worktree Hygiene

Before escalating `[done]` to `@primary`:
1. Confirm via `swarm_board_read(status=true)` / `swarm_worker(action="status")` that **all non-observer workers are idle or finished (`waiting` / completed, NOT still `working` mid-turn)** so no late worker posts arrive after closeout.
2. Inspect every author's changes with `swarm_worker(action="diff")` (optionally pass `name="<worker>"` for one worker, or omit `name` to aggregate all workers) and `swarm_workspace(action="git_diff", conversation_id=...)` and run real tests.
3. If `worktree=True` was explicitly used, land verified worktree commits/patches into the target branch before running `swarm_workspace(action="prune_worktrees")`; otherwise verify clean `git status` on the active branch.
4. Write the verified receipt/handoff and any new one-line lesson to the active project's Obsidian vault (`Evidence/`, `Handoffs/`, `Lessons/lessons.md`) when a project vault is declared — never to a separate personal vault — and cite the saved vault path in the `[done]` summary.
5. Release scope locks with `swarm_board_read(scope_action="release", ...)` and post `[done] <summary>` on `main` (without `@`) so any monitor goes dormant.
6. Use `swarm_manage(action="clear_finished")` when resetting completed worker task history.
