---
name: hermes-swarm-pane
description: >-
  How the Coordinator structures the 1:1 session-bound Hermes Swarm Overview
  goal, outcome-grouped threads, and concise task labels via
  swarm_manage(action="set_plan"). Load before briefing the first workers.
disable-slash-command: true
---

# The Hermes Swarm Overview

The Swarm sidecar pane is bound 1:1 to the active Antigravity conversation (`1 conversation = 1 swarm`, zero swarm switcher dropdown) and renders its Overview cleanly as an ops dashboard from live tasks, roster state, board posts, and your structured plan. Keep it focused by publishing and updating the plan via `set_plan`.

Do not spawn a `narrator` worker unless the user explicitly asks for one — `set_plan` and the built-in Overview handle standard swarms with zero extra token burn.

## Publish the plan

Right as you launch workers (and whenever you add workers, regroup lanes, or shift phase), call `swarm_manage(action="set_plan", plan={...})`:

```json
{
  "goal": {
    "title": "Fix split pane card layouts",
    "line": "Audit running across views; theme fixes queued next."
  },
  "tasks": {
    "impl-a": "Implement shared card component",
    "impl-b": "Unify split pane layouts",
    "test-runner": "Run dark and light theme UI suite"
  },
  "threads": [
    {
      "id": "t-split",
      "title": "Split pane cards",
      "goal": "Panes share one card with clean headers.",
      "now": "Implementing unified card components",
      "tasks": ["impl-a", "impl-b", "test-runner"]
    }
  ]
}
```

Rules:
- `goal.title`: 4 to 8 words naming the outcome.
- `goal.line`: at most 12 words summarizing current standing.
- `tasks`: map each active member roster name (`dict[str, str]`, including registered Antigravity IDE Specialists/Verifiers AND Hermes execution workers, e.g. `"arch-kernel": "..."`, `"impl-a": "..."`) to a short phrase (2 to 8 words).
- `threads`: 1 to 6 groups structured by the **outcome** they deliver, never one thread per agent; **always include a human-readable `threads[].title` (distinct from `threads[].id`, otherwise the UI falls back to the raw `id` slug)**, along with `goal`, `now`, and every active/planned member in `tasks`.
- **Dynamic updates:** Rewrite `plan` only when you add workers, move work (`Moving`, `Done`, `Blocked`, updating `now` as workers start, shift phase, or finish), or the goal changes; nothing else.
- Do not duplicate raw status logs or blockers inside `plan` — live status, escalations, and `@coordinator` questions come automatically from tasks and board posts.

## Result Images & Deliverable Handoff (`Worker -> Coordinator -> Primary -> Chat`)

Board Overview is strictly a live ops/coordination dashboard — never a result-image gallery:
- Workers and verifiers pass verified screenshot or output file paths to `@coordinator` in `files` / `[done]` reports.
- Coordinator verifies them and passes the final paths/results in the `@primary` escalation (`kind="escalation"`).
- Primary delivers the verified result images and full report directly to the user in chat.
