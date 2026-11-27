"""Event-driven MentionRouter, 3-tier wakeup classifier, pending-mention replay, and 500ms disk watcher."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import os
from pathlib import Path
import re
import time
from typing import Any, Awaitable, Callable

import board as board_mod
import models
import store

_MAX_DEDUPE_ENTRIES = 4096
_OBSERVER_ROLES = frozenset({models.Role.MONITOR.value, models.Role.NARRATOR.value})
_OBSERVER_NAME_RE = re.compile(r"^(?:narrator|monitor)(?:-\d+)?$", re.IGNORECASE)
_DONE_OR_STALL_RE = re.compile(r"^(?:@[A-Za-z0-9_-]+[\s,:–—-]*)*\[(?:done|stall)\]", re.IGNORECASE)
_COORDINATOR_WAKE_TYPES = frozenset({
    models.MessageType.QUESTION.value,
    models.MessageType.ESCALATION.value,
    models.MessageType.HANDOFF.value,
})
_COORDINATOR_WAKE_RAW_KINDS = frozenset({
    "question",
    "blocker",
    "escalation",
    "handoff",
    "handoff_ready",
    "approval_request",
})


def _parse_sender_tier(raw: Any) -> int:
    if raw is None or raw == "":
        return 2
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 2


def classify_wake_event(msg: dict[str, Any]) -> str | None:
    """Classify a board message into `coordinator_wake`, `primary_wake`, or `None`.

    Strict 3-tier law:
    - Tier-3 `subagent` records NEVER emit wake events.
    - Tier-2 worker posts targeting `@coordinator`, `question`, `blocker`, `escalation`,
      `handoff`, `handoff_ready`, `approval_request`, `[done]`, or `[stall]` emit
      `coordinator_wake` ONLY (never `primary_wake`).
    - Primary posts targeting `@coordinator` emit `coordinator_wake`.
    - Coordinator posts targeting `@primary` or `kind="escalation"` emit `primary_wake`.
    """
    if not isinstance(msg, dict) or isinstance(msg.get("control"), dict):
        return None
    status = str(msg.get("status") or "open").strip().lower()
    if status != "open":
        return None

    sender_role = str(msg.get("senderRole") or "").strip().lower()
    sender_tier = _parse_sender_tier(msg.get("senderTier"))
    if sender_role == models.Role.SUBAGENT.value or sender_tier == 3:
        return None

    sender = str(msg.get("sender") or "").strip().lower()
    raw_kind = str(msg.get("kind") or msg.get("type") or "").strip().lower()
    mtype = models.normalize_message_type(msg.get("type") or msg.get("kind"))
    summary_raw = str(msg.get("summary") or msg.get("text") or "").strip()
    summary_low = summary_raw.lower()

    mentions: set[str] = set()
    if msg.get("mentions") is not None:
        for x in (msg.get("mentions") or []):
            cleaned = str(x).strip().lstrip("@").lower()
            if cleaned:
                mentions.add(cleaned)
    else:
        for x in (msg.get("to") or []):
            cleaned = str(x).strip().lstrip("@").lower()
            if cleaned:
                mentions.add(cleaned)
        for inline in board_mod.extract_mentions(summary_raw):
            mentions.add(inline.lower())

    if sender_role == models.Role.COORDINATOR.value or sender == models.Role.COORDINATOR.value:
        if (
            mtype == models.MessageType.ESCALATION.value
            or raw_kind == models.MessageType.ESCALATION.value
            or models.Role.PRIMARY.value in mentions
        ):
            return "primary_wake"
        return None

    if sender_role == models.Role.PRIMARY.value or sender == models.Role.PRIMARY.value:
        if models.Role.COORDINATOR.value in mentions:
            return "coordinator_wake"
        return None

    if (
        models.Role.COORDINATOR.value in mentions
        or raw_kind in _COORDINATOR_WAKE_RAW_KINDS
        or mtype in _COORDINATOR_WAKE_TYPES
    ):
        return "coordinator_wake"
    if summary_low.startswith(("[done]", "[stall]")) or bool(_DONE_OR_STALL_RE.match(summary_raw)):
        return "coordinator_wake"
    return None


def resolve_worker_targets(msg: dict[str, Any], board: dict[str, Any]) -> list[str]:
    """Resolve live Tier-2 worker member names addressed by `@peer`, `@#channel`, or `@all`."""
    if not isinstance(msg, dict) or isinstance(msg.get("control"), dict):
        return []
    sender_role = str(msg.get("senderRole") or "").strip().lower()
    if sender_role == models.Role.SUBAGENT.value or _parse_sender_tier(msg.get("senderTier")) == 3:
        return []

    sender_low = str(msg.get("sender") or "").strip().lower()
    members = board.get("members") if isinstance(board.get("members"), dict) else {}

    live_workers: list[tuple[str, dict[str, Any]]] = []
    for name, mem in members.items():
        if not isinstance(mem, dict):
            continue
        role = str(mem.get("role") or "").strip().lower()
        if role in (models.Role.PRIMARY.value, models.Role.COORDINATOR.value, models.Role.SUBAGENT.value):
            continue
        if name.lower() in (models.Role.PRIMARY.value, models.Role.COORDINATOR.value):
            continue
        if mem.get("killed") or str(mem.get("state") or "").lower() == models.MemberState.KILLED.value:
            continue
        if str(mem.get("runtime") or "hermes").lower() != "hermes":
            continue
        live_workers.append((name, mem))

    if not live_workers:
        return []

    raw_mentions = [str(x).strip().lstrip("@").lower() for x in (msg.get("mentions") or []) if str(x).strip()]
    if msg.get("mentions") is None:
        raw_mentions = [
            x.lower()
            for x in board_mod.extract_mentions(str(msg.get("summary") or msg.get("text") or ""))
        ]
    has_all = "all" in raw_mentions
    chan_mentions = [models.normalize_channel_name(c) for c in (msg.get("channelMentions") or [])]
    channels_map = board.get("channels") if isinstance(board.get("channels"), dict) else {}
    active_chan_mentions: list[str] = []
    for ch in chan_mentions:
        if ch == models.DEFAULT_CHANNEL:
            continue
        rec = channels_map.get(ch)
        if isinstance(rec, dict) and rec.get("archived"):
            continue
        active_chan_mentions.append(ch)

    targets: list[str] = []
    seen_low: set[str] = set()

    def _add(wname: str) -> None:
        wlow = wname.lower()
        if wlow == sender_low or wlow in seen_low:
            return
        seen_low.add(wlow)
        targets.append(wname)

    for wname, mem in live_workers:
        wlow = wname.lower()
        wrole = str(mem.get("role") or "").strip().lower()
        is_observer = wrole in _OBSERVER_ROLES or bool(_OBSERVER_NAME_RE.match(wname))

        # Direct @name or numbered group prefix (<prefix>-<digits>).
        direct_hit = wlow in raw_mentions
        if not direct_hit and "-" in wlow:
            prefix, suffix = wlow.rsplit("-", 1)
            if suffix.isdigit() and prefix in raw_mentions:
                direct_hit = True
        if direct_hit:
            _add(wname)
            continue

        # @all wakes task-execution workers (never narrator/monitor unless named directly).
        if has_all and not is_observer:
            _add(wname)
            continue

        # @#channel wakes members subscribed to that channel.
        if active_chan_mentions:
            eff_chs = board_mod.effective_member_channels(wrole, mem.get("channels"), wname)
            if "*" in eff_chs or any(ch in eff_chs for ch in active_chan_mentions):
                _add(wname)

    return targets


def format_wake_prompt(messages: list[dict[str, Any]], *, target_worker: str) -> str:
    """Format one or more board messages into a clean wakeup prompt for a Tier-2 worker."""
    if len(messages) == 1:
        m = messages[0]
        seq = m.get("seq")
        ch = board_mod.channel_display_name(m.get("channel"))
        mtype = m.get("type") or "post"
        sender = m.get("sender") or "peer"
        summary = str(m.get("summary") or "").strip()
        details = str(m.get("details") or "").strip()
        files = [str(f) for f in (m.get("files") or []) if str(f).strip()]
        lines = [
            f"[Board @mention wakeup #{seq} in #{ch} ({mtype}) from @{sender} to @{target_worker}]",
            f"Summary: {summary}",
        ]
        if details:
            lines.append(f"Details:\n{details}")
        if files:
            lines.append("Files: " + ", ".join(files))
        lines.append("Act on this message in your workspace and post your update back to the Swarm Board.")
        return "\n".join(lines)

    lines = [
        f"[Batched Board @mention wakeup ({len(messages)} messages) for @{target_worker}]",
    ]
    for m in messages:
        seq = m.get("seq")
        ch = board_mod.channel_display_name(m.get("channel"))
        mtype = m.get("type") or "post"
        sender = m.get("sender") or "peer"
        summary = str(m.get("summary") or "").strip()
        details = str(m.get("details") or "").strip()
        lines.append(f"- #{seq} in #{ch} ({mtype}) from @{sender}: {summary}")
        if details:
            lines.append(f"  Details: {details[:400]}")
    lines.append("Process these messages in order and post your response on the Swarm Board.")
    return "\n".join(lines)


def _find_member_record(board: dict[str, Any], target: str) -> dict[str, Any] | None:
    members = board.get("members") if isinstance(board.get("members"), dict) else {}
    exact = members.get(target)
    if isinstance(exact, dict):
        return exact
    tlow = str(target or "").strip().lower()
    for name, rec in members.items():
        if name.lower() == tlow and isinstance(rec, dict):
            return rec
    return None


def _is_member_killed(mem: dict[str, Any] | None) -> bool:
    if not isinstance(mem, dict):
        return False
    return bool(mem.get("killed")) or str(mem.get("state") or "").strip().lower() == models.MemberState.KILLED.value


def _filter_unread_for_member(
    messages: list[dict[str, Any]],
    board: dict[str, Any],
    target: str,
) -> list[dict[str, Any]]:
    mem = _find_member_record(board, target)
    if _is_member_killed(mem):
        return []
    last_read = int((mem or {}).get("lastReadSeq") or 0)
    if last_read <= 0:
        return list(messages)
    return [
        m for m in messages
        if int(m.get("seq") or 0) == 0 or int(m.get("seq") or 0) > last_read
    ]


class MentionRouter:
    """Routes board messages to SSE wakeups and Tier-2 Hermes workers with 500ms disk watching."""

    def __init__(
        self,
        board_service: board_mod.BoardService,
        *,
        is_worker_busy: Callable[[str, str], bool] | None = None,
        wake_worker: Callable[[str, str, str], Awaitable[bool]] | None = None,
        emit_event: Callable[[str, str, dict[str, Any]], None] | None = None,
        poll_interval: float = 0.5,
        debounce_seconds: float = 0.05,
    ) -> None:
        self._board = board_service
        self._is_worker_busy = is_worker_busy
        self._wake_worker = wake_worker
        self._emit_event = emit_event
        self._poll_interval = max(0.1, float(poll_interval))
        self._debounce_seconds = max(0.0, float(debounce_seconds))

        self._last_processed_seq: dict[str, int] = {}
        self._last_file_sigs: dict[str, tuple[int, int] | None] = {}
        self._last_view_sigs: dict[str, tuple[int, int] | None] = {}
        self._dispatched: OrderedDict[tuple[str, int, str], float] = OrderedDict()
        self._swarms_with_pending: set[str] = set()
        self._seeded = False
        self._watcher_task: asyncio.Task[None] | None = None
        self._watcher_loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._debounce_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._debounce_buffers: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _ensure_lock(self) -> asyncio.Lock:
        try:
            cur_loop = asyncio.get_running_loop()
        except RuntimeError:
            return self._lock
        if self._lock_loop is not cur_loop:
            self._lock = asyncio.Lock()
            self._lock_loop = cur_loop
        return self._lock

    def set_callbacks(
        self,
        *,
        is_worker_busy: Callable[[str, str], bool] | None = None,
        wake_worker: Callable[[str, str, str], Awaitable[bool]] | None = None,
        emit_event: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        if is_worker_busy is not None:
            self._is_worker_busy = is_worker_busy
        if wake_worker is not None:
            self._wake_worker = wake_worker
        if emit_event is not None:
            self._emit_event = emit_event

    def _mark_dispatched(self, swarm_id: str, seq: int, target_low: str) -> None:
        key = (swarm_id, int(seq), target_low)
        self._dispatched[key] = time.monotonic()
        self._dispatched.move_to_end(key)
        while len(self._dispatched) > _MAX_DEDUPE_ENTRIES:
            self._dispatched.popitem(last=False)

    def _is_dispatched(self, swarm_id: str, seq: int, target_low: str) -> bool:
        return (swarm_id, int(seq), target_low) in self._dispatched

    def seed_existing(self, swarm_ids: list[str] | None = None) -> None:
        """Record current board head seqs on startup so pre-existing posts are not re-dispatched."""
        swarms_dir = self._board.state_dir / "swarms"
        candidates: set[str] = set(swarm_ids or [])
        if swarms_dir.is_dir():
            try:
                for entry in swarms_dir.iterdir():
                    if entry.is_dir():
                        candidates.add(entry.name)
            except OSError:
                pass
        for sid in candidates:
            _, bfile, _ = board_mod.resolve_board_location(sid, state_dir=self._board.state_dir)
            try:
                st = bfile.stat()
                self._last_file_sigs[sid] = (st.st_mtime_ns, st.st_size)
            except OSError:
                self._last_file_sigs[sid] = None
            vfile = bfile.parent / "view.json"
            try:
                vst = vfile.stat()
                self._last_view_sigs[sid] = (vst.st_mtime_ns, vst.st_size)
            except OSError:
                self._last_view_sigs[sid] = None
            board = self._board.read_board(sid)
            head = self._board.head_seq(board)
            self._last_processed_seq[sid] = max(self._last_processed_seq.get(sid, 0), head)
            pending_map = board.get("pendingMentions")
            if isinstance(pending_map, dict) and any(v for v in pending_map.values()):
                self._swarms_with_pending.add(sid)

    def _emit_wake_if_needed(self, swarm_id: str, msg: dict[str, Any]) -> str | None:
        wake_kind = classify_wake_event(msg)
        if wake_kind and self._emit_event is not None:
            seq = int(msg.get("seq") or 0)
            if not self._is_dispatched(swarm_id, seq, f"__sse:{wake_kind}"):
                self._mark_dispatched(swarm_id, seq, f"__sse:{wake_kind}")
                self._emit_event(
                    swarm_id,
                    wake_kind,
                    {
                        "swarmId": swarm_id,
                        "seq": seq,
                        "message": msg,
                    },
                )
        return wake_kind

    async def process_message(
        self,
        swarm_id: str,
        msg: dict[str, Any],
        *,
        board: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Process one newly posted message: emit SSE wake events and route worker mentions."""
        direct_dispatches: list[tuple[str, str, list[dict[str, Any]]]] = []
        async with self._ensure_lock():
            res = await self._process_message_locked(
                swarm_id,
                msg,
                board=board,
                _defer_direct=direct_dispatches,
            )
        for d_sid, d_target, d_msgs in direct_dispatches:
            try:
                ok = await self._dispatch_worker_wake(d_sid, d_target, d_msgs)
                if ok and d_target not in res["woken"]:
                    res["woken"].append(d_target)
            except Exception as exc:
                store.append_swarm_log(
                    d_sid,
                    "wakeup",
                    {"event": "wake_dispatch_exception", "target": d_target, "error": str(exc)},
                    state_dir=self._board.state_dir,
                )
        return res

    async def _process_message_locked(
        self,
        swarm_id: str,
        msg: dict[str, Any],
        *,
        board: dict[str, Any] | None = None,
        _defer_direct: list[tuple[str, str, list[dict[str, Any]]]] | None = None,
    ) -> dict[str, Any]:
        sid = store.sanitize_swarm_id(swarm_id) or "default"
        if not isinstance(msg, dict):
            return {"wakeEvent": None, "woken": [], "queued": []}

        seq = int(msg.get("seq") or 0)
        if seq > 0:
            self._last_processed_seq[sid] = max(self._last_processed_seq.get(sid, 0), seq)

        wake_event = self._emit_wake_if_needed(sid, msg)
        cur_board = board if isinstance(board, dict) else self._board.read_board(sid)
        targets = resolve_worker_targets(msg, cur_board)
        if not targets:
            return {"wakeEvent": wake_event, "woken": [], "queued": []}

        woken: list[str] = []
        queued: list[str] = []
        for target in targets:
            tlow = target.lower()
            if seq > 0 and self._is_dispatched(sid, seq, tlow):
                continue

            mem = _find_member_record(cur_board, target)
            if _is_member_killed(mem):
                continue
            last_read = int((mem or {}).get("lastReadSeq") or 0)
            if seq > 0 and last_read >= seq:
                self._mark_dispatched(sid, seq, tlow)
                continue

            busy = bool(self._is_worker_busy and self._is_worker_busy(sid, target))
            if busy:
                self._board.enqueue_pending_mention(
                    target,
                    {
                        "seq": seq,
                        "sender": msg.get("sender"),
                        "channel": msg.get("channel"),
                        "type": msg.get("type"),
                        "summary": msg.get("summary"),
                        "details": msg.get("details"),
                        "files": list(msg.get("files") or []),
                        "createdAt": msg.get("createdAt"),
                    },
                    swarm_id=sid,
                )
                self._swarms_with_pending.add(sid)
                if seq > 0:
                    self._mark_dispatched(sid, seq, tlow)
                queued.append(target)
                continue

            if self._debounce_seconds > 0:
                await self._schedule_debounced_wake(sid, target, msg)
                woken.append(target)
            elif _defer_direct is not None:
                _defer_direct.append((sid, target, [msg]))
            else:
                ok = await self._dispatch_worker_wake(sid, target, [msg])
                if ok:
                    woken.append(target)

        return {"wakeEvent": wake_event, "woken": woken, "queued": queued}

    def _enqueue_batch_on_failure(self, swarm_id: str, target: str, batch: list[dict[str, Any]]) -> None:
        for m in batch:
            try:
                self._board.enqueue_pending_mention(
                    target,
                    {
                        "seq": int(m.get("seq") or 0),
                        "sender": m.get("sender"),
                        "channel": m.get("channel"),
                        "type": m.get("type"),
                        "summary": m.get("summary"),
                        "details": m.get("details"),
                        "files": list(m.get("files") or []),
                        "createdAt": m.get("createdAt"),
                    },
                    swarm_id=swarm_id,
                )
            except Exception:
                pass
        if batch:
            self._swarms_with_pending.add(swarm_id)

    async def _schedule_debounced_wake(self, swarm_id: str, target: str, msg: dict[str, Any]) -> None:
        key = (swarm_id, target.lower())
        buf = self._debounce_buffers.setdefault(key, [])
        seq = int(msg.get("seq") or 0)
        if not any(int(x.get("seq") or 0) == seq and seq > 0 for x in buf):
            buf.append(msg)

        prev_task = self._debounce_tasks.get(key)
        if prev_task and not prev_task.done():
            prev_task.cancel()

        async def _fire() -> None:
            batch: list[dict[str, Any]] = []
            dispatched = False
            try:
                await asyncio.sleep(self._debounce_seconds)
                async with self._ensure_lock():
                    batch = self._debounce_buffers.pop(key, [])
                if batch:
                    dispatched = True
                    await self._dispatch_worker_wake(swarm_id, target, batch)
            except asyncio.CancelledError:
                if batch and not dispatched:
                    self._enqueue_batch_on_failure(swarm_id, target, batch)
                return
            except Exception:
                if batch and not dispatched:
                    self._enqueue_batch_on_failure(swarm_id, target, batch)
            finally:
                if self._debounce_tasks.get(key) is asyncio.current_task():
                    self._debounce_tasks.pop(key, None)

        self._debounce_tasks[key] = asyncio.create_task(_fire())

    async def _dispatch_worker_wake(
        self,
        swarm_id: str,
        target: str,
        new_messages: list[dict[str, Any]],
    ) -> bool:
        if self._wake_worker is None:
            return False

        board = self._board.read_board(swarm_id)
        if _is_member_killed(_find_member_record(board, target)):
            self._board.drain_pending_mentions(target, swarm_id=swarm_id)
            return False

        # Re-check busy right before dispatch in case worker started between enqueue & flush.
        if self._is_worker_busy and self._is_worker_busy(swarm_id, target):
            for m in _filter_unread_for_member(new_messages, board, target):
                seq = int(m.get("seq") or 0)
                self._board.enqueue_pending_mention(
                    target,
                    {
                        "seq": seq,
                        "sender": m.get("sender"),
                        "channel": m.get("channel"),
                        "type": m.get("type"),
                        "summary": m.get("summary"),
                        "details": m.get("details"),
                        "files": list(m.get("files") or []),
                        "createdAt": m.get("createdAt"),
                    },
                    swarm_id=swarm_id,
                )
                self._swarms_with_pending.add(swarm_id)
                if seq > 0:
                    self._mark_dispatched(swarm_id, seq, target.lower())
            return False

        pending = self._board.drain_pending_mentions(target, swarm_id=swarm_id)
        combined: list[dict[str, Any]] = []
        seen_seqs: set[int] = set()
        for m in [*pending, *new_messages]:
            seq = int(m.get("seq") or 0)
            if seq > 0 and seq in seen_seqs:
                continue
            if seq > 0:
                seen_seqs.add(seq)
            combined.append(m)

        combined = _filter_unread_for_member(combined, board, target)
        if not combined:
            return False
        combined.sort(key=lambda m: int(m.get("seq") or 0))
        prompt = format_wake_prompt(combined, target_worker=target)
        seqs = [int(m.get("seq") or 0) for m in combined if int(m.get("seq") or 0) > 0]
        try:
            ok = await self._wake_worker(swarm_id, target, prompt)
        except asyncio.CancelledError:
            self._enqueue_batch_on_failure(swarm_id, target, combined)
            raise
        except Exception as exc:
            store.append_swarm_log(
                swarm_id,
                "wakeup",
                {"event": "wake_dispatch_error", "target": target, "seqs": seqs, "error": str(exc)},
                state_dir=self._board.state_dir,
            )
            self._enqueue_batch_on_failure(swarm_id, target, combined)
            return False
        if ok:
            store.append_swarm_log(
                swarm_id,
                "wakeup",
                {"event": "wake_dispatch_ok", "target": target, "seqs": seqs},
                state_dir=self._board.state_dir,
            )
            max_seq = 0
            for m in combined:
                seq = int(m.get("seq") or 0)
                if seq > 0:
                    self._mark_dispatched(swarm_id, seq, target.lower())
                    if seq > max_seq:
                        max_seq = seq
            if max_seq > 0:
                self._board.mark_member_read(target, max_seq, swarm_id=swarm_id)
        else:
            store.append_swarm_log(
                swarm_id,
                "wakeup",
                {"event": "wake_dispatch_failed", "target": target, "seqs": seqs},
                state_dir=self._board.state_dir,
            )
            # Re-queue on failure so no mention is lost silently.
            self._enqueue_batch_on_failure(swarm_id, target, combined)
        return ok

    async def _flush_pending_locked(self, sid: str, worker_name: str, *, board: dict[str, Any] | None = None) -> bool:
        if self._wake_worker is None:
            return False
        if self._is_worker_busy and self._is_worker_busy(sid, worker_name):
            self._swarms_with_pending.add(sid)
            return False
        cur_board = board if isinstance(board, dict) else self._board.read_board(sid)
        if _is_member_killed(_find_member_record(cur_board, worker_name)):
            self._board.drain_pending_mentions(worker_name, swarm_id=sid)
            return False
        pending = self._board.drain_pending_mentions(worker_name, swarm_id=sid)
        if not pending:
            return False
        pending = _filter_unread_for_member(pending, cur_board, worker_name)
        if not pending:
            return False
        pending.sort(key=lambda m: int(m.get("seq") or 0))
        prompt = format_wake_prompt(pending, target_worker=worker_name)
        seqs = [int(m.get("seq") or 0) for m in pending if int(m.get("seq") or 0) > 0]
        try:
            ok = await self._wake_worker(sid, worker_name, prompt)
        except asyncio.CancelledError:
            self._enqueue_batch_on_failure(sid, worker_name, pending)
            raise
        except Exception as exc:
            store.append_swarm_log(
                sid,
                "wakeup",
                {"event": "wake_flush_error", "target": worker_name, "seqs": seqs, "error": str(exc)},
                state_dir=self._board.state_dir,
            )
            self._enqueue_batch_on_failure(sid, worker_name, pending)
            return False
        if ok:
            store.append_swarm_log(
                sid,
                "wakeup",
                {"event": "wake_flush_ok", "target": worker_name, "seqs": seqs},
                state_dir=self._board.state_dir,
            )
            max_seq = 0
            for m in pending:
                seq = int(m.get("seq") or 0)
                if seq > 0:
                    self._mark_dispatched(sid, seq, worker_name.lower())
                    if seq > max_seq:
                        max_seq = seq
            if max_seq > 0:
                self._board.mark_member_read(worker_name, max_seq, swarm_id=sid)
        else:
            store.append_swarm_log(
                sid,
                "wakeup",
                {"event": "wake_flush_failed", "target": worker_name, "seqs": seqs},
                state_dir=self._board.state_dir,
            )
            self._enqueue_batch_on_failure(sid, worker_name, pending)
        return ok

    async def flush_pending_for_worker(self, swarm_id: str, worker_name: str) -> bool:
        """Replay any queued `pendingMentions` for `worker_name` once its turn finishes."""
        sid = store.sanitize_swarm_id(swarm_id) or "default"
        return await self._flush_pending_locked(sid, worker_name)

    async def poll_disk_once(self, swarm_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Check `board.json` mtime/size for changes, route unseen messages, and replay orphaned mentions."""
        results: list[dict[str, Any]] = []
        direct_dispatches: list[tuple[str, str, list[dict[str, Any]]]] = []
        pending_flushes: list[tuple[str, str, dict[str, Any]]] = []
        check_empty_swarms: list[str] = []

        async with self._ensure_lock():
            swarms_dir = self._board.state_dir / "swarms"
            candidates: set[str] = set(swarm_ids or [])
            candidates.update(self._last_processed_seq.keys())
            candidates.update(self._swarms_with_pending)
            if swarms_dir.is_dir():
                try:
                    for entry in swarms_dir.iterdir():
                        if entry.is_dir():
                            candidates.add(entry.name)
                except OSError:
                    pass

            for sid in sorted(candidates):
                _, bfile, _ = board_mod.resolve_board_location(sid, state_dir=self._board.state_dir)
                try:
                    st = bfile.stat()
                    sig: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
                except OSError:
                    sig = None

                prev_sig = self._last_file_sigs.get(sid)
                sig_changed = sig is not None and sig != prev_sig
                vfile = bfile.parent / "view.json"
                try:
                    vst = vfile.stat()
                    view_sig: tuple[int, int] | None = (vst.st_mtime_ns, vst.st_size)
                except OSError:
                    view_sig = None
                prev_view_sig = self._last_view_sigs.get(sid)
                view_changed = view_sig is not None and view_sig != prev_view_sig

                if not sig_changed and not view_changed and sid not in self._swarms_with_pending:
                    continue
                if sig is not None:
                    self._last_file_sigs[sid] = sig
                if view_sig is not None:
                    self._last_view_sigs[sid] = view_sig

                board = self._board.read_board(sid)
                if view_changed:
                    head_now = self._board.head_seq(board)
                    if head_now > 0:
                        for mname, mrec in (board.get("members") or {}).items():
                            if not isinstance(mrec, dict) or _is_member_killed(mrec):
                                continue
                            mrole = str(mrec.get("role") or "").strip().lower()
                            if mrole == models.Role.NARRATOR.value or mname.lower().startswith("narrator"):
                                if int(mrec.get("lastReadSeq") or 0) < head_now:
                                    self._board.mark_member_read(mname, head_now, swarm_id=sid)
                    if self._emit_event is not None:
                        self._emit_event(
                            sid,
                            "board_update",
                            {"swarmId": sid, "head": head_now, "viewUpdated": True},
                        )

                if sig_changed:
                    last_seq = self._last_processed_seq.get(sid, 0)
                    unseen = [
                        m for m in (board.get("messages") or [])
                        if isinstance(m, dict) and int(m.get("seq") or 0) > last_seq
                    ]
                    unseen.sort(key=lambda m: int(m.get("seq") or 0))
                    if unseen:
                        if self._emit_event is not None:
                            self._emit_event(
                                sid,
                                "board_update",
                                {"swarmId": sid, "head": self._board.head_seq(board)},
                            )
                        for msg in unseen:
                            res = await self._process_message_locked(
                                sid,
                                msg,
                                board=board,
                                _defer_direct=direct_dispatches,
                            )
                            results.append({"swarmId": sid, "seq": msg.get("seq"), **res})

                # Close TOCTOU race between enqueue_pending_mention and turn completion:
                # replay any queued mentions for idle, non-killed workers.
                pending_map = board.get("pendingMentions") if isinstance(board.get("pendingMentions"), dict) else {}
                non_empty_targets = [
                    str(wkey) for wkey, items in pending_map.items()
                    if isinstance(items, list) and len(items) > 0
                ]
                if not non_empty_targets:
                    self._swarms_with_pending.discard(sid)
                elif self._wake_worker is not None:
                    still_busy = False
                    for wkey in non_empty_targets:
                        if self._is_worker_busy and self._is_worker_busy(sid, wkey):
                            still_busy = True
                            continue
                        pending_flushes.append((sid, wkey, board))
                    if not still_busy:
                        check_empty_swarms.append(sid)

        for d_sid, d_target, d_msgs in direct_dispatches:
            try:
                await self._dispatch_worker_wake(d_sid, d_target, d_msgs)
            except Exception:
                pass
        for f_sid, f_wkey, f_board in pending_flushes:
            try:
                await self._flush_pending_locked(f_sid, f_wkey, board=f_board)
            except Exception:
                pass
        for sid in check_empty_swarms:
            try:
                refreshed = self._board.read_board(sid)
                ref_pending = refreshed.get("pendingMentions") if isinstance(refreshed.get("pendingMentions"), dict) else {}
                if not any(isinstance(v, list) and len(v) > 0 for v in ref_pending.values()):
                    self._swarms_with_pending.discard(sid)
            except Exception:
                pass
        return results

    def start_watcher(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Start the 500ms background disk watcher task (idempotent)."""
        target_loop = loop
        if target_loop is None:
            try:
                target_loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        if target_loop.is_closed():
            return
        if (
            self._watcher_task is not None
            and not self._watcher_task.done()
            and self._watcher_loop is target_loop
            and not self._watcher_loop.is_closed()
        ):
            return

        if not self._seeded:
            self.seed_existing()
            self._seeded = True
        self._watcher_loop = target_loop
        stop_ev = asyncio.Event()
        self._stop_event = stop_ev

        async def _loop() -> None:
            while not stop_ev.is_set():
                try:
                    await self.poll_disk_once()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop_ev.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    continue

        def _spawn() -> None:
            if (
                self._watcher_task is not None
                and not self._watcher_task.done()
                and self._watcher_loop is target_loop
            ):
                return
            self._watcher_task = target_loop.create_task(_loop(), name="swarm-board-disk-watcher")

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is target_loop:
            _spawn()
        else:
            try:
                target_loop.call_soon_threadsafe(_spawn)
            except RuntimeError:
                pass

    async def stop_watcher(self) -> None:
        """Stop the background disk watcher and cancel pending debounce tasks cleanly."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._watcher_task is not None and not self._watcher_task.done():
            self._watcher_task.cancel()
            try:
                await self._watcher_task
            except (asyncio.CancelledError, Exception):
                pass
        self._watcher_task = None
        self._watcher_loop = None
        for task in list(self._debounce_tasks.values()):
            if not task.done():
                task.cancel()
        for (sid, target_key), pending_batch in list(self._debounce_buffers.items()):
            if pending_batch:
                self._enqueue_batch_on_failure(sid, target_key, pending_batch)
        self._debounce_tasks.clear()
        self._debounce_buffers.clear()
