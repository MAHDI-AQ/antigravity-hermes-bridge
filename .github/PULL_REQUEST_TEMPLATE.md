## Summary
<!-- Describe what this PR changes and why. -->

## Component Scope
- [ ] `packages/mcp-server`
- [ ] `packages/antigravity-plugin`
- [ ] `packages/hermes-bridge`
- [ ] `scripts/` or `docs/`

## Verification Checklist
- [ ] `python scripts/check_privacy.py` exits `0`
- [ ] `packages/hermes-bridge` tests pass (`test_manifest_and_stdlib_only` green)
- [ ] `packages/mcp-server` unit tests (`test_hermes_a2a.py`) pass
- [ ] `node --test --test-timeout=30000 packages/mcp-server/tests/test_sidecar.mjs` passes
- [ ] Docs (`README.md`, `AGENTS.md`, `docs/`) updated if behavior or flags changed
