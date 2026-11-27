# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| `1.0.x` | Yes |
| `< 1.0` | No |

## Architecture Trust Boundaries

Hermes Swarm operates strictly on **loopback (`127.0.0.1`)** by default:

- `/internal/*` board and wake endpoints require a per-state-directory Bearer token (`internal_token`, mode `0600`) plus strict loopback peer validation.
- The Antigravity sidecar exposes an in-process UI without unauthenticated remote exposure and enforces 1:1 `conversationId` swarm scoping (`403` on cross-swarm access).
- `GET /board/member/history` opens the Hermes session SQLite database in **read-only mode (`?mode=ro`)** and exposes redacted transcript fields without secret environment variables.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for suspected security vulnerabilities.

Report vulnerabilities privately using **GitHub Security Advisories** ("Report a vulnerability" under the **Security** tab of this repository). Include:

- Affected component (`packages/mcp-server`, `packages/antigravity-plugin`, or `packages/hermes-bridge`)
- Reproduction steps or proof-of-concept
- Potential impact and suggested remediation (if known)

We aim to acknowledge reports within 48 hours and publish a verified patch or advisory within 30 days.
