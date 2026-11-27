"""Windows-terminal-safe CLI for Tier-2 Hermes workers to interact with the Swarm Board."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import sys
from typing import Any
import urllib.error
import urllib.request

import board as board_mod
import store


def _resolve_token(token_file: str | None) -> str:
    if token_file:
        p = Path(store.normalize_local_path(token_file))
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                pass
    return os.environ.get("HERMES_A2A_HTTP_TOKEN", "").strip()


def _discover_http_url(
    explicit_url: str | None,
    *,
    swarm_id: str,
    conversation_id: str | None = None,
    board_dir: str | None = None,
) -> str | None:
    if explicit_url and explicit_url.strip():
        return explicit_url.strip().rstrip("/")
    env_url = os.environ.get("HERMES_A2A_HTTP_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    state_dir: Path | None = None
    if board_dir:
        bp = Path(store.normalize_local_path(board_dir))
        if (bp / "registry.json").is_file():
            state_dir = bp
        elif bp.parent.name == "swarms" and (bp.parent.parent / "registry.json").is_file():
            state_dir = bp.parent.parent
        else:
            return None
    reg = store.get_registry(state_dir=state_dir)
    servers = reg.get("servers") or {}
    clean_conv = str(conversation_id or "").strip()
    if clean_conv:
        for entry in servers.values():
            if isinstance(entry, dict) and entry.get("conversationId") == clean_conv and entry.get("httpUrl"):
                return str(entry["httpUrl"]).rstrip("/")
    for entry in servers.values():
        if isinstance(entry, dict) and entry.get("swarmId") == swarm_id and entry.get("httpUrl"):
            return str(entry["httpUrl"]).rstrip("/")
    return None


def _post_via_http(
    http_url: str,
    token: str,
    payload: dict[str, Any],
    *,
    timeout: float = 4.0,
) -> dict[str, Any] | None:
    url = f"{http_url.rstrip('/')}/internal/board/post"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                raw = resp.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw) if raw.strip() else {}
                return parsed if isinstance(parsed, dict) else {"result": parsed}
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code} from {url}: {err_body or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    return None


def _split_csv_list(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        text = str(raw).strip()
        if not text:
            continue
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                for item in parsed:
                    cleaned = str(item).strip()
                    if cleaned and cleaned not in out:
                        out.append(cleaned)
                continue
        for part in text.split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in out:
                out.append(cleaned)
    return out


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes Swarm Board CLI")
    parser.add_argument("--board-dir", default=os.environ.get("HERMES_A2A_SWARM_BOARD_DIR") or None)
    parser.add_argument("--swarm", default=os.environ.get("HERMES_A2A_SWARM_ID") or None)
    parser.add_argument("--conversation-id", default=os.environ.get("HERMES_A2A_CONVERSATION_ID") or None)
    parser.add_argument("--agent", "--sender", dest="agent", default=os.environ.get("HERMES_A2A_WORKER_NAME") or "worker")
    parser.add_argument("--role", default=os.environ.get("HERMES_A2A_WORKER_ROLE") or None)
    parser.add_argument("--task-id", default=os.environ.get("HERMES_A2A_TASK_ID") or None)
    parser.add_argument("--context-id", default=os.environ.get("HERMES_A2A_CONTEXT_ID") or None)
    parser.add_argument("--http-url", default=os.environ.get("HERMES_A2A_HTTP_URL") or None)
    parser.add_argument("--token-file", default=os.environ.get("HERMES_A2A_TOKEN_FILE") or None)

    sub = parser.add_subparsers(dest="command", required=True)

    p_post = sub.add_parser("post", help="Post a message to the Swarm Board")
    p_post.add_argument("--channel", default="main")
    p_post.add_argument("--to", action="append", default=[])
    p_post.add_argument("--kind", default="post")
    p_post.add_argument("--summary", required=True)
    p_post.add_argument("--details", default="")
    p_post.add_argument("--files", action="append", default=[])
    p_post.add_argument("--reply-to", type=int, default=None)
    p_post.add_argument("--thread-id", default=None)

    p_read = sub.add_parser("read", help="Read messages from the Swarm Board")
    p_read.add_argument("--since", type=int, default=None)
    p_read.add_argument("--seqs", default=None, help="Comma-separated exact sequence numbers")
    p_read.add_argument("--status", action="store_true", help="Return roster status instead of messages")
    p_read.add_argument("--channel", default=None)
    p_read.add_argument("--mention", default=None)
    p_read.add_argument("--sender", default=None)
    p_read.add_argument("--kind", default=None)
    p_read.add_argument("--limit", type=int, default=100)
    p_read.add_argument("--mark-read", action="store_true")
    p_read.add_argument("--include-archive", action="store_true")
    p_read.add_argument("--before-seq", type=int, default=None)
    p_read.add_argument("--search", default=None)
    p_read.add_argument("--full", action="store_true")

    p_ch = sub.add_parser("channels", help="List or manage Swarm Board channels")
    p_ch.add_argument("--op", default="list", choices=["list", "create", "subscribe", "unsubscribe", "archive"])
    p_ch.add_argument("--channel", default=None)
    p_ch.add_argument("--topic", default="")
    p_ch.add_argument("--members", action="append", default=[])

    p_claim = sub.add_parser("claim", help="Claim an editing scope on the Swarm Board")
    p_claim.add_argument("--scope", required=True)
    p_claim.add_argument("--notes", default="")

    p_release = sub.add_parser("release", help="Release one or all claimed scopes")
    p_release.add_argument("--scope", default=None)

    sub.add_parser("status", help="Inspect Swarm Board status, roster, channels, and claims")

    p_wait = sub.add_parser("wait", help="Wait for new board activity after --since")
    p_wait.add_argument("--since", type=int, default=0)
    p_wait.add_argument("--mentions-only", default="true")
    p_wait.add_argument("--timeout", type=float, default=300.0)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    bdir = args.board_dir
    state_dir: Path | None = None
    if bdir:
        bp = Path(store.normalize_local_path(bdir))
        if bp.parent.name == "swarms":
            state_dir = bp.parent.parent
    if args.swarm:
        swarm_id = store.sanitize_swarm_id(args.swarm) or "default"
    elif args.conversation_id:
        swarm_id = (
            store.resolve_swarm_for_conversation(args.conversation_id, state_dir=state_dir)
            or store.derive_swarm_id(conversation_id=args.conversation_id)
        )
    else:
        swarm_id = "default"
    svc = board_mod.BoardService()

    if args.command == "post":
        to_list = _split_csv_list(args.to)
        files_list = _split_csv_list(args.files)
        http_url = _discover_http_url(
            args.http_url,
            swarm_id=swarm_id,
            conversation_id=args.conversation_id,
            board_dir=bdir,
        )
        token = _resolve_token(args.token_file)
        if http_url:
            payload: dict[str, Any] = {
                "swarmId": swarm_id,
                "sender": args.agent,
                "senderRole": args.role,
                "kind": args.kind,
                "channel": args.channel,
                "to": to_list,
                "summary": args.summary,
                "details": args.details,
                "files": files_list,
                "replyTo": args.reply_to,
                "threadId": args.thread_id,
                "taskId": args.task_id,
                "contextId": args.context_id,
            }
            if args.conversation_id:
                payload["conversationId"] = args.conversation_id
            try:
                http_res = _post_via_http(http_url, token, payload)
            except RuntimeError as exc:
                print(json.dumps({"error": str(exc)}), file=sys.stderr)
                return 1
            if http_res is not None:
                _print_json(http_res)
                return 0

        msg = svc.post_message(
            sender=args.agent,
            summary=args.summary,
            msg_type=args.kind,
            channel=args.channel,
            to=to_list,
            details=args.details,
            files=files_list,
            reply_to=args.reply_to,
            thread_id=args.thread_id,
            task_id=args.task_id,
            context_id=args.context_id,
            sender_role=args.role,
            swarm_id=swarm_id,
            board_dir=bdir,
        )
        _print_json(msg)
        return 0

    if args.command == "read":
        seqs_list: list[int] | None = None
        if getattr(args, "seqs", None):
            try:
                seqs_list = [int(x.strip()) for x in str(args.seqs).split(",") if x.strip()]
            except ValueError:
                print(json.dumps({"error": f"Invalid --seqs value: {args.seqs!r}"}), file=sys.stderr)
                return 2
        res = svc.read_messages(
            swarm_id=swarm_id,
            since=args.since,
            seqs=seqs_list,
            status=bool(getattr(args, "status", False)),
            agent=args.agent,
            channel=args.channel,
            mention=args.mention,
            sender=args.sender,
            msg_type=args.kind,
            limit=args.limit,
            mark_read_by=args.agent if args.mark_read else None,
            include_archive=bool(getattr(args, "include_archive", False)),
            before_seq=getattr(args, "before_seq", None),
            search=getattr(args, "search", None),
            full=bool(getattr(args, "full", False)),
            board_dir=bdir,
        )
        _print_json(res)
        return 0

    if args.command == "channels":
        members = _split_csv_list(args.members)
        res = svc.channel_action(
            args.op,
            channel=args.channel,
            topic=args.topic,
            members=members or None,
            actor=args.agent,
            swarm_id=swarm_id,
            board_dir=bdir,
        )
        _print_json(res)
        return 0

    if args.command == "claim":
        res = svc.claim_scope(
            scope=args.scope,
            holder=args.agent,
            notes=args.notes,
            task_id=args.task_id,
            swarm_id=swarm_id,
            board_dir=bdir,
        )
        _print_json(res)
        return 0

    if args.command == "release":
        res = svc.release_scope(
            scope=args.scope,
            holder=args.agent,
            swarm_id=swarm_id,
            board_dir=bdir,
        )
        _print_json(res)
        return 0

    if args.command == "status":
        res = svc.get_status(swarm_id=swarm_id, board_dir=bdir)
        _print_json(res)
        return 0

    if args.command == "wait":
        mentions_only = str(args.mentions_only).strip().lower() not in ("0", "false", "no", "off")
        wait_timeout = args.timeout if math.isfinite(args.timeout) else 300.0
        res = asyncio.run(
            svc.wait_for_update(
                swarm_id=swarm_id,
                since=args.since,
                target=args.agent,
                mentions_only=mentions_only,
                timeout_seconds=wait_timeout,
                board_dir=bdir,
            )
        )
        _print_json(res)
        return 0

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
