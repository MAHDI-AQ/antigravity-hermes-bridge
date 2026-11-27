# NousResearch/hermes-agent Plugin Catalog PR Template

Copy and update the pinned `sha`, maintainer handle, and screenshots (once captured) when opening the PR adding `plugin-catalog/antigravity-hermes-bridge.yaml` to `NousResearch/hermes-agent`.

---

Adds **Antigravity <-> Hermes Bridge** (`antigravity-hermes-bridge`) to the Hermes Agent plugin catalog.

## What does this PR do?

Adds `plugin-catalog/antigravity-hermes-bridge.yaml`, pointing to [antigravity-hermes-bridge](https://github.com/MAHDI-AQ/antigravity-hermes-bridge) (`subdir: packages/hermes-bridge`) pinned at commit `<PINNED_40_CHAR_SHA>` (version `1.0.0`). I am the repository maintainer (Rule 5).

This is an **Antigravity <-> Hermes interoperability bridge** that lets teams pair Antigravity IDE sessions with Hermes Agent workers across local, open-weight, or custom-provider models cleanly through official public plugin surfaces:

- **Format:** Standalone Hermes plugin (`plugin.yaml`, `__init__.py`, `swarm_tools.py`, `README.md`), pure Python standard library (`zero` pip dependencies).
- **Declared Tools (`7`):** `swarm_post`, `swarm_read`, `swarm_claim`, `swarm_release`, `swarm_status`, `swarm_wait`, `swarm_channel`.
- **Conditional Worker Hooks (active only when `HERMES_SWARM_ID` is set):** `post_compaction`, `pre_llm_call`, `pre_verify`, `post_tool_call` (plus `hermes-swarm-bridge.worker` system prompt section via `ctx.register_system_prompt_section`).
- **Companion Requirement:** Pairs with Antigravity IDE + `packages/antigravity-plugin` + `packages/mcp-server` from the same repository.

## Disclosure (Catalog Rules 3, 7, 9, 11, 12, 13 & 17)

- **Network:** Communicates strictly over loopback (`127.0.0.1`) with the local Swarm HTTP server configured via `HERMES_SWARM_HTTP_URL` using the per-swarm Bearer token (`HERMES_SWARM_HTTP_TOKEN` or `HERMES_SWARM_TOKEN_FILE`). Makes zero external network requests.
- **Credentials:** Reads no `.env` files, Hermes `auth.json`, browser profiles, keychains, or external credential stores.
- **Local Footprint:** Reads/writes board state (`board.json`, `board_archive.jsonl`, `board.lock`, capped `logs/bridge.jsonl`) strictly inside `HERMES_SWARM_BOARD_DIR` when disk fallback is enabled.
- **Execution & Safety:** Pure Python stdlib only; `zero` compiled binaries or `.pyc` files; `zero` runtime patches/rebinds of Hermes core modules; `zero` self-updating code; `swarm_wait` runs strictly in-process via `threading.Event.wait(...)` without bypassing approvals or unattended execution limits.

## Verification

1. `hermes plugins validate packages/hermes-bridge --install-deps` -> `Validation passed.` (0 warnings)
2. `python scripts/scan_plugin_payloads.py` -> exit `0`
3. `python scripts/validate_plugin_catalog.py plugin-catalog/antigravity-hermes-bridge.yaml` -> `OK: 1 file(s) valid`
4. `python -m unittest discover -s packages/hermes-bridge/tests -p "test_*.py" -v` -> `15/15 OK`

## Checklist

- [x] Official public Hermes plugin surfaces only (`register_tool`, `register_system_prompt_section`, `register_hook`); zero core overrides
- [x] Exact 40-character immutable commit SHA pinned in `sha` and `docs_url`
- [x] Pure Python stdlib, zero binaries, zero self-updater, zero telemetry
- [x] Full Disclosure section included in both this PR and `packages/hermes-bridge/README.md`
- [x] Tested cleanly via `hermes plugins validate --install-deps`, `validate_plugin_catalog.py`, `scan_plugin_payloads.py`, and stdlib unit tests
