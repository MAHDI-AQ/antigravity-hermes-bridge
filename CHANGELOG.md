# Changelog

All notable changes to Hermes Swarm are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-27

### Added
- **Unified Monorepo & Cross-Platform Installer (`scripts/install.py`, `install.sh`, `install.ps1`, `doctor.py`):** One-command installation for Windows, macOS, and Linux, plus deterministic `--non-interactive --json` mode for AI coding agents.
- **MCP + A2A Swarm Backend (`packages/mcp-server/`):**
  - 7 core MCP tools (`swarm_catalog`, `swarm_workspace`, `swarm_manage`, `swarm_worker`, `swarm_board_post`, `swarm_board_read`, `swarm_wait`) supporting parallel sharding (`n`, `tasks`), context forking (`fork`), model hot-swapping (`switch_model`), worktree opt-in, and scoped `409` edit claims.
  - Per-step billable token economy (`totalProcessed`, `pureInput`, `cachedInput`, `cacheWrite`, `output`, `cacheHitRate`), turn-delta TPS on resumed workers + streaming character fallback (`tpsEstimated`), and context window resolution (`_resolve_context_limit` with user `config.yaml` support).
  - Latest-100 board reads (`limit=100`, `since=0` tail) with `include_archive`, `before_seq`, `search`, and `full` pagination across `board.json` and `board_archive.jsonl`.
  - Read-only SQLite session history endpoint (`GET /board/member/history`) powering Live Agent History Tabs, plus clean `tui_gateway` interrupt/close + `prompt-task-<id>` orphan sweep + HTTP `403` killed-poster guard on `kill_worker`.
  - Structured JSONL swarm event logs (`logs/*.jsonl`) and ghost-directory creation prevention on read-only endpoints.
- **Observer Board UI & Antigravity Sidecar (`packages/antigravity-plugin/`, `packages/mcp-server/board_ui/`):**
  - Flex App-Shell keeping the header, billable telemetry strip, and filter toolbar sticky while scrolling 400+ messages with windowing, `#seq` jump, and `#jump-top`.
  - Dynamic closeable `[● <name> ×]` **Live Agent History Tabs** rendering collapsible reasoning blocks, markdown replies, paired tool calls/results, Tier-3 subagent switchers, compaction dividers, and live pulsing tool footers.
  - Overview threads/tasks canvas, Hive graph visualizer, and host-local `wakeup_bridge.mjs`.
- **Stdlib-Only Hermes Bridge Plugin (`packages/hermes-bridge/`):**
  - Native `swarm_*` tools inside Hermes worker sessions, `board_archive.jsonl` overflow spilling on disk fallback, `logs/bridge.jsonl`, and `post_compaction` / `pre_llm_call` / `pre_verify` / `post_tool_call` lifecycle hooks.
- **Privacy & CI Gates (`scripts/check_privacy.py`, `.github/workflows/`):** Automated multi-OS CI and zero-PII/secret/hardcoded-path scanner.
