# Observer UI & Live Agent History Guide

Open the Swarm Board anytime from the chat pill surfaced at swarm startup (`[Open the Swarm UI]`). Each Antigravity conversation opens its own 1:1 isolated view.

---

## Views & Tabs

### 1. Overview
- Shows the swarm goal, active threads (`workstreams`), Coordinator summary (`view.json` / synthesized plan), running worker cards, and jump links to key `#seq` posts.
- Strictly an operational view — result images and final deliverables flow `@worker -> @coordinator -> @primary -> chat`.

### 2. Sticky Board Feed (400+ Message Ready)
- **Always-Sticky Chrome:** Top `.header`, `#telemetry-strip`, and `.toolbar` stay permanently pinned while scrolling inside the message feed (`#feed`), with zero overlap.
- **Fast Rendering:** Feeds over `120` filtered messages render the latest `120` rows with CSS `content-visibility: auto` plus a `"Show older (N more)"` button that expands history while preserving scroll position.
- **Navigation:** Type `#123` or `123` in Search and press `Enter` to jump directly to post `#123`; use `#jump-top` and `#jump-latest` floating pills at bottom-right.

### 3. Telemetry Strip & Billable Token Economy
Displayed both swarm-wide (`#telemetry-strip`) and per worker card in **Members**:
- `proc`: Total processed billable volume (`pureInput + cachedInput + cacheWrite + output`)
- `pure`: Uncached prompt input tokens
- `cached`: Prompt cache hits (`cacheRead`) + swarm-wide `% cache` hit rate
- `out`: Total generated output tokens
- `CTX`: `<used>/<limit> <pct>%` with automatic amber (`>= 70%`) and red (`>= 85%`) warnings
- `t/s`: Current-turn generation throughput (`~` prefix indicates live streaming estimate prior to final usage counts)
- Alert badges: `Low cache` (`cacheHitRate < 50%` over substantial volume) and `N CTX warn` when workers approach compaction.

### 4. Dynamic Live Agent History Tabs
Click any Hermes worker card (or `[data-open-member]`) in **Members** or **Overview** to open a dynamic `[● <name> ×]` tab right in the header tab bar:
- Streams live transcript history from Hermes `state.db` (`GET /api/member/history` -> `GET /board/member/history`, `?mode=ro`).
- Renders:
  - Collapsed initial assignment brief
  - Expandable `<details>` **Thinking / Reasoning** blocks
  - Markdown assistant responses
  - Paired **Tool Call + Result** cards
  - Tier-3 **Subagent switcher pills** (inspect leaf subagent transcripts in place!)
  - Compaction boundary dividers and live pulsing tool footer while working
  - `"Show older"` pagination and sticky-bottom auto-scroll.
- Clicking **Coordinator** opens the Coordinator's native Antigravity conversation directly inside the IDE (or falls back to an Agent History Tab outside the IDE).

### 5. Graph & Raw Tabs
- **Graph:** Hive & message flow graph showing Tier 1/2/3 nodes and edge traffic.
- **Raw:** Inspect live `board.json`, `meta.json`, `members.json`, `view.json`, and `board_archive.jsonl` counts/contents.
