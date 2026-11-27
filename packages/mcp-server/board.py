"""Unified event-driven Swarm v2 board engine, channels, roster, claims, and screenshot indexing."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fnmatch
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterator
import uuid

import models
import store

MAX_BOARD_MESSAGES = 1000
MAX_SCREENSHOTS = 24

_MENTION_RE = re.compile(r"(?<![\w.-])@([\w][\w-]*)")
_CHANNEL_MENTION_RE = re.compile(r"(?<![\w.-])@#([a-z0-9][a-z0-9_-]{0,47})(?![\w-])", re.IGNORECASE)
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")
_INLINE_CODE_RE = re.compile(r"(`+)(?:(?!\1)[^\r\n])*\1")
_SCREENSHOT_RE = re.compile(r"(?im)^\s*(?:[-*]\s*)?Screenshot\s*:\s*`?([^|`\r\n]+?)`?(?:\s*\|\s*(.*?))?\s*$")
_RESERVED_CHANNELS = frozenset({"main", "general", "all", "here"})
_CHANNEL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_ALL_CHANNEL_ROLES = frozenset({
    models.Role.COORDINATOR.value,
    models.Role.MONITOR.value,
    models.Role.NARRATOR.value,
})
_ROLE_WORKER_NAME_RE = re.compile(r"^(?:narrator|monitor)(?:-\d+)?$", re.IGNORECASE)
_TERMINAL_CLAIM_STATES = frozenset({
    models.MemberState.KILLED.value,
    models.MemberState.DONE.value,
    models.MemberState.FAILED.value,
    models.MemberState.CANCELED.value,
})


class ScopeConflictError(ValueError):
    """Raised when a scope claim conflicts with an active claim held by another agent."""


def _normalize_scope_key(raw: str) -> str:
    """Normalize a path/glob scope to forward slashes without leading `./`."""
    s = str(raw or "").strip().replace("\\", "/")
    while "//" in s:
        s = s.replace("//", "/")
    if s.startswith("./"):
        s = s[2:]
    return s


def _norm_scope_cmp(raw: str) -> str:
    return _normalize_scope_key(raw).rstrip("/").lower()


def _has_glob(s: str) -> bool:
    return any(ch in s for ch in ("*", "?", "["))


def _scopes_overlap(a: str, b: str) -> bool:
    """Return True if two normalized path/glob scopes overlap."""
    na = _norm_scope_cmp(a)
    nb = _norm_scope_cmp(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if not _has_glob(na) and not _has_glob(nb):
        return nb.startswith(na + "/") or na.startswith(nb + "/")
    if fnmatch.fnmatchcase(na, nb) or fnmatch.fnmatchcase(nb, na):
        return True
    # Check non-wildcard prefix overlap for globs like `src/*.py` vs `src/`
    pa = na.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0].rstrip("/")
    pb = nb.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0].rstrip("/")
    if pa and pb:
        return pa == pb or pb.startswith(pa + "/") or pa.startswith(pb + "/")
    return True


def _is_claim_expired(claim: dict[str, Any], members: dict[str, Any], now_dt: datetime) -> bool:
    exp = claim.get("expiresAt")
    if exp:
        try:
            exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if exp_dt <= now_dt:
                return True
        except ValueError:
            pass
    holder = str(claim.get("holder") or "").strip()
    if holder:
        _, mrec = find_member_ci(members, holder)
        if isinstance(mrec, dict):
            mstate = str(mrec.get("state") or "").strip().lower()
            if bool(mrec.get("killed")) or mstate in _TERMINAL_CLAIM_STATES:
                return True
    return False


def _reap_expired_claims(board: dict[str, Any]) -> list[str]:
    claims = board.get("claims")
    if not isinstance(claims, dict) or not claims:
        return []
    members = board.get("members") if isinstance(board.get("members"), dict) else {}
    now_dt = datetime.now(timezone.utc)
    reaped: list[str] = []
    for k in list(claims.keys()):
        v = claims.get(k)
        if not isinstance(v, dict) or _is_claim_expired(v, members, now_dt):
            claims.pop(k, None)
            reaped.append(k)
    return reaped


def _active_claims_view(board: dict[str, Any]) -> dict[str, Any]:
    claims = board.get("claims")
    if not isinstance(claims, dict) or not claims:
        return {}
    members = board.get("members") if isinstance(board.get("members"), dict) else {}
    now_dt = datetime.now(timezone.utc)
    return {
        k: v
        for k, v in claims.items()
        if isinstance(v, dict) and not _is_claim_expired(v, members, now_dt)
    }


def _advance_contiguous_last_read(
    member_name: str,
    member_rec: dict[str, Any],
    board_messages: list[Any],
    channels_map: dict[str, Any],
    *,
    head: int,
) -> int:
    """Advance `lastReadSeq` contiguously without skipping any earlier visible unread message."""
    cur_last = int(member_rec.get("lastReadSeq") or 0)
    _, unseen_seqs = compute_visible_unread(member_name, member_rec, board_messages, channels_map)
    if unseen_seqs:
        target = max(cur_last, min(unseen_seqs) - 1)
    else:
        target = max(cur_last, int(head))
    member_rec["lastReadSeq"] = target
    return target


def channel_display_name(raw: str | None) -> str:
    """Return UI/board display channel slug (`main` for `#general`/`main`, else stripped slug)."""
    norm = models.normalize_channel_name(raw)
    if norm == models.DEFAULT_CHANNEL:
        return "main"
    return norm.lstrip("#") or "main"


def _strip_code_for_mentions(text: str) -> str:
    """Strip fenced code blocks and inline backtick code spans before mention regex scanning."""
    if not text:
        return ""
    cleaned = _FENCED_CODE_RE.sub(" ", text)
    cleaned = _INLINE_CODE_RE.sub(" ", cleaned)
    return cleaned


def extract_mentions(text: str, explicit_to: list[str] | None = None) -> list[str]:
    """Extract unique `@name` targets (excluding `@#channel` mentions) preserving order."""
    found: list[str] = []
    seen_low: set[str] = set()
    for item in explicit_to or []:
        cleaned = str(item or "").strip().lstrip("@")
        if not cleaned or cleaned.startswith("#"):
            continue
        low = cleaned.lower()
        if low not in seen_low:
            seen_low.add(low)
            found.append(cleaned)
    scan_text = _strip_code_for_mentions(text or "")
    for match in _MENTION_RE.finditer(scan_text):
        cleaned = match.group(1).strip()
        low = cleaned.lower()
        if cleaned and low not in seen_low:
            seen_low.add(low)
            found.append(cleaned)
    return found


def extract_channel_mentions(text: str, explicit_to: list[str] | None = None) -> list[str]:
    """Extract unique `@#channel` targets normalized to `#slug` (ignoring `#main`/`#general`)."""
    found: list[str] = []
    seen: set[str] = set()
    candidates: list[str] = []
    for item in explicit_to or []:
        raw = str(item or "").strip()
        if raw.startswith("@#"):
            candidates.append(raw[2:].lower())
        elif raw.startswith("#"):
            candidates.append(raw[1:].lower())
    scan_text = _strip_code_for_mentions(text or "")
    for match in _CHANNEL_MENTION_RE.finditer(scan_text):
        candidates.append(match.group(1).lower())
    for slug in candidates:
        if slug in _RESERVED_CHANNELS or not _CHANNEL_SLUG_RE.match(slug):
            continue
        norm = f"#{slug}"
        if norm not in seen:
            seen.add(norm)
            found.append(norm)
    return found


def effective_member_channels(role: str | None, stored_channels: list[str] | None, name: str = "") -> list[str]:
    """Return effective normalized channel subscriptions (`['*']` for coordinator/monitor/narrator)."""
    if stored_channels is not None:
        raw_list = list(stored_channels)
        if "*" in raw_list:
            return ["*"]
        out: list[str] = []
        for ch in raw_list:
            norm = models.normalize_channel_name(str(ch))
            if norm not in out:
                out.append(norm)
        return out
    r = str(role or "").strip().lower()
    if r in _ALL_CHANNEL_ROLES or _ROLE_WORKER_NAME_RE.match(str(name or "").strip()):
        return ["*"]
    return [models.DEFAULT_CHANNEL]


def find_member_ci(members: dict[str, Any], target: str | None) -> tuple[str | None, dict[str, Any] | None]:
    """Find a member entry by exact or case-insensitive name."""
    clean = str(target or "").strip().lstrip("@")
    if not clean or not isinstance(members, dict):
        return None, None
    if clean in members and isinstance(members[clean], dict):
        return clean, members[clean]
    low = clean.lower()
    for mname, mrec in members.items():
        if mname.lower() == low and isinstance(mrec, dict):
            return mname, mrec
    return None, None


def _mention_matches_name(mention_tok: str, name_low: str) -> bool:
    ml = str(mention_tok or "").strip().lstrip("@").lower()
    if not ml or not name_low:
        return False
    if ml == "all" or ml == name_low:
        return True
    if "-" in name_low:
        prefix, _, suffix = name_low.rpartition("-")
        if suffix.isdigit() and ml == prefix:
            return True
    return False


def sees_in(
    name: str,
    eff_chs: list[str],
    msg: dict[str, Any],
    channels_map: dict[str, Any] | None = None,
) -> bool:
    """Return True if `name` with `eff_chs` can see `msg` (matching channels.mjs seesIn)."""
    ch = models.normalize_channel_name(msg.get("channel"))
    sender = str(msg.get("sender") or msg.get("agent") or "").strip()
    nlow = str(name or "").strip().lstrip("@").lower()
    if ch == models.DEFAULT_CHANNEL or (nlow and sender.lower() == nlow):
        return True
    if "*" in eff_chs or ch in eff_chs:
        return True
    # @mention or @all
    raw_mentions = msg.get("mentions")
    if raw_mentions is None:
        raw_mentions = extract_mentions(str(msg.get("summary") or msg.get("text") or ""))
    if any(_mention_matches_name(str(x), nlow) for x in (raw_mentions or [])):
        return True
    # @#channel mention on an active (non-archived) channel
    raw_ch_mentions = msg.get("channelMentions")
    if raw_ch_mentions is None:
        raw_ch_mentions = extract_channel_mentions(str(msg.get("summary") or msg.get("text") or ""))
    cmap = channels_map or {}
    for cm in raw_ch_mentions or []:
        norm_cm = models.normalize_channel_name(str(cm))
        if norm_cm == models.DEFAULT_CHANNEL:
            continue
        crec = cmap.get(norm_cm)
        if isinstance(crec, dict) and crec.get("archived"):
            continue
        if "*" in eff_chs or norm_cm in eff_chs:
            return True
    return False


def compute_visible_unread(
    name: str,
    member_rec: dict[str, Any] | None,
    msgs: list[dict[str, Any]],
    channels_map: dict[str, Any] | None = None,
) -> tuple[int, list[int]]:
    """Return `(visible_unread_count, unseen_seq_list)` for `name` using channels.mjs rules."""
    rec = member_rec or {}
    role = rec.get("role")
    stored_chs = rec.get("channels")
    eff_chs = effective_member_channels(role, stored_chs, name)
    last_read = int(rec.get("lastReadSeq") or 0)
    nlow = str(name or "").strip().lstrip("@").lower()
    unseen_seqs: list[int] = []
    for m in msgs:
        if not isinstance(m, dict) or isinstance(m.get("control"), dict):
            continue
        seq = int(m.get("seq") or 0)
        if seq <= last_read:
            continue
        sender_low = str(m.get("sender") or m.get("agent") or "").strip().lower()
        if nlow and sender_low == nlow:
            continue
        read_by = [str(r).strip().lower() for r in (m.get("readBy") or []) if str(r).strip()]
        if nlow and nlow in read_by:
            continue
        if sees_in(name, eff_chs, m, channels_map):
            unseen_seqs.append(seq)
    return len(unseen_seqs), unseen_seqs


def resolve_board_location(
    swarm_id: str | None = None,
    *,
    state_dir: Path | str | None = None,
    board_dir: Path | str | None = None,
) -> tuple[str, Path, Path]:
    """Resolve `(swarm_id, board_file, board_lock)` cleanly for state_dir or direct board_dir."""
    sid = store.sanitize_swarm_id(swarm_id or os.environ.get("HERMES_A2A_SWARM_ID")) or "default"
    if board_dir is not None:
        bdir = Path(store.normalize_local_path(str(board_dir)))
        if (bdir / "swarms").is_dir() and not (bdir / "board.json").is_file():
            paths = store.resolve_swarm_paths(sid, state_dir=bdir)
            return paths.swarm_id, paths.board_file, paths.board_lock
        return sid, bdir / "board.json", bdir / "board.lock"
    sdir = Path(store.normalize_local_path(str(state_dir))) if state_dir is not None else None
    paths = store.resolve_swarm_paths(sid, state_dir=sdir)
    return paths.swarm_id, paths.board_file, paths.board_lock


def read_archived_messages(board_file: Path) -> list[dict[str, Any]]:
    """Read archived messages from `board_archive.jsonl` sibling of `board_file`."""
    archive_file = board_file.with_name("board_archive.jsonl")
    if not archive_file.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with archive_file.open("r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    out.append(item)
    except OSError:
        return []
    return out


def count_archived_messages(board_file: Path) -> int:
    """Count non-empty lines in `board_archive.jsonl` without loading full payloads."""
    archive_file = board_file.with_name("board_archive.jsonl")
    if not archive_file.is_file():
        return 0
    count = 0
    try:
        with archive_file.open("r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                if raw_line.strip():
                    count += 1
    except OSError:
        return 0
    return count


def _resolve_screenshot_path(raw_path: str, base_ws: str | None = None) -> str | None:
    cleaned = str(raw_path or "").strip().strip("`\"'")
    if not cleaned:
        return None
    norm = store.normalize_local_path(cleaned)
    if not norm:
        return None
    p = Path(norm)
    if not p.is_absolute():
        if ".." in p.parts:
            return None
        if base_ws and str(base_ws).strip():
            ws_resolved = Path(store.normalize_local_path(base_ws)).resolve()
            try:
                cand = (ws_resolved / p).resolve()
                if not cand.is_relative_to(ws_resolved):
                    return None
                return str(cand)
            except OSError:
                return None
    return str(p)


def extract_screenshots(
    board: dict[str, Any],
    tasks: list[dict[str, Any]] | None = None,
    *,
    workspace_path: str | None = None,
    limit: int = MAX_SCREENSHOTS,
) -> list[dict[str, Any]]:
    """Extract `Screenshot: <path> | <caption>` frames from Tier-2 worker posts and task artifacts."""
    entries: list[dict[str, Any]] = []
    members_map = board.get("members") if isinstance(board.get("members"), dict) else {}
    sender_wt_map: dict[str, str] = {}
    for mname, mrec in members_map.items():
        if isinstance(mrec, dict) and mrec.get("worktreePath"):
            sender_wt_map[str(mname).strip().lower()] = str(mrec["worktreePath"])
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        t_meta = t.get("metadata") or {}
        t_wcfg = t_meta.get("workerConfig") or {}
        t_wn = str(t_wcfg.get("worker_name") or "").strip().lower()
        t_wt = t_meta.get("worktreePath") or t_wcfg.get("workdir")
        if t_wn and t_wt and t_wn not in sender_wt_map:
            sender_wt_map[t_wn] = str(t_wt)

    clamped_limit = max(1, int(limit))
    deduped: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    def _try_add(entry: dict[str, Any]) -> bool:
        raw_p = str(entry["path"])
        try:
            key = os.path.normcase(os.path.realpath(raw_p)) if os.path.isabs(raw_p) else os.path.normcase(raw_p)
        except OSError:
            key = os.path.normcase(raw_p)
        if key in seen_keys:
            return False
        seen_keys.add(key)
        deduped.append(entry)
        return len(deduped) >= clamped_limit

    msgs_rev = sorted(
        (m for m in (board.get("messages") or []) if isinstance(m, dict) and not isinstance(m.get("control"), dict)),
        key=lambda m: int(m.get("seq") or 0),
        reverse=True,
    )
    for m in msgs_rev:
        sender = str(m.get("sender") or "")
        srole = str(m.get("senderRole") or (members_map.get(sender) or {}).get("role") or "").lower()
        if srole in (models.Role.COORDINATOR.value, models.Role.MONITOR.value, models.Role.NARRATOR.value, models.Role.SUBAGENT.value):
            continue
        if _ROLE_WORKER_NAME_RE.match(sender):
            continue
        text = "\n".join(x for x in (str(m.get("summary") or ""), str(m.get("details") or "")) if x)
        if not text:
            continue
        seq = int(m.get("seq") or 0)
        ts = str(m.get("createdAt") or "")
        sender_wt = sender_wt_map.get(sender.strip().lower())
        bases = [b for b in (sender_wt, workspace_path) if b and str(b).strip()]
        done = False
        for match in _SCREENSHOT_RE.finditer(text):
            raw_shot = match.group(1)
            resolved: str | None = None
            first_resolved: str | None = None
            for base in bases:
                cand_res = _resolve_screenshot_path(raw_shot, base)
                if not cand_res:
                    continue
                if first_resolved is None:
                    first_resolved = cand_res
                if Path(cand_res).is_file():
                    resolved = cand_res
                    break
            if resolved is None:
                resolved = first_resolved or _resolve_screenshot_path(raw_shot, workspace_path)
            if not resolved:
                continue
            if _try_add({
                "path": resolved,
                "caption": str(match.group(2) or "").strip()[:90],
                "agent": sender or "worker",
                "seq": seq,
                "time": ts,
            }):
                done = True
                break
        if done:
            break

    if len(deduped) < clamped_limit:
        task_entries: list[dict[str, Any]] = []
        for t in tasks or []:
            if not isinstance(t, dict):
                continue
            meta = t.get("metadata") or {}
            wcfg = meta.get("workerConfig") or {}
            task_ws = meta.get("worktreePath") or wcfg.get("workdir") or workspace_path
            agent = str(wcfg.get("worker_name") or t.get("id") or "worker")
            t_time = str(((t.get("status") or {}).get("timestamp")) or meta.get("createdAt") or "")
            for art in t.get("artifacts") or []:
                if not isinstance(art, dict):
                    continue
                for part in art.get("parts") or []:
                    if not isinstance(part, dict) or part.get("text") is None:
                        continue
                    for match in _SCREENSHOT_RE.finditer(str(part["text"])):
                        resolved = _resolve_screenshot_path(match.group(1), task_ws)
                        if not resolved:
                            continue
                        task_entries.append({
                            "path": resolved,
                            "caption": str(match.group(2) or "").strip()[:90],
                            "agent": agent,
                            "seq": 0,
                            "time": t_time,
                        })
        task_entries.sort(key=lambda e: str(e.get("time") or ""), reverse=True)
        for entry in task_entries:
            if _try_add(entry):
                break

    deduped.reverse()
    return deduped


class BoardService:
    """Thread-safe, file-locked, event-driven v2 Swarm Board service."""

    def __init__(self, state_dir: Path | str | None = None) -> None:
        self._store = store.SwarmStore(state_dir)
        self._listeners: list[Callable[[str, str, dict[str, Any]], None]] = []
        self._wait_queues: dict[str, list[tuple[asyncio.Queue[dict[str, Any]], asyncio.AbstractEventLoop | None]]] = {}

    @property
    def state_dir(self) -> Path:
        return self._store.state_dir

    def add_listener(self, callback: Callable[[str, str, dict[str, Any]], None]) -> None:
        """Register an in-process listener `(swarm_id, event_type, payload)`."""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[str, str, dict[str, Any]], None]) -> None:
        self._listeners = [cb for cb in self._listeners if cb is not callback]

    def subscribe_waiter(self, swarm_id: str) -> asyncio.Queue[dict[str, Any]]:
        sid = store.sanitize_swarm_id(swarm_id) or "default"
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        self._wait_queues.setdefault(sid, []).append((q, loop))
        return q

    def unsubscribe_waiter(self, swarm_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        sid = store.sanitize_swarm_id(swarm_id) or "default"
        subs = self._wait_queues.get(sid)
        if not subs:
            return
        self._wait_queues[sid] = [item for item in subs if item[0] is not q]
        if not self._wait_queues[sid]:
            self._wait_queues.pop(sid, None)

    def _emit(self, swarm_id: str, event_type: str, payload: dict[str, Any]) -> None:
        sid = store.sanitize_swarm_id(swarm_id) or "default"
        for q, sub_loop in list(self._wait_queues.get(sid) or []):
            def _push(target_q: asyncio.Queue[dict[str, Any]] = q) -> None:
                try:
                    target_q.put_nowait({"event": event_type, **payload})
                except asyncio.QueueFull:
                    pass

            if sub_loop is not None and not sub_loop.is_closed() and sub_loop.is_running():
                try:
                    cur = asyncio.get_running_loop()
                except RuntimeError:
                    cur = None
                if cur is sub_loop:
                    _push()
                else:
                    try:
                        sub_loop.call_soon_threadsafe(_push)
                    except RuntimeError:
                        pass
            else:
                _push()

        for cb in list(self._listeners):
            try:
                cb(sid, event_type, payload)
            except Exception:
                pass

    @contextmanager
    def _locked_board(
        self,
        swarm_id: str | None = None,
        *,
        board_dir: Path | str | None = None,
        mutate: bool = False,
    ) -> Iterator[tuple[str, dict[str, Any], Path]]:
        sid, bfile, block = resolve_board_location(
            swarm_id,
            state_dir=self._store._state_dir,
            board_dir=board_dir,
        )
        if not mutate and not block.parent.is_dir() and not bfile.is_file():
            with store.get_swarm_thread_lock(sid):
                yield sid, store.migrate_board_v1_to_v2(None, sid), bfile
                return
        with store.get_swarm_thread_lock(sid), store.atomic_file_lock(block):
            raw = store.read_json_safe(bfile, None, raise_on_error=mutate)
            board = store.migrate_board_v1_to_v2(raw, sid)
            yield sid, board, bfile
            if mutate:
                msgs = list(board.get("messages") or [])
                if len(msgs) > MAX_BOARD_MESSAGES:
                    overflow = msgs[:-MAX_BOARD_MESSAGES]
                    archive_file = bfile.with_name("board_archive.jsonl")
                    try:
                        with archive_file.open("a", encoding="utf-8") as afh:
                            for om in overflow:
                                afh.write(json.dumps(om, ensure_ascii=False) + "\n")
                        store.append_swarm_log(
                            sid,
                            "board",
                            {"event": "archive_append", "count": len(overflow), "archiveFile": str(archive_file)},
                            state_dir=self._store._state_dir,
                        )
                    except OSError as exc:
                        store.append_swarm_log(
                            sid,
                            "board",
                            {"event": "archive_write_error", "count": len(overflow), "error": str(exc)},
                            state_dir=self._store._state_dir,
                        )
                    board["messages"] = msgs[-MAX_BOARD_MESSAGES:]
                board["schemaVersion"] = models.SCHEMA_VERSION
                board["swarmId"] = sid
                board["updatedAt"] = models.now_iso()
                store.atomic_write_json(bfile, board)

    def read_board(
        self,
        swarm_id: str = "default",
        *,
        board_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        sid, bfile, _ = resolve_board_location(
            swarm_id,
            state_dir=self._store._state_dir,
            board_dir=board_dir,
        )
        raw = store.read_json_safe(bfile, None)
        return store.migrate_board_v1_to_v2(raw, sid)

    def head_seq(self, board: dict[str, Any]) -> int:
        msgs = board.get("messages") or []
        max_msg_seq = max((int(m.get("seq") or 0) for m in msgs if isinstance(m, dict)), default=0)
        next_seq = int(board.get("nextSeq") or 1)
        return max(max_msg_seq, max(0, next_seq - 1))

    def post_message(
        self,
        *,
        sender: str,
        summary: str,
        msg_type: str = models.MessageType.POST.value,
        channel: str = models.DEFAULT_CHANNEL,
        to: list[str] | None = None,
        details: str = "",
        files: list[str] | None = None,
        refs: list[dict[str, Any]] | None = None,
        reply_to: int | str | None = None,
        thread_id: str | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        sender_role: str | None = None,
        control: dict[str, Any] | None = None,
        swarm_id: str | None = None,
        board_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        """Atomically append a v2 message to `board.json` and emit board notification."""
        clean_summary = str(summary or "").strip()
        if not clean_summary:
            raise ValueError("Board message `summary` must be non-empty.")

        clean_sender = str(sender or "worker").strip() or "worker"
        sender_low = clean_sender.lower()
        if sender_role:
            resolved_role = models.normalize_role(sender_role)
        elif sender_low == models.Role.PRIMARY.value:
            resolved_role = models.Role.PRIMARY.value
        elif sender_low == models.Role.COORDINATOR.value:
            resolved_role = models.Role.COORDINATOR.value
        else:
            resolved_role = models.Role.WORKER.value

        if resolved_role == models.Role.PRIMARY.value:
            sender_tier = 1
        elif resolved_role == models.Role.SUBAGENT.value:
            sender_tier = 3
        else:
            sender_tier = 2

        norm_ch = models.normalize_channel_name(channel)
        mentions = extract_mentions(clean_summary, explicit_to=to) if control is None else []
        chan_mentions = extract_channel_mentions(clean_summary, explicit_to=to) if control is None else []
        raw_kind = str(msg_type or "").strip().lower()
        norm_type = models.normalize_message_type(msg_type)
        if raw_kind == "approval_request" and norm_type == models.MessageType.POST.value:
            norm_type = models.MessageType.QUESTION.value
        elif raw_kind == "handoff_ready" and norm_type == models.MessageType.POST.value:
            norm_type = models.MessageType.HANDOFF.value

        created_msg: dict[str, Any] = {}
        resolved_sid = "default"
        with self._locked_board(swarm_id, board_dir=board_dir, mutate=True) as (sid, board, _):
            resolved_sid = sid
            channels = board.setdefault("channels", {})
            existing_ch = channels.get(norm_ch)
            if isinstance(existing_ch, dict) and existing_ch.get("archived"):
                raise ValueError(f"Channel '{channel_display_name(norm_ch)}' is archived.")

            if norm_ch != models.DEFAULT_CHANNEL and norm_ch not in channels:
                raise ValueError(
                    f"Channel '#{channel_display_name(norm_ch)}' does not exist. Create it first with channel_action(op='create')."
                )

            seq = max(int(board.get("nextSeq") or 1), self.head_seq(board) + 1)
            board["nextSeq"] = seq + 1

            members = board.setdefault("members", {})
            sender_member_key, sender_member_rec = find_member_ci(members, clean_sender)
            if sender_member_key is not None and sender_member_rec is not None:
                sender_killed = (
                    bool(sender_member_rec.get("killed"))
                    or str(sender_member_rec.get("state") or "").strip().lower() == models.MemberState.KILLED.value
                )
                if sender_killed and control is None:
                    raise PermissionError(f"Member '{clean_sender}' is killed and cannot post to the board.")
                resolved_role = models.normalize_role(
                    sender_role or sender_member_rec.get("role"),
                    default=resolved_role,
                )
                if resolved_role == models.Role.PRIMARY.value:
                    sender_tier = 1
                elif resolved_role == models.Role.SUBAGENT.value:
                    sender_tier = 3
                else:
                    sender_tier = 2
                sender_member_rec["lastPostSeq"] = max(int(sender_member_rec.get("lastPostSeq") or 0), seq)
                prev_last_read = int(sender_member_rec.get("lastReadSeq") or 0)
                eff_sender_chs = effective_member_channels(
                    resolved_role,
                    sender_member_rec.get("channels"),
                    sender_member_key,
                )
                sender_low = sender_member_key.lower()
                has_earlier_unread = any(
                    isinstance(m, dict)
                    and not isinstance(m.get("control"), dict)
                    and prev_last_read < int(m.get("seq") or 0) < seq
                    and str(m.get("sender") or m.get("agent") or "").strip().lower() != sender_low
                    and sender_low not in [str(r).strip().lower() for r in (m.get("readBy") or []) if str(r).strip()]
                    and sees_in(sender_member_key, eff_sender_chs, m, channels)
                    for m in (board.get("messages") or [])
                )
                if not has_earlier_unread:
                    sender_member_rec["lastReadSeq"] = max(prev_last_read, seq)
                sender_member_rec["lastHeartbeatAt"] = models.now_iso()
                sender_member_rec["updatedAt"] = models.now_iso()

            msg = models.new_board_message(
                seq=seq,
                sender=clean_sender,
                summary=clean_summary,
                channel=norm_ch,
                channels=[norm_ch],
                msg_type=norm_type,
                sender_role=resolved_role,
                sender_tier=sender_tier,
                mentions=mentions,
                channel_mentions=chan_mentions,
                reply_to=reply_to,
                thread_id=thread_id,
                task_id=task_id,
                context_id=context_id,
                details=str(details or "").strip(),
                files=[str(f).strip() for f in (files or []) if str(f).strip()],
                refs=list(refs or []),
                status="open",
            )
            if raw_kind in ("approval_request", "handoff_ready", "blocker"):
                msg["kind"] = raw_kind
            if control is not None:
                msg["control"] = dict(control)
            board.setdefault("messages", []).append(msg)
            created_msg = msg

        self._emit(resolved_sid, "board_post", {"message": created_msg, "seq": created_msg["seq"]})
        return created_msg

    def read_messages(
        self,
        *,
        swarm_id: str | None = None,
        since: int | None = None,
        seqs: list[int] | None = None,
        status: bool = False,
        agent: str | None = None,
        channel: str | None = None,
        mention: str | None = None,
        sender: str | None = None,
        msg_type: str | None = None,
        limit: int = 100,
        mark_read_by: str | None = None,
        include_control: bool = True,
        include_archive: bool = False,
        before_seq: int | None = None,
        search: str | None = None,
        full: bool = False,
        board_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        """Read board messages oldest-first with optional filters, archive merge, search, and read-cursor update."""
        if status:
            return self.get_status(swarm_id=swarm_id, board_dir=board_dir)

        max_limit = 2000 if full else 500
        clamped = max(1, min(max_limit, int(limit)))
        ch_raw = str(channel).strip() if channel is not None else ""
        is_all_channels = ch_raw == "*"
        norm_ch = models.normalize_channel_name(ch_raw) if (ch_raw and not is_all_channels) else None
        norm_type = models.normalize_message_type(msg_type) if msg_type else None
        mention_low = mention.strip().lstrip("@").lower() if mention else None
        sender_low = sender.strip().lower() if sender else None
        search_q = search.strip().lower() if search and str(search).strip() else None
        seq_set = {int(s) for s in seqs if int(s) > 0} if seqs is not None else None
        reader_agent = str(agent or mark_read_by or "").strip()

        effective_since: int | None = int(since) if since is not None else None
        if effective_since == 0 and before_seq is None and not full:
            effective_since = None
        before_val: int | None = int(before_seq) if before_seq is not None else None

        should_mutate = bool(mark_read_by and str(mark_read_by).strip())
        with self._locked_board(swarm_id, board_dir=board_dir, mutate=should_mutate) as (sid, board, bfile):
            live_msgs = [m for m in (board.get("messages") or []) if isinstance(m, dict)]
            if include_archive:
                archived_msgs = read_archived_messages(bfile)
                by_seq: dict[int, dict[str, Any]] = {}
                extra_msgs: list[dict[str, Any]] = []
                for am in archived_msgs:
                    aseq = int(am.get("seq") or 0)
                    if aseq > 0:
                        by_seq[aseq] = am
                    else:
                        extra_msgs.append(am)
                for lm in live_msgs:
                    lseq = int(lm.get("seq") or 0)
                    if lseq > 0:
                        by_seq[lseq] = lm
                    else:
                        extra_msgs.append(lm)
                msgs = [*by_seq.values(), *extra_msgs]
            else:
                msgs = live_msgs
            msgs.sort(key=lambda m: int(m.get("seq") or 0))
            head = self.head_seq(board)
            channels_map = board.get("channels") or {}
            members = board.setdefault("members", {})

            if seq_set is not None:
                selected = [m for m in msgs if int(m.get("seq") or 0) in seq_set]
            else:
                filtered = msgs
                if effective_since is not None:
                    filtered = [m for m in filtered if int(m.get("seq") or 0) > effective_since]
                if before_val is not None:
                    filtered = [m for m in filtered if int(m.get("seq") or 0) < before_val]
                if not include_control:
                    filtered = [m for m in filtered if not isinstance(m.get("control"), dict)]
                if norm_ch:
                    filtered = [m for m in filtered if models.normalize_channel_name(m.get("channel")) == norm_ch]
                elif not is_all_channels and reader_agent:
                    _, mrec = find_member_ci(members, reader_agent)
                    eff_chs = effective_member_channels(
                        (mrec or {}).get("role"),
                        (mrec or {}).get("channels"),
                        reader_agent,
                    )
                    filtered = [m for m in filtered if sees_in(reader_agent, eff_chs, m, channels_map)]
                if sender_low:
                    filtered = [m for m in filtered if str(m.get("sender") or "").lower() == sender_low]
                if norm_type:
                    filtered = [m for m in filtered if models.normalize_message_type(m.get("type")) == norm_type]
                if mention_low:
                    def _matches_mention(m: dict[str, Any]) -> bool:
                        for x in (m.get("mentions") or []):
                            xl = str(x).strip().lstrip("@").lower()
                            if mention_low == "all":
                                if xl == "all":
                                    return True
                            elif _mention_matches_name(xl, mention_low) or _mention_matches_name(mention_low, xl):
                                return True
                        return False
                    filtered = [m for m in filtered if _matches_mention(m)]
                if search_q:
                    filtered = [
                        m for m in filtered
                        if search_q in str(m.get("summary") or "").lower()
                        or search_q in str(m.get("details") or "").lower()
                        or search_q in str(m.get("sender") or "").lower()
                    ]

                if effective_since is None:
                    selected = filtered[-clamped:]
                else:
                    selected = filtered[:clamped]

            if should_mutate:
                reader = str(mark_read_by).strip()
                reader_low = reader.lower()
                selected_seqs = {int(m.get("seq") or 0) for m in selected}
                for m in board.get("messages") or []:
                    if int(m.get("seq") or 0) in selected_seqs:
                        rb = m.setdefault("readBy", [])
                        if reader_low not in {str(r).strip().lower() for r in rb}:
                            rb.append(reader)
                reader_key, reader_rec = find_member_ci(members, reader)
                if reader_key is not None and reader_rec is not None:
                    _advance_contiguous_last_read(
                        reader_key,
                        reader_rec,
                        board.get("messages") or [],
                        channels_map,
                        head=head,
                    )
                    reader_rec["updatedAt"] = models.now_iso()

            result: dict[str, Any] = {
                "swarmId": sid,
                "head": head,
                "count": len(selected),
                "messages": selected,
            }
            if reader_agent:
                _, r_rec = find_member_ci(members, reader_agent)
                vis_cnt, unseen_seqs = compute_visible_unread(reader_agent, r_rec, msgs, channels_map)
                result["visibleUnread"] = vis_cnt
                result["unseen"] = unseen_seqs
            return result

    def channel_action(
        self,
        op: str,
        *,
        channel: str | None = None,
        topic: str = "",
        description: str = "",
        members: list[str] | None = None,
        actor: str = "coordinator",
        swarm_id: str | None = None,
        board_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        """Manage channels (`create|subscribe|unsubscribe|list|archive`) with control messages."""
        clean_op = str(op or "list").strip().lower()
        if clean_op == "list":
            with self._locked_board(swarm_id, board_dir=board_dir, mutate=False) as (sid, board, _):
                msgs = [m for m in (board.get("messages") or []) if isinstance(m, dict)]
                out: list[dict[str, Any]] = []
                for ch_key, rec in sorted((board.get("channels") or {}).items()):
                    if not isinstance(rec, dict):
                        continue
                    count = sum(1 for m in msgs if models.normalize_channel_name(m.get("channel")) == ch_key)
                    out.append({
                        **rec,
                        "displayName": channel_display_name(ch_key),
                        "messageCount": count,
                    })
                return {"swarmId": sid, "channels": out}

        if not channel or not str(channel).strip():
            raise ValueError(f"Channel op '{clean_op}' requires a non-empty `channel` name.")
        norm_ch = models.normalize_channel_name(channel)
        disp = channel_display_name(norm_ch)
        if clean_op in ("create", "archive") and disp == "main":
            raise ValueError("Cannot create or archive the primary channel.")

        target_members = [str(m).strip().lstrip("@") for m in (members or []) if str(m).strip()]
        ts = models.now_iso()
        result_payload: dict[str, Any] = {}
        control_msg: dict[str, Any] | None = None
        resolved_sid = "default"

        with self._locked_board(swarm_id, board_dir=board_dir, mutate=True) as (sid, board, _):
            resolved_sid = sid
            channels = board.setdefault("channels", {})
            board_members = board.setdefault("members", {})

            if clean_op == "create":
                existing = channels.get(norm_ch) if isinstance(channels.get(norm_ch), dict) else None
                rec = models.new_channel_record(
                    norm_ch,
                    description=description or topic or (existing or {}).get("description", ""),
                    topic=topic or description or (existing or {}).get("topic", ""),
                    subscribers=list((existing or {}).get("subscribers") or []),
                    archived=False,
                )
                for mname in target_members:
                    if mname not in rec["subscribers"]:
                        rec["subscribers"].append(mname)
                    if mname in board_members and isinstance(board_members[mname], dict):
                        mchs = list(board_members[mname].get("channels") or [models.DEFAULT_CHANNEL])
                        if norm_ch not in mchs:
                            mchs.append(norm_ch)
                        board_members[mname]["channels"] = mchs
                        board_members[mname]["updatedAt"] = ts
                rec["createdBy"] = (existing or {}).get("createdBy") or actor
                channels[norm_ch] = rec
                seq = max(int(board.get("nextSeq") or 1), self.head_seq(board) + 1)
                board["nextSeq"] = seq + 1
                control_msg = models.new_board_message(
                    seq=seq,
                    sender=actor,
                    summary=f"Created channel #{disp}" + (f": {rec['topic']}" if rec["topic"] else ""),
                    channel=norm_ch,
                    msg_type=models.MessageType.SYSTEM.value,
                    sender_role=models.Role.COORDINATOR.value if actor == "coordinator" else models.Role.WORKER.value,
                    sender_tier=2,
                )
                control_msg["control"] = {"op": "create", "channel": disp, "topic": rec["topic"]}
                board.setdefault("messages", []).append(control_msg)
                result_payload = {"channel": rec, "controlMessage": control_msg}

            elif clean_op in ("subscribe", "unsubscribe"):
                if not target_members:
                    raise ValueError(f"Channel op '{clean_op}' requires `members`.")
                rec = channels.get(norm_ch)
                if not isinstance(rec, dict):
                    if clean_op == "unsubscribe":
                        raise KeyError(f"Channel '{norm_ch}' does not exist.")
                    rec = models.new_channel_record(norm_ch, description=description, topic=topic)
                    channels[norm_ch] = rec
                if rec.get("archived") and clean_op == "subscribe":
                    raise ValueError(f"Channel '{norm_ch}' is archived.")

                prev_map: dict[str, list[str] | None] = {}
                next_map: dict[str, list[str] | None] = {}
                prev_subs = list(rec.get("subscribers") or [])
                subs = list(prev_subs)
                for mname in target_members:
                    _, mem = find_member_ci(board_members, mname)
                    prev_raw = mem.get("channels") if isinstance(mem, dict) else None
                    prev_map[mname] = (
                        None
                        if prev_raw is None
                        else [channel_display_name(c) if c != "*" else "*" for c in prev_raw]
                    )
                    if clean_op == "subscribe":
                        cur_chs = list(prev_raw or [])
                        if norm_ch not in cur_chs and "*" not in cur_chs:
                            cur_chs.append(norm_ch)
                        if mname.lower() not in {str(s).strip().lower() for s in subs}:
                            subs.append(mname)
                    else:
                        cur_chs_list = [
                            c for c in (prev_raw or [])
                            if models.normalize_channel_name(c) != norm_ch
                        ]
                        cur_chs = cur_chs_list if cur_chs_list else None
                        subs = [s for s in subs if str(s).strip().lower() != mname.lower()]
                    next_map[mname] = (
                        None
                        if cur_chs is None
                        else [channel_display_name(c) if c != "*" else "*" for c in cur_chs]
                    )
                    if isinstance(mem, dict):
                        mem["channels"] = cur_chs
                        mem["updatedAt"] = ts
                rec["subscribers"] = subs
                rec["updatedAt"] = ts
                if prev_map != next_map or subs != prev_subs:
                    seq = max(int(board.get("nextSeq") or 1), self.head_seq(board) + 1)
                    board["nextSeq"] = seq + 1
                    verb = "Subscribed" if clean_op == "subscribe" else "Unsubscribed"
                    control_msg = models.new_board_message(
                        seq=seq,
                        sender=actor,
                        summary=f"{verb} {', '.join('@' + m for m in target_members)} {'to' if clean_op == 'subscribe' else 'from'} #{disp}",
                        channel=norm_ch,
                        msg_type=models.MessageType.SYSTEM.value,
                        sender_role=models.Role.COORDINATOR.value if actor == "coordinator" else models.Role.WORKER.value,
                        sender_tier=2,
                    )
                    control_msg["control"] = {
                        "op": clean_op,
                        "channel": disp,
                        "members": target_members,
                        "prev": prev_map,
                        "next": next_map,
                    }
                    board.setdefault("messages", []).append(control_msg)
                result_payload = {"channel": rec, "controlMessage": control_msg}

            elif clean_op == "archive":
                rec = channels.get(norm_ch)
                if not isinstance(rec, dict):
                    raise KeyError(f"Channel '{norm_ch}' does not exist.")
                rec["archived"] = True
                rec["updatedAt"] = ts
                seq = max(int(board.get("nextSeq") or 1), self.head_seq(board) + 1)
                board["nextSeq"] = seq + 1
                control_msg = models.new_board_message(
                    seq=seq,
                    sender=actor,
                    summary=f"Archived channel #{disp}",
                    channel=norm_ch,
                    msg_type=models.MessageType.SYSTEM.value,
                    sender_role=models.Role.COORDINATOR.value if actor == "coordinator" else models.Role.WORKER.value,
                    sender_tier=2,
                )
                control_msg["control"] = {"op": "archive", "channel": disp}
                board.setdefault("messages", []).append(control_msg)
                result_payload = {"channel": rec, "controlMessage": control_msg}
            else:
                raise ValueError(f"Unsupported channel op: {op}")

            if control_msg is not None:
                actor_key, actor_rec = find_member_ci(board_members, actor)
                if actor_rec is not None:
                    cseq = int(control_msg["seq"])
                    actor_rec["lastPostSeq"] = max(int(actor_rec.get("lastPostSeq") or 0), cseq)
                    prev_last_read = int(actor_rec.get("lastReadSeq") or 0)
                    eff_actor_chs = effective_member_channels(
                        actor_rec.get("role") or (models.Role.COORDINATOR.value if actor == "coordinator" else models.Role.WORKER.value),
                        actor_rec.get("channels"),
                        actor_key or actor,
                    )
                    actor_low = (actor_key or actor).lower()
                    has_earlier_unread = any(
                        isinstance(m, dict)
                        and not isinstance(m.get("control"), dict)
                        and prev_last_read < int(m.get("seq") or 0) < cseq
                        and str(m.get("sender") or m.get("agent") or "").strip().lower() != actor_low
                        and actor_low not in [str(r).strip().lower() for r in (m.get("readBy") or []) if str(r).strip()]
                        and sees_in(actor_key or actor, eff_actor_chs, m, channels)
                        for m in (board.get("messages") or [])
                    )
                    if not has_earlier_unread:
                        actor_rec["lastReadSeq"] = max(prev_last_read, cseq)
                    actor_rec["lastHeartbeatAt"] = ts
                    actor_rec["updatedAt"] = ts

        if control_msg is not None:
            self._emit(resolved_sid, "channel_updated", {"op": clean_op, "channel": disp, "seq": control_msg["seq"]})
        return {"swarmId": resolved_sid, **result_payload}

    def upsert_member(
        self,
        name: str,
        *,
        role: str = models.Role.WORKER.value,
        tier: int = 2,
        runtime: str = "hermes",
        state: str = models.MemberState.WORKING.value,
        assignment: str | None = None,
        channels: list[str] | None = None,
        parent: str | None = "coordinator",
        parent_task_id: str | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        session_id: str | None = None,
        conversation_id: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        workdir: str | None = None,
        worktree_path: str | None = None,
        worktree_branch: str | None = None,
        wait_since: str | None = None,
        killed: bool | None = None,
        swarm_id: str | None = None,
        board_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        """Register or update a board member preserving existing cursors and channels unless overridden."""
        key = str(name or "").strip() or "worker"
        ts = models.now_iso()
        resolved_sid = "default"
        entry_out: dict[str, Any] = {}

        with self._locked_board(swarm_id, board_dir=board_dir, mutate=True) as (sid, board, _):
            resolved_sid = sid
            members = board.setdefault("members", {})
            prev = dict(members.get(key) or {})
            eff_role = models.normalize_role(role or prev.get("role"))
            eff_channels = channels if channels is not None else prev.get("channels")
            if eff_role in _ALL_CHANNEL_ROLES:
                eff_channels = ["*"]

            prev_killed = (
                bool(prev.get("killed", False))
                or str(prev.get("state") or "").strip().lower() == models.MemberState.KILLED.value
            )
            if prev_killed and killed is not False:
                eff_killed = True
                eff_state = models.MemberState.KILLED.value
            else:
                eff_killed = bool(killed) if killed is not None else False
                eff_state = (
                    models.MemberState.KILLED.value
                    if eff_killed
                    else (state or prev.get("state") or models.MemberState.WAITING.value)
                )

            entry = models.new_board_member(
                key,
                role=eff_role,
                tier=tier if prev.get("tier") is None else int(tier or prev["tier"]),
                runtime=runtime or prev.get("runtime") or "hermes",
                state=eff_state,
                assignment=(assignment[:280] if assignment is not None else str(prev.get("assignment") or "")),
                channels=eff_channels if eff_channels != ["*"] else None,
                parent=parent if parent is not None else prev.get("parent"),
                parent_task_id=parent_task_id if parent_task_id is not None else prev.get("parentTaskId"),
                task_id=task_id if task_id is not None else prev.get("taskId"),
                context_id=context_id if context_id is not None else prev.get("contextId"),
                session_id=session_id if session_id is not None else prev.get("sessionId"),
                conversation_id=conversation_id if conversation_id is not None else prev.get("conversationId"),
                model=model if model is not None else prev.get("model"),
                provider=provider if provider is not None else prev.get("provider"),
                workdir=workdir if workdir is not None else prev.get("workdir"),
                worktree_path=worktree_path if worktree_path is not None else prev.get("worktreePath"),
                worktree_branch=worktree_branch if worktree_branch is not None else prev.get("worktreeBranch"),
                wait_since=wait_since if wait_since is not None else prev.get("waitSince"),
                last_read_seq=int(prev.get("lastReadSeq") or 0),
                last_post_seq=int(prev.get("lastPostSeq") or 0),
                killed=eff_killed,
                subagents=list(prev.get("subagents") or []),
            )
            if eff_channels == ["*"]:
                entry["channels"] = ["*"]
            entry["lastHeartbeatAt"] = ts
            entry["updatedAt"] = ts
            members[key] = entry
            entry_out = entry

            # Ensure channel subscriber lists reflect explicit member channels.
            if channels is not None and eff_channels != ["*"]:
                ch_map = board.setdefault("channels", {})
                for ch in entry["channels"]:
                    ch_rec = ch_map.setdefault(ch, models.new_channel_record(ch))
                    subs = ch_rec.setdefault("subscribers", [])
                    if key not in subs:
                        subs.append(key)

        self._emit(resolved_sid, "member_updated", {"member": key, "state": entry_out.get("state")})
        return entry_out

    def set_member_wait(
        self,
        name: str,
        *,
        waiting: bool,
        last_read_seq: int | None = None,
        read_seqs: list[int] | None = None,
        waiter_id: str | None = None,
        advance_contiguous: bool = False,
        swarm_id: str | None = None,
        board_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        key = str(name or "").strip() or "coordinator"
        ts = models.now_iso()
        with self._locked_board(swarm_id, board_dir=board_dir, mutate=True) as (_, board, _):
            members = board.setdefault("members", {})
            m_key, mem = find_member_ci(members, key)
            if m_key is not None and mem is not None:
                key = m_key
            if not isinstance(mem, dict):
                mem = models.new_board_member(
                    key,
                    role=models.Role.COORDINATOR.value if key.lower() == "coordinator" else models.Role.WORKER.value,
                    runtime="antigravity" if key.lower() == "coordinator" else "hermes",
                )
                members[key] = mem
            head = self.head_seq(board)
            is_killed = (
                bool(mem.get("killed"))
                or str(mem.get("state") or "").strip().lower() == models.MemberState.KILLED.value
            )
            if is_killed:
                mem["state"] = models.MemberState.KILLED.value
                mem["killed"] = True
                mem["waitSince"] = None
                mem["waiterId"] = None
            elif waiting:
                mem["state"] = models.MemberState.WAITING.value
                mem["waitSince"] = str(head)
                if waiter_id is not None:
                    mem["waiterId"] = waiter_id
            else:
                cur_wid = mem.get("waiterId")
                if waiter_id is None or not cur_wid or cur_wid == waiter_id:
                    mem["state"] = models.MemberState.WORKING.value
                    mem["waitSince"] = None
                    mem["waiterId"] = None
            if read_seqs:
                key_low = key.lower()
                seq_set = {int(s) for s in read_seqs if int(s) > 0}
                for m in board.get("messages") or []:
                    if isinstance(m, dict) and int(m.get("seq") or 0) in seq_set:
                        rb = m.setdefault("readBy", [])
                        if key_low not in {str(r).strip().lower() for r in rb}:
                            rb.append(key)
            if advance_contiguous:
                _advance_contiguous_last_read(
                    key,
                    mem,
                    board.get("messages") or [],
                    board.get("channels") or {},
                    head=head,
                )
            elif last_read_seq is not None:
                mem["lastReadSeq"] = max(int(mem.get("lastReadSeq") or 0), int(last_read_seq))
            mem["lastHeartbeatAt"] = ts
            mem["updatedAt"] = ts
            return dict(mem)

    def mark_member_read(
        self,
        name: str,
        seq: int,
        *,
        swarm_id: str | None = None,
        board_dir: Path | str | None = None,
    ) -> int:
        """Advance `members[name].lastReadSeq` monotonically without altering member state."""
        target = str(name or "").strip()
        target_seq = max(0, int(seq or 0))
        if not target or target_seq <= 0:
            return 0
        ts = models.now_iso()
        with self._locked_board(swarm_id, board_dir=board_dir, mutate=True) as (_, board, _):
            members = board.setdefault("members", {})
            mem = members.get(target)
            if not isinstance(mem, dict):
                tlow = target.lower()
                for mname, mrec in members.items():
                    if mname.lower() == tlow and isinstance(mrec, dict):
                        mem = mrec
                        break
            if not isinstance(mem, dict):
                return 0
            cur = int(mem.get("lastReadSeq") or 0)
            updated = max(cur, target_seq)
            if updated != cur:
                mem["lastReadSeq"] = updated
                mem["updatedAt"] = ts
            return updated

    def upsert_subagents(
        self,
        parent_worker: str,
        records: list[dict[str, Any]],
        *,
        swarm_id: str | None = None,
        board_dir: Path | str | None = None,
    ) -> None:
        """Persist read-only Tier-3 leaf subagent metadata under `board.json.subagents` and parent member."""
        p_key = str(parent_worker or "").strip()
        if not p_key or not records:
            return
        with self._locked_board(swarm_id, board_dir=board_dir, mutate=True) as (sid, board, _):
            sub_map = board.setdefault("subagents", {})
            bucket = sub_map.setdefault(p_key, [])
            by_id = {str(x.get("id")): dict(x) for x in bucket if isinstance(x, dict) and x.get("id")}
            for rec in records:
                rid = str(rec.get("id") or "")
                if rid:
                    by_id[rid] = dict(rec)
            merged = list(by_id.values())[-40:]
            sub_map[p_key] = merged
            mem = (board.get("members") or {}).get(p_key)
            if isinstance(mem, dict):
                ts_now = models.now_iso()
                mem["subagents"] = merged
                mem["lastHeartbeatAt"] = ts_now
                mem["updatedAt"] = ts_now
        # Tier-3 records update observer state only — never emit coordinator_wake or primary_wake.
        self._emit(sid, "subagents_updated", {"parentWorker": p_key, "count": len(records)})

    def claim_scope(
        self,
        *,
        scope: str,
        holder: str,
        notes: str = "",
        task_id: str | None = None,
        worktree_path: str | None = None,
        force: bool = False,
        ttl_seconds: float | None = 3600.0,
        swarm_id: str | None = None,
        board_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        """Claim an editing scope or path lock for a holder, rejecting active overlaps unless `force=True`."""
        clean_scope = _normalize_scope_key(scope)
        clean_holder = str(holder or "").strip()
        if not clean_scope:
            raise ValueError("`scope` must be non-empty.")
        if not clean_holder:
            raise ValueError("`holder` must be non-empty.")

        resolved_sid = "default"
        conflict: dict[str, Any] | None = None
        entry: dict[str, Any] = {}
        with self._locked_board(swarm_id, board_dir=board_dir, mutate=True) as (sid, board, _):
            resolved_sid = sid
            claims = board.setdefault("claims", {})
            _reap_expired_claims(board)
            holder_low = clean_holder.lower()
            for existing_scope, prev in list(claims.items()):
                if not isinstance(prev, dict):
                    continue
                prev_holder = str(prev.get("holder") or "").strip()
                if prev_holder.lower() == holder_low:
                    continue
                if _scopes_overlap(clean_scope, existing_scope):
                    if not force:
                        raise ScopeConflictError(
                            f"Scope '{clean_scope}' conflicts with active claim '{existing_scope}' held by '{prev_holder}'"
                        )
                    if conflict is None:
                        conflict = dict(prev)
            entry = models.new_scope_claim(
                clean_scope,
                holder=clean_holder,
                task_id=task_id,
                worktree_path=worktree_path,
                notes=notes.strip(),
                ttl_seconds=ttl_seconds,
            )
            claims[clean_scope] = entry

        self._emit(resolved_sid, "claim_updated", {"scope": clean_scope, "holder": clean_holder})
        return {"swarmId": resolved_sid, "claimed": entry, "conflictWarning": conflict}

    def release_scope(
        self,
        *,
        scope: str | None = None,
        holder: str | None = None,
        swarm_id: str | None = None,
        board_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        """Release one scope or all scopes held by `holder`."""
        clean_scope = _normalize_scope_key(scope) if scope else None
        clean_holder = str(holder).strip() if holder else None
        holder_low = clean_holder.lower() if clean_holder else None
        removed: list[str] = []
        resolved_sid = "default"
        with self._locked_board(swarm_id, board_dir=board_dir, mutate=True) as (sid, board, _):
            resolved_sid = sid
            claims = board.setdefault("claims", {})
            if clean_scope:
                match_keys = [
                    k for k in list(claims.keys())
                    if k == clean_scope or _norm_scope_cmp(k) == _norm_scope_cmp(clean_scope)
                ]
                for k in match_keys:
                    cur = claims.get(k)
                    cur_holder_low = str(cur.get("holder") or "").strip().lower() if isinstance(cur, dict) else ""
                    if isinstance(cur, dict) and (holder_low is None or cur_holder_low == holder_low):
                        claims.pop(k, None)
                        removed.append(k)
            elif holder_low:
                for k in [
                    k for k, v in claims.items()
                    if isinstance(v, dict) and str(v.get("holder") or "").strip().lower() == holder_low
                ]:
                    claims.pop(k, None)
                    removed.append(k)
        if removed:
            self._emit(resolved_sid, "claim_updated", {"released": removed})
        return {"swarmId": resolved_sid, "released": removed}

    def list_claims(
        self,
        *,
        swarm_id: str | None = None,
        board_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        with self._locked_board(swarm_id, board_dir=board_dir, mutate=False) as (sid, board, _):
            return {"swarmId": sid, "claims": _active_claims_view(board)}

    def set_plan(
        self,
        plan: dict[str, Any],
        *,
        swarm_id: str | None = None,
        board_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        """Store Coordinator plan (`goal`, `tasks`, `threads`, `summary`) on the board and sync meta."""
        if not isinstance(plan, dict):
            raise ValueError("`plan` must be an object.")
        raw_goal = plan.get("goal")
        clean_goal: dict[str, str] | None = None
        if isinstance(raw_goal, dict):
            clean_goal = {
                "title": str(raw_goal.get("title") or "").strip(),
                "line": str(raw_goal.get("line") or "").strip(),
            }
        elif isinstance(raw_goal, str) and raw_goal.strip():
            clean_goal = {"title": raw_goal.strip(), "line": ""}

        raw_tasks = plan.get("tasks")
        clean_tasks: dict[str, Any] | list[dict[str, Any]] = {}
        if isinstance(raw_tasks, dict):
            clean_tasks = {
                str(k).strip().lstrip("@"): v
                for k, v in raw_tasks.items()
                if str(k).strip()
            }
        elif isinstance(raw_tasks, list):
            clean_tasks = [dict(t) for t in raw_tasks if isinstance(t, dict)]

        clean_threads: list[dict[str, Any]] = []
        for idx, th in enumerate(plan.get("threads") or []):
            if not isinstance(th, dict):
                continue
            tid = str(th.get("id") or f"t-{idx + 1}").strip()
            task_refs = th.get("tasks") if isinstance(th.get("tasks"), list) else (th.get("workstreams") or [])
            clean_threads.append({
                "id": tid,
                "title": str(th.get("title") or tid).strip(),
                "goal": str(th.get("goal") or "").strip(),
                "now": str(th.get("now") or "").strip(),
                "state": str(th.get("state") or "").strip(),
                "tasks": [str(x).strip().lstrip("@") for x in task_refs if str(x).strip()],
            })

        ts = models.now_iso()
        summary_text = str(plan.get("summary") or (clean_goal["line"] if clean_goal else "") or "").strip()
        plan_doc: dict[str, Any] = {
            "summary": summary_text,
            "threads": clean_threads,
            "tasks": clean_tasks,
            "updatedAt": ts,
        }
        if clean_goal is not None:
            plan_doc["goal"] = clean_goal

        resolved_sid = "default"
        with self._locked_board(swarm_id, board_dir=board_dir, mutate=True) as (sid, board, _):
            resolved_sid = sid
            board["plan"] = plan_doc
            if clean_goal and clean_goal.get("title"):
                board["title"] = clean_goal["title"]
            if clean_goal and clean_goal.get("line"):
                board["goal"] = clean_goal["line"]

        if clean_goal and (clean_goal.get("title") or clean_goal.get("line")) and board_dir is None:
            try:
                self._store.upsert_meta(
                    resolved_sid,
                    title=clean_goal.get("title") or None,
                    goal=clean_goal.get("line") if clean_goal.get("line") else None,
                )
            except Exception:
                pass

        self._emit(resolved_sid, "plan_updated", {"plan": plan_doc})
        return {"swarmId": resolved_sid, "plan": plan_doc}

    def enqueue_pending_mention(
        self,
        worker_name: str,
        entry: dict[str, Any],
        *,
        swarm_id: str | None = None,
        board_dir: Path | str | None = None,
    ) -> list[dict[str, Any]]:
        """Queue a mention for a busy worker so it replays upon turn completion."""
        wkey = str(worker_name or "").strip().lower()
        if not wkey:
            return []
        seq_val = int(entry.get("seq") or 0)
        with self._locked_board(swarm_id, board_dir=board_dir, mutate=True) as (_, board, _):
            pending = board.setdefault("pendingMentions", {})
            bucket = list(pending.get(wkey) or [])
            if not any(int(x.get("seq") or 0) == seq_val and seq_val > 0 for x in bucket):
                bucket.append(dict(entry))
            pending[wkey] = bucket[-50:]
            return list(pending[wkey])

    def drain_pending_mentions(
        self,
        worker_name: str,
        *,
        swarm_id: str | None = None,
        board_dir: Path | str | None = None,
    ) -> list[dict[str, Any]]:
        """Pop all queued mentions for `worker_name` atomically."""
        wkey = str(worker_name or "").strip().lower()
        if not wkey:
            return []
        with self._locked_board(swarm_id, board_dir=board_dir, mutate=True) as (_, board, _):
            pending = board.setdefault("pendingMentions", {})
            items = list(pending.pop(wkey, []) or [])
            return items

    def get_status(
        self,
        *,
        swarm_id: str | None = None,
        board_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        """Return compact board status (head, members, roster, channels, claims) for monitors and CLI."""
        with self._locked_board(swarm_id, board_dir=board_dir, mutate=False) as (sid, board, _):
            meta = self._store.read_meta(sid) or models.default_swarm_meta(sid)
            msgs = [m for m in (board.get("messages") or []) if isinstance(m, dict)]
            channels_map = board.get("channels") or {}
            members = dict(board.get("members") or {})
            roster: list[dict[str, Any]] = []
            for mname, mrec in members.items():
                if not isinstance(mrec, dict):
                    continue
                vis_cnt, _ = compute_visible_unread(mname, mrec, msgs, channels_map)
                eff_chs = effective_member_channels(mrec.get("role"), mrec.get("channels"), mname)
                roster.append({
                    "name": mname,
                    "role": mrec.get("role") or models.Role.WORKER.value,
                    "state": mrec.get("state") or models.MemberState.WAITING.value,
                    "waitSince": mrec.get("waitSince"),
                    "lastReadSeq": int(mrec.get("lastReadSeq") or 0),
                    "visibleUnread": vis_cnt,
                    "channels": ["*" if c == "*" else channel_display_name(c) for c in eff_chs],
                    "model": mrec.get("model"),
                })
            return {
                "swarmId": sid,
                "title": meta.get("title") or board.get("title") or sid,
                "head": self.head_seq(board),
                "members": members,
                "roster": roster,
                "channels": list(channels_map.values()),
                "claims": _active_claims_view(board),
                "pendingMentions": {k: len(v) for k, v in (board.get("pendingMentions") or {}).items() if v},
                "updatedAt": board.get("updatedAt"),
            }

    def message_matches_target(
        self,
        msg: dict[str, Any],
        *,
        target: str,
        board: dict[str, Any],
        mentions_only: bool = True,
    ) -> bool:
        """Return True if `msg` should wake `target` under `mentions_only` and channel rules."""
        if not isinstance(msg, dict) or isinstance(msg.get("control"), dict):
            return False
        if str(msg.get("senderRole") or "").lower() == models.Role.SUBAGENT.value or int(msg.get("senderTier") or 2) == 3:
            return False
        sender_low = str(msg.get("sender") or "").strip().lower()
        tlow = str(target or "coordinator").strip().lstrip("@").lower()
        if sender_low == tlow:
            return False
        if not mentions_only:
            _, mem = find_member_ci(board.get("members") or {}, target)
            eff_chs = effective_member_channels((mem or {}).get("role"), (mem or {}).get("channels"), target)
            return sees_in(target, eff_chs, msg, board.get("channels") or {})

        raw_mentions = [str(x).strip().lstrip("@").lower() for x in (msg.get("mentions") or []) if str(x).strip()]
        if msg.get("mentions") is None:
            inline = extract_mentions(str(msg.get("summary") or msg.get("text") or ""))
            for x in inline:
                if x.lower() not in raw_mentions:
                    raw_mentions.append(x.lower())
        if "all" in raw_mentions or tlow in raw_mentions:
            return True

        # Prefix match for sharded worker groups (`<prefix>-<k>`).
        if "-" in tlow:
            prefix = tlow.rsplit("-", 1)[0]
            if tlow.rsplit("-", 1)[1].isdigit() and prefix in raw_mentions:
                return True

        # Coordinator also wakes on unaddressed Tier-2 questions/escalations/handoffs/approval_request/[done]/[stall].
        if tlow == models.Role.COORDINATOR.value and not raw_mentions:
            mtype = str(msg.get("type") or "").lower()
            raw_kind = str(msg.get("kind") or mtype).lower()
            summary_low = str(msg.get("summary") or "").strip().lower()
            if mtype in (models.MessageType.QUESTION.value, models.MessageType.ESCALATION.value, models.MessageType.HANDOFF.value):
                return True
            if raw_kind in ("question", "escalation", "blocker", "handoff", "handoff_ready", "approval_request"):
                return True
            if summary_low.startswith(("[done]", "[stall]")):
                return True

        # Channel mentions (`@#channel`) wake members subscribed to that active channel.
        chan_mentions = [models.normalize_channel_name(c) for c in (msg.get("channelMentions") or [])]
        if chan_mentions:
            _, mem = find_member_ci(board.get("members") or {}, target)
            mem = mem or {}
            eff_chs = effective_member_channels(mem.get("role"), mem.get("channels"), target)
            channels_map = board.get("channels") or {}
            for ch in chan_mentions:
                ch_rec = channels_map.get(ch)
                if isinstance(ch_rec, dict) and ch_rec.get("archived"):
                    continue
                if "*" in eff_chs or ch in eff_chs:
                    return True

        return False

    async def wait_for_update(
        self,
        *,
        swarm_id: str = "default",
        since: int = 0,
        target: str = "coordinator",
        mentions_only: bool = True,
        timeout_seconds: float = 300.0,
        board_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        """Block until a matching board message arrives after `since` or timeout expires."""
        sid, bfile, _ = resolve_board_location(
            swarm_id,
            state_dir=self._store._state_dir,
            board_dir=board_dir,
        )
        since_int = max(0, int(since))
        try:
            raw_timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            raw_timeout = 300.0
        if not math.isfinite(raw_timeout):
            raw_timeout = 300.0
        timeout = max(0.2, min(1800.0, raw_timeout))
        deadline = time.monotonic() + timeout
        waiter_id = uuid.uuid4().hex

        try:
            await asyncio.to_thread(
                self.set_member_wait,
                target,
                waiting=True,
                waiter_id=waiter_id,
                swarm_id=sid,
                board_dir=board_dir,
            )
        except Exception:
            pass

        def _stat_sig() -> tuple[int, int] | None:
            try:
                st = bfile.stat()
                return (st.st_mtime_ns, st.st_size)
            except OSError:
                return None

        def _check() -> dict[str, Any] | None:
            board = self.read_board(sid, board_dir=board_dir)
            head = self.head_seq(board)
            msgs = [
                m for m in (board.get("messages") or [])
                if isinstance(m, dict) and int(m.get("seq") or 0) > since_int
            ]
            msgs.sort(key=lambda m: int(m.get("seq") or 0))
            matched = [
                m for m in msgs
                if self.message_matches_target(m, target=target, board=board, mentions_only=mentions_only)
            ]
            if matched:
                return {"woken": True, "timedOut": False, "swarmId": sid, "head": head, "messages": matched}
            return None

        def _complete_wait(matched_msgs: list[dict[str, Any]]) -> None:
            matched_seqs = [int(m.get("seq") or 0) for m in matched_msgs if int(m.get("seq") or 0) > 0]
            self.set_member_wait(
                target,
                waiting=False,
                read_seqs=matched_seqs,
                waiter_id=waiter_id,
                advance_contiguous=True,
                swarm_id=sid,
                board_dir=board_dir,
            )

        q = self.subscribe_waiter(sid)
        try:
            last_sig = await asyncio.to_thread(_stat_sig)
            immediate = await asyncio.to_thread(_check)
            if immediate is not None:
                try:
                    await asyncio.to_thread(_complete_wait, immediate["messages"])
                except Exception:
                    pass
                return immediate

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    board = await asyncio.to_thread(self.read_board, sid, board_dir=board_dir)
                    return {
                        "woken": False,
                        "timedOut": True,
                        "swarmId": sid,
                        "head": self.head_seq(board),
                        "messages": [],
                    }
                got_event = False
                try:
                    await asyncio.wait_for(q.get(), timeout=min(0.5, remaining))
                    got_event = True
                except asyncio.TimeoutError:
                    pass

                while not q.empty():
                    try:
                        q.get_nowait()
                        got_event = True
                    except asyncio.QueueEmpty:
                        break

                cur_sig = await asyncio.to_thread(_stat_sig)
                if not got_event and cur_sig == last_sig:
                    continue
                last_sig = cur_sig

                hit = await asyncio.to_thread(_check)
                if hit is not None:
                    try:
                        await asyncio.to_thread(_complete_wait, hit["messages"])
                    except Exception:
                        pass
                    return hit
        finally:
            self.unsubscribe_waiter(sid, q)
            try:
                loop = asyncio.get_running_loop()
                loop.run_in_executor(
                    None,
                    lambda: self.set_member_wait(
                        target,
                        waiting=False,
                        waiter_id=waiter_id,
                        swarm_id=sid,
                        board_dir=board_dir,
                    ),
                )
            except Exception:
                pass
