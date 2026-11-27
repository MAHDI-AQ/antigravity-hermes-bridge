"""A2A v1.0 HTTP/JSON-RPC/SSE server, read-only Swarm observer endpoints, and loopback CLI post hook."""

from __future__ import annotations

import asyncio
import atexit
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import random
import re
import socket
import sys
import threading
import time
from typing import Any, AsyncIterator
from urllib.parse import quote, urlparse

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
import uvicorn

import board as board_mod
import catalog as catalog_mod
import models
import runtime as runtime_mod
import store

_BASE_DIR = Path(__file__).resolve().parent
_BOARD_UI_DIR = _BASE_DIR / "board_ui"

_FRAME_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_ASSET_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}
_MAX_FRAME_BYTES = 8 * 1024 * 1024
_MAX_VIEW_BYTES = 64 * 1024
_MAX_BODY_BYTES = 2 * 1024 * 1024
_MAX_SSE_SUBSCRIBERS = 64
STUCK_MS = 600_000
STEP_ERROR_SETTLE_MS = 60_000
QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|quota|\b429\b|exhausted your capacity|rate.?limit", re.I)
_ROLE_WORKER_RE = re.compile(r"^(?:narrator|monitor)(?:$|[-_0-9])", re.I)
_FILE_CACHE_LOCK = threading.Lock()
_FILE_CACHE: OrderedDict[str, tuple[tuple[int, int], Any]] = OrderedDict()
_VIEW_JSON_CACHE_LOCK = threading.Lock()
_VIEW_JSON_CACHE: OrderedDict[str, tuple[dict[str, Any], int]] = OrderedDict()
_SSE_SUBSCRIBERS_LOCK = threading.Lock()
_ACTIVE_SSE_QUEUES: set[asyncio.Queue[Any]] = set()


def _is_test_mode() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST") or "unittest" in sys.modules or "pytest" in sys.modules)


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _load_url_hosts() -> list[str]:
    raw = (os.environ.get("HERMES_SWARM_URL_HOSTS") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(h).strip() for h in parsed if str(h).strip()]
        except Exception:
            return [h.strip() for h in raw.split(",") if h.strip()]
    cand = Path.home() / ".gemini" / "config" / "plugins" / "hermes-swarm" / "sidecars" / "swarm_board" / "url_hosts.json"
    if cand.is_file():
        try:
            parsed = json.loads(cand.read_text(encoding="utf-8"))
            if isinstance(parsed, list):
                return [str(h).strip() for h in parsed if str(h).strip()]
        except Exception:
            pass
    return []


def _iso_to_epoch_ms(raw: str | None) -> int:
    if not raw:
        return int(time.time() * 1000)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)


def _parse_host_header(host_header: str) -> str:
    raw = (host_header or "").strip()
    if not raw:
        return ""
    if raw == "::1":
        return "::1"
    try:
        hostname = urlparse(f"http://{raw}").hostname
        return (hostname or "").lower()
    except ValueError:
        return "__invalid__"


def _check_auth(request: Request, token: str | None) -> Response | None:
    if not token:
        parsed_host = _parse_host_header(request.headers.get("host") or "")
        if not parsed_host or parsed_host not in ("127.0.0.1", "localhost", "::1"):
            return JSONResponse({"error": "Forbidden host without bearer token"}, status_code=403)
        return None
    auth = request.headers.get("authorization") or ""
    supplied = ""
    if auth.startswith("Bearer "):
        supplied = auth[7:].strip()
    elif request.headers.get("x-sidecar-token"):
        supplied = str(request.headers.get("x-sidecar-token") or "").strip()
    if not supplied or not hmac.compare_digest(supplied, token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return None


def _allowed_loopback_clients() -> tuple[str, ...]:
    if _is_test_mode():
        return ("127.0.0.1", "::1", "localhost", "testclient")
    return ("127.0.0.1", "::1", "localhost")


def _check_loopback_internal_auth(request: Request, manager: runtime_mod.SwarmManager) -> Response | None:
    client_host = request.client.host if request.client else ""
    if not client_host or client_host not in _allowed_loopback_clients():
        return JSONResponse({"error": "Forbidden: loopback required"}, status_code=403)
    parsed_host = _parse_host_header(request.headers.get("host") or "")
    if not parsed_host or parsed_host not in ("127.0.0.1", "localhost", "::1"):
        return JSONResponse({"error": "Forbidden host"}, status_code=403)

    expected = manager.get_internal_token()
    auth = request.headers.get("authorization") or ""
    supplied = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied, expected):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return None


def _check_loopback_wake_auth(
    request: Request,
    bearer_token: str | None,
    manager: runtime_mod.SwarmManager | None = None,
) -> Response | None:
    client_host = request.client.host if request.client else ""
    if not client_host or client_host not in _allowed_loopback_clients():
        return JSONResponse({"error": "Forbidden: loopback required"}, status_code=403)
    parsed_host = _parse_host_header(request.headers.get("host") or "")
    if not parsed_host or parsed_host not in ("127.0.0.1", "localhost", "::1"):
        return JSONResponse({"error": "Forbidden host"}, status_code=403)

    expected = (
        bearer_token
        or (os.environ.get("HERMES_A2A_HTTP_TOKEN") or "").strip()
        or (manager.get_internal_token() if manager is not None else None)
    )
    if expected:
        auth = request.headers.get("authorization") or ""
        supplied = ""
        if auth.startswith("Bearer "):
            supplied = auth[7:].strip()
        elif request.headers.get("x-sidecar-token"):
            supplied = str(request.headers.get("x-sidecar-token") or "").strip()
        if not supplied or not hmac.compare_digest(supplied, expected):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return None


async def _read_bounded_body(
    request: Request,
    max_bytes: int = _MAX_BODY_BYTES,
) -> tuple[bytes | None, Response | None]:
    cl_raw = request.headers.get("content-length")
    if cl_raw is not None:
        try:
            if int(cl_raw) > max_bytes:
                return None, JSONResponse({"error": "Request body too large"}, status_code=413)
        except ValueError:
            return None, JSONResponse({"error": "Invalid Content-Length"}, status_code=400)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return None, JSONResponse({"error": "Request body too large"}, status_code=413)
        chunks.append(chunk)
    return b"".join(chunks), None


async def _read_json_body(request: Request) -> tuple[dict[str, Any] | None, Response | None]:
    raw_bytes, err_resp = await _read_bounded_body(request)
    if err_resp is not None:
        return None, err_resp
    assert raw_bytes is not None
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return None, JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    if not isinstance(raw, dict):
        return None, JSONResponse({"error": "JSON body must be an object"}, status_code=400)
    return raw, None


def _extract_worker_config(params: dict[str, Any]) -> models.WorkerConfig:
    meta = params.get("metadata")
    if not isinstance(meta, dict):
        msg = params.get("message")
        meta = msg.get("metadata") if isinstance(msg, dict) else None
    if isinstance(meta, dict):
        hermes_meta = meta.get("hermes") if isinstance(meta.get("hermes"), dict) else meta
        return models.WorkerConfig.from_dict(hermes_meta)
    return models.WorkerConfig()


def _extract_scope_hints(
    request: Request,
    body: dict[str, Any] | None = None,
) -> tuple[str | None, str | None, str | None]:
    qp = request.query_params
    hdrs = request.headers
    b = body or {}
    swarm_hint = (
        qp.get("swarm")
        or qp.get("swarmId")
        or hdrs.get("x-swarm-id")
        or b.get("swarmId")
        or b.get("swarm_id")
        or b.get("swarm")
    )
    conv_hint = (
        qp.get("conversationId")
        or qp.get("conversation_id")
        or hdrs.get("x-conversation-id")
        or b.get("conversationId")
        or b.get("conversation_id")
    )
    ws_hint = (
        qp.get("workspace")
        or qp.get("workspacePath")
        or hdrs.get("x-workspace-path")
        or b.get("workspacePath")
        or b.get("workspace")
    )
    return (
        str(swarm_hint).strip() if swarm_hint is not None and str(swarm_hint).strip() else None,
        str(conv_hint).strip() if conv_hint is not None and str(conv_hint).strip() else None,
        str(ws_hint).strip() if ws_hint is not None and str(ws_hint).strip() else None,
    )


def _allowed_app_data_brain_roots() -> list[Path]:
    roots: list[Path] = []
    data_dir = os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR", "").strip()
    if data_dir:
        cur = Path(store.normalize_local_path(data_dir))
        for parent in [cur, *cur.parents]:
            if parent.name == "sidecar_data":
                brain = parent.parent / "brain"
                if brain.is_dir():
                    roots.append(brain)
                break
    if not roots:
        gemini_root = Path.home() / ".gemini"
        if gemini_root.is_dir():
            try:
                for child in gemini_root.iterdir():
                    brain = child / "brain"
                    if brain.is_dir():
                        roots.append(brain)
            except OSError:
                pass
    return roots


def _read_cached_text(path: Path) -> tuple[str | None, os.stat_result | None]:
    try:
        st = path.stat()
    except OSError:
        return None, None
    sig = (st.st_mtime_ns, st.st_size)
    key = str(path)
    with _FILE_CACHE_LOCK:
        cached = _FILE_CACHE.get(key)
        if cached and cached[0] == sig:
            _FILE_CACHE.move_to_end(key)
            return cached[1], st
    try:
        txt = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None, st
    with _FILE_CACHE_LOCK:
        _FILE_CACHE[key] = (sig, txt)
        _FILE_CACHE.move_to_end(key)
        while len(_FILE_CACHE) > 256:
            _FILE_CACHE.popitem(last=False)
    return txt, st


def _read_narrator_view(swarm_dir: Path, narrator_conv_id: str | None = None) -> dict[str, Any]:
    view_dir = swarm_dir
    source = "swarm"
    vjson = view_dir / "view.json"
    vhtml = view_dir / "view.html"
    if not vjson.is_file() and not vhtml.is_file() and narrator_conv_id:
        clean_cid = str(narrator_conv_id).strip()
        if clean_cid and clean_cid != "." and "/" not in clean_cid and "\\" not in clean_cid and ".." not in clean_cid:
            for root in _allowed_app_data_brain_roots():
                fb_dir = root / clean_cid / "swarm_view"
                if not store.is_path_within(fb_dir, [root]):
                    continue
                if (fb_dir / "view.json").is_file() or (fb_dir / "view.html").is_file():
                    view_dir = fb_dir
                    source = "brain"
                    vjson = view_dir / "view.json"
                    vhtml = view_dir / "view.html"
                    break

    cache_key = str(swarm_dir)
    out: dict[str, Any] = {
        "exists": False,
        "dir": str(view_dir),
        "source": source,
        "json": None,
        "jsonMtime": 0,
        "jsonError": "",
        "html": None,
        "htmlKey": "",
        "htmlMtime": 0,
        "htmlError": "",
    }
    if vjson.is_file():
        out["exists"] = True
        try:
            st = vjson.stat()
            out["jsonMtime"] = int(st.st_mtime * 1000)
            if st.st_size > _MAX_VIEW_BYTES:
                out["jsonError"] = "too_large"
            else:
                raw_txt, _ = _read_cached_text(vjson)
                parsed = json.loads(raw_txt or "")
                if isinstance(parsed, dict):
                    out["json"] = parsed
                    with _VIEW_JSON_CACHE_LOCK:
                        _VIEW_JSON_CACHE[cache_key] = (parsed, out["jsonMtime"])
                        _VIEW_JSON_CACHE.move_to_end(cache_key)
                        while len(_VIEW_JSON_CACHE) > 128:
                            _VIEW_JSON_CACHE.popitem(last=False)
                else:
                    out["jsonError"] = "invalid"
        except Exception:
            out["jsonError"] = "invalid"
        if out["json"] is None:
            with _VIEW_JSON_CACHE_LOCK:
                _VIEW_JSON_CACHE.pop(cache_key, None)
    else:
        with _VIEW_JSON_CACHE_LOCK:
            _VIEW_JSON_CACHE.pop(cache_key, None)
    if vhtml.is_file():
        out["exists"] = True
        try:
            st_h = vhtml.stat()
            out["htmlMtime"] = int(st_h.st_mtime * 1000)
            if st_h.st_size > _MAX_VIEW_BYTES:
                out["htmlError"] = "too_large"
            else:
                html_txt, _ = _read_cached_text(vhtml)
                if not (html_txt or "").strip():
                    out["htmlError"] = "empty"
                else:
                    out["html"] = html_txt
                    out["htmlKey"] = f"{view_dir}|{st_h.st_mtime_ns}:{st_h.st_size}"
        except Exception:
            out["htmlError"] = "invalid"
    return out


_VALID_PLAN_STATES = frozenset({"queued", "active", "blocked", "review", "done", "failed"})
_PLAN_STATUS_ALIASES = {
    "running": "active",
    "working": "active",
    "in_progress": "active",
    "completed": "done",
    "complete": "done",
    "canceled": "done",
    "cancelled": "done",
    "killed": "done",
    "waiting": "queued",
    "pending": "queued",
    "input_required": "blocked",
    "auth_required": "blocked",
}
_DONE_POST_RE = re.compile(r"^(?:@[A-Za-z0-9_-]+[\s,:–—-]*)*(?:\*\*|\*|`|_)?\[done\]", re.IGNORECASE)
_SLICE_HEADER_RE = re.compile(r"^\[(?:ASSIGNED SLICE|SHARD INSTANCE|SYSTEM|TASK)[^\]]*\]\s*$", re.IGNORECASE)


def _normalize_plan_status(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if s in _VALID_PLAN_STATES:
        return s
    return _PLAN_STATUS_ALIASES.get(s, "")


def _clean_assignment_title(raw_text: str | None, fallback: str) -> str:
    text = str(raw_text or "").strip()
    if not text:
        return fallback
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not _SLICE_HEADER_RE.match(ln.strip())]
    cand = lines[0] if lines else text
    cand = re.sub(r"^\s*(?:[-*]|#+)\s+", "", cand)
    cand = re.sub(r"\*\*([^*]+)\*\*", r"\1", cand)
    cand = cand.replace("`", "").replace("__", "")
    cand = re.sub(r"^@[A-Za-z0-9_-]+[\s,:–—-]*", "", cand)
    cand = re.sub(
        r"^(?:you\s+(?:are|own|handle|drive|run|lead|manage)|your\s+(?:role|job|task|assignment|goal|focus)\s+is(?:\s+to)?|assignment\s*:|role\s*:|task\s*:)\s*",
        "",
        cand,
        flags=re.IGNORECASE,
    )
    cand = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", cand, maxsplit=1)[0]
    cand = re.sub(r"\s+", " ", cand).strip(" .:-–—")
    if not cand:
        return fallback
    if len(cand) > 110:
        cand = cand[:107].rstrip(" ,;:-") + "…"
    return cand


def _normalize_plan_for_snapshot(
    raw_plan: dict[str, Any] | None,
    *,
    members_out: list[dict[str, Any]],
    latest_task_by_worker: dict[str, dict[str, Any]],
    last_post_by: dict[str, dict[str, Any]],
    messages_out: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(raw_plan, dict):
        return None

    member_by_low = {str(m.get("name") or "").lower(): m for m in members_out if isinstance(m, dict) and m.get("name")}
    done_senders: set[str] = set()
    coord_briefs: dict[str, str] = {}
    last_non_ctrl: dict[str, Any] | None = None
    for msg in messages_out:
        if not isinstance(msg, dict) or isinstance(msg.get("control"), dict):
            continue
        last_non_ctrl = msg
        sender_low = str(msg.get("sender") or "").strip().lower()
        summary = str(msg.get("summary") or msg.get("text") or "").strip()
        if sender_low and _DONE_POST_RE.match(summary):
            done_senders.add(sender_low)
        if str(msg.get("senderRole") or "").lower() == models.Role.COORDINATOR.value:
            for ment in msg.get("mentions") or []:
                mlow = str(ment or "").strip().lower()
                if mlow and mlow not in coord_briefs:
                    coord_briefs[mlow] = summary

    has_working_worker = any(
        (not m.get("killed")) and m.get("status") == "working"
        for m in members_out
        if m.get("role") != models.Role.COORDINATOR.value
    )
    swarm_closed = (
        not has_working_worker
        and last_non_ctrl is not None
        and str(last_non_ctrl.get("senderRole") or "").lower() == models.Role.COORDINATOR.value
        and bool(_DONE_POST_RE.match(str(last_non_ctrl.get("summary") or last_non_ctrl.get("text") or "")))
    )

    def _build_task_entry(task_key: str, raw_val: Any) -> dict[str, Any]:
        explicit_title = ""
        explicit_assignee = ""
        explicit_status = ""
        explicit_note = ""
        progress_val: Any = None
        if isinstance(raw_val, str):
            explicit_title = raw_val.strip()
        elif isinstance(raw_val, dict):
            explicit_title = str(raw_val.get("title") or raw_val.get("name") or raw_val.get("label") or "").strip()
            explicit_assignee = str(raw_val.get("assignee") or raw_val.get("worker") or "").strip().lstrip("@")
            explicit_status = _normalize_plan_status(raw_val.get("status") or raw_val.get("state"))
            explicit_note = str(raw_val.get("note") or raw_val.get("now") or "").strip()
            if "progress" in raw_val:
                progress_val = raw_val.get("progress")

        mem = (
            member_by_low.get(explicit_assignee.lower())
            if explicit_assignee
            else member_by_low.get(task_key.lower())
        )
        mname = str(mem.get("name") or "") if mem else ""
        assignee = explicit_assignee or mname
        trow = latest_task_by_worker.get(mname) if mname else None
        lp = last_post_by.get(mname) if mname else None

        if mem:
            if mname.lower() in done_senders or mem.get("killed"):
                status = "done"
            elif mem.get("status") == "working":
                status = "active"
            elif trow and trow.get("state") == models.TaskState.FAILED.value:
                status = "failed"
            elif trow and trow.get("state") in (
                models.TaskState.INPUT_REQUIRED.value,
                models.TaskState.AUTH_REQUIRED.value,
            ):
                status = "blocked"
            elif lp:
                status = "done" if swarm_closed else "review"
            elif trow and trow.get("state") == models.TaskState.COMPLETED.value:
                status = "done"
            else:
                status = explicit_status or "queued"
        else:
            status = explicit_status or "queued"

        title = explicit_title if (explicit_title and explicit_title.lower() != "task") else ""
        if not title and mem:
            title = _clean_assignment_title(mem.get("assignment") or coord_briefs.get(mname.lower()), "")
        if not title:
            title = task_key

        note = explicit_note
        if not note and mem:
            if mem.get("status") == "working" and mem.get("activity"):
                note = str(mem["activity"])
            elif lp:
                note = re.sub(r"^(?:@[A-Za-z0-9_-]+[\s,:–—-]*)+", "", str(lp.get("summary") or "")).strip()
            elif trow and trow.get("state") == models.TaskState.FAILED.value and trow.get("lastError"):
                note = str(trow["lastError"])[:180]

        entry: dict[str, Any] = {
            "title": title[:120],
            "status": status,
            "note": note[:200],
        }
        if assignee:
            entry["assignee"] = assignee
        if progress_val is not None:
            entry["progress"] = progress_val
        return entry

    norm_tasks: dict[str, dict[str, Any]] = {}
    raw_tasks = raw_plan.get("tasks")
    if isinstance(raw_tasks, dict):
        for raw_k, raw_v in raw_tasks.items():
            clean_k = str(raw_k or "").strip().lstrip("@")
            if clean_k:
                norm_tasks[clean_k] = _build_task_entry(clean_k, raw_v)
    elif isinstance(raw_tasks, list):
        for idx, item in enumerate(raw_tasks):
            if isinstance(item, dict):
                clean_k = str(
                    item.get("id")
                    or item.get("taskId")
                    or item.get("name")
                    or item.get("assignee")
                    or f"task-{idx + 1}"
                ).strip().lstrip("@")
                if clean_k:
                    norm_tasks[clean_k] = _build_task_entry(clean_k, item)
            elif isinstance(item, str) and item.strip():
                clean_k = item.strip().lstrip("@")
                norm_tasks[clean_k] = _build_task_entry(clean_k, None)

    for th in raw_plan.get("threads") or []:
        if not isinstance(th, dict):
            continue
        refs = th.get("tasks") if isinstance(th.get("tasks"), list) else (th.get("workstreams") or [])
        for raw_ref in refs:
            clean_ref = str(raw_ref or "").strip().lstrip("@")
            if clean_ref and clean_ref not in norm_tasks:
                norm_tasks[clean_ref] = _build_task_entry(clean_ref, None)

    out_plan = dict(raw_plan)
    out_plan["tasks"] = norm_tasks
    return out_plan


def _normalize_recent_tools(raw: Any, *, limit: int = 12) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[-limit:]:
        if not isinstance(item, dict):
            continue
        out.append({
            "name": str(item.get("name") or "tool"),
            "preview": str(item.get("preview") or ""),
            "status": str(item.get("status") or "ok"),
            "durationMs": int(item.get("durationMs") or 0),
            "at": str(item.get("at") or ""),
        })
    return out


def _task_to_summary_row(task: dict[str, Any], members_map: dict[str, Any]) -> dict[str, Any]:
    st = str(((task.get("status") or {}).get("state")) or "")
    meta = task.get("metadata") or {}
    wcfg = meta.get("workerConfig") or {}
    act = meta.get("liveActivity") or {}
    wname = str(wcfg.get("worker_name") or task.get("id") or "")
    member_rec = members_map.get(wname) if isinstance(members_map.get(wname), dict) else {}
    tool_calls = int(act.get("toolCalls") or 0)
    steps = int(act.get("steps") or (1 if tool_calls > 0 else 0))
    raw_cur = act.get("currentTool")
    current_tool: dict[str, Any] | None = None
    if st in models.ACTIVE_STATES and isinstance(raw_cur, dict) and raw_cur.get("name"):
        current_tool = {
            "name": str(raw_cur.get("name") or ""),
            "preview": str(raw_cur.get("preview") or ""),
            "startedAt": str(raw_cur.get("startedAt") or ""),
        }
    recent_tools = _normalize_recent_tools(act.get("recentTools"), limit=12)
    ended_at = act.get("endedAt") or (
        (task.get("status") or {}).get("timestamp") if st in models.TERMINAL_STATES else None
    )
    elapsed_ms = int(act.get("elapsedMs") or 0)
    if elapsed_ms <= 0:
        elapsed_ms = runtime_mod._elapsed_ms(act.get("startedAt") or meta.get("createdAt"), ended_at=ended_at)
    ctx_limit = runtime_mod._resolve_context_limit(wcfg.get("model"), wcfg.get("provider"), wcfg.get("base_url"))
    tokens = runtime_mod._normalize_tokens_dict(meta.get("tokens"), context_limit=ctx_limit)
    tps = float(act.get("tps") if act.get("tps") is not None else runtime_mod._compute_tps(tokens["output"], elapsed_ms))
    if tps <= 0.0 and tokens["output"] > 0 and elapsed_ms > 0:
        tps = runtime_mod._compute_tps(tokens["output"], elapsed_ms)
    cost_raw = meta.get("costUsd")
    cost_usd = round(float(cost_raw), 6) if cost_raw is not None else None
    return {
        "taskId": task.get("id"),
        "contextId": task.get("contextId"),
        "state": st,
        "role": wcfg.get("role") or models.Role.WORKER.value,
        "tier": 2,
        "workerName": wname,
        "name": wname,
        "model": wcfg.get("model"),
        "provider": wcfg.get("provider"),
        "workdir": wcfg.get("workdir"),
        "worktreePath": meta.get("worktreePath"),
        "worktreeBranch": meta.get("worktreeBranch"),
        "createdAt": meta.get("createdAt"),
        "endedAt": act.get("endedAt"),
        "lastEventAt": act.get("lastEventAt") or act.get("startedAt") or meta.get("createdAt"),
        "steps": steps,
        "toolCalls": tool_calls,
        "lastTool": act.get("lastTool"),
        "currentTool": current_tool,
        "recentTools": recent_tools,
        "lastTextPreview": act.get("lastTextPreview") or "",
        "lastError": act.get("lastError") or "",
        "hermesSessionId": act.get("hermesSessionId"),
        "tokens": tokens,
        "tps": tps,
        "elapsedMs": elapsed_ms,
        "costUsd": cost_usd,
        "assignment": member_rec.get("assignment") or "",
    }


def _extract_token_economy(tok: dict[str, Any]) -> tuple[int, int, int, int, int, float]:
    """Return `(pure_input, cached_input, cache_write, output, total_processed, context_pct)` from a token dict."""
    raw_in = int(tok.get("input") or 0)
    pure_in = int(
        tok["pureInput"]
        if tok.get("pureInput") is not None
        else (tok["uncachedInput"] if tok.get("uncachedInput") is not None else raw_in)
    )
    cached_in = int(
        tok["cachedInput"]
        if tok.get("cachedInput") is not None
        else (tok.get("cacheRead") or 0)
    )
    cache_write = int(tok.get("cacheWrite") or 0)
    out_tok = int(tok.get("output") or 0)
    tot_proc = int(
        tok["totalProcessed"]
        if tok.get("totalProcessed") is not None
        else (tok.get("total") or (pure_in + cached_in + cache_write + out_tok))
    )
    ctx_used = int(tok.get("contextUsed") or 0)
    ctx_limit = int(tok.get("contextLimit") or 0)
    if tok.get("contextPct") is not None:
        ctx_pct = float(tok.get("contextPct") or 0.0)
    elif ctx_limit > 0 and ctx_used > 0:
        ctx_pct = (ctx_used / ctx_limit) * 100.0
    else:
        ctx_pct = 0.0
    return pure_in, cached_in, cache_write, out_tok, tot_proc, ctx_pct


def compute_swarm_telemetry(members_out: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute swarm-wide live telemetry rollup across Tier-2 workers and non-overlapping Tier-3 subagents."""
    active_workers = 0
    waiting_workers = 0
    killed_workers = 0
    subagent_count = 0
    total_steps = 0
    total_tool_calls = 0
    total_tokens = 0
    pure_input_tokens = 0
    cached_input_tokens = 0
    cache_write_tokens = 0
    output_tokens = 0
    total_output_for_tps = 0
    total_elapsed_ms_for_tps = 0
    live_tps = 0.0
    cost_sum = 0.0
    has_cost = False
    compaction_warning_count = 0
    seen_sub_ids: set[str] = set()

    for m in members_out:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip().lower()
        subs = m.get("subagents") if isinstance(m.get("subagents"), list) else []
        subagent_count += len(subs)
        total_steps += int(m.get("steps") or 0)
        total_tool_calls += int(m.get("toolCalls") or 0)
        tok = m.get("tokens") if isinstance(m.get("tokens"), dict) else {}
        m_pure, m_cached, m_cw, m_out, m_tot, m_ctx_pct = _extract_token_economy(tok)
        pure_input_tokens += m_pure
        cached_input_tokens += m_cached
        cache_write_tokens += m_cw
        output_tokens += m_out
        total_tokens += m_tot
        if m_ctx_pct >= 70.0:
            compaction_warning_count += 1
        el_ms = int(m.get("elapsedMs") or 0)
        if m_out > 0 and el_ms > 0:
            total_output_for_tps += m_out
            total_elapsed_ms_for_tps += el_ms
        if m.get("costUsd") is not None:
            has_cost = True
            cost_sum += float(m["costUsd"])

        for s in subs:
            if not isinstance(s, dict):
                continue
            sid_key = str(s.get("id") or "").strip()
            if sid_key:
                if sid_key in seen_sub_ids:
                    continue
                seen_sub_ids.add(sid_key)
            if s.get("includedInParent"):
                continue
            stok = s.get("tokens") if isinstance(s.get("tokens"), dict) else {}
            s_pure, s_cached, s_cw, s_out, s_tot, s_ctx_pct = _extract_token_economy(stok)
            pure_input_tokens += s_pure
            cached_input_tokens += s_cached
            cache_write_tokens += s_cw
            output_tokens += s_out
            total_tokens += s_tot
            if s_ctx_pct >= 70.0:
                compaction_warning_count += 1
            s_el = int(s.get("elapsedMs") or 0)
            if s_out > 0 and s_el > 0:
                total_output_for_tps += s_out
                total_elapsed_ms_for_tps += s_el

        if role in (models.Role.COORDINATOR.value, models.Role.PRIMARY.value):
            continue
        if m.get("killed"):
            killed_workers += 1
        elif m.get("status") == "working":
            active_workers += 1
            live_tps += float(m.get("tps") or 0.0)
        else:
            waiting_workers += 1

    new_tokens = pure_input_tokens + output_tokens
    total_input_for_cache = pure_input_tokens + cached_input_tokens
    cache_hit_rate = round(cached_input_tokens / total_input_for_cache, 4) if total_input_for_cache > 0 else 0.0
    low_cache_hit_alert = bool(total_input_for_cache > 50_000 and cache_hit_rate < 0.25)
    avg_tps = runtime_mod._compute_tps(total_output_for_tps, total_elapsed_ms_for_tps)
    return {
        "activeWorkers": active_workers,
        "waitingWorkers": waiting_workers,
        "killedWorkers": killed_workers,
        "subagentCount": subagent_count,
        "totalSteps": total_steps,
        "totalToolCalls": total_tool_calls,
        "totalTokens": total_tokens,
        "totalProcessedTokens": total_tokens,
        "pureInputTokens": pure_input_tokens,
        "cachedInputTokens": cached_input_tokens,
        "cacheWriteTokens": cache_write_tokens,
        "outputTokens": output_tokens,
        "newTokens": new_tokens,
        "cacheHitRate": cache_hit_rate,
        "compactionWarningCount": compaction_warning_count,
        "lowCacheHitAlert": low_cache_hit_alert,
        "avgTps": avg_tps,
        "liveTps": round(live_tps, 1),
        "totalCostUsd": round(cost_sum, 6) if has_cost else None,
    }


def _member_status_for_ui(raw_state: str, *, killed: bool) -> str:
    if killed or raw_state == models.MemberState.KILLED.value:
        return "killed"
    if raw_state in (models.MemberState.WORKING.value, "running", "active"):
        return "working"
    return "waiting"


def _pick_narrator(members_out: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select the highest-ranked live narrator, or newest killed narrator if none live."""
    candidates = [
        m for m in members_out
        if m.get("narrator") or (m.get("role") != models.Role.COORDINATOR.value and str(m.get("name") or "").lower().startswith("narrator"))
    ]
    if not candidates:
        return None
    def _rank(m: dict[str, Any]) -> tuple[int, int, str]:
        killed = bool(m.get("killed"))
        st = str(m.get("status") or "")
        if not killed:
            tier = 0 if st == "working" else 1
        else:
            tier = 2
        return (tier, -(int(m.get("lastPostSeq") or 0)), str(m.get("name") or ""))
    candidates.sort(key=_rank)
    return candidates[0]


def build_observer_snapshot(
    manager: runtime_mod.SwarmManager,
    swarm_id: str,
    *,
    member: str = "",
    known_etag: str = "",
) -> dict[str, Any]:
    """Build a read-only v2 observer snapshot aligned with `board_ui` and `wakeup_bridge.mjs`."""
    sid = store.sanitize_swarm_id(swarm_id) or "default"
    paths = manager.store.paths(sid)
    meta = manager.store.read_meta(sid) or models.default_swarm_meta(sid)
    board = manager.board_service.read_board(sid)
    runtime = manager.get_runtime(sid)
    raw_tasks = runtime.get_task_snapshots()

    members_map = board.get("members") if isinstance(board.get("members"), dict) else {}
    task_rows = [_task_to_summary_row(t, members_map) for t in raw_tasks]
    latest_task_by_worker: dict[str, dict[str, Any]] = {}
    for row in task_rows:
        wn = str(row.get("workerName") or "")
        if wn and wn not in latest_task_by_worker:
            latest_task_by_worker[wn] = row

    killed_workers = {
        str(mname).strip().lower()
        for mname, mrec in members_map.items()
        if isinstance(mrec, dict) and (
            bool(mrec.get("killed")) or str(mrec.get("state") or "").strip().lower() == models.MemberState.KILLED.value
        )
    }
    killed_workers.update(getattr(runtime, "_killed_workers", ()))
    active_tasks = [
        r for r in task_rows
        if r["state"] in models.ACTIVE_STATES
        and str(r.get("workerName") or "").strip().lower() not in killed_workers
    ]
    recent_completed = [
        r for r in task_rows
        if r["state"] in models.TERMINAL_STATES
    ][:30]

    # Channels: non-main channels (plus #general when defined alongside non-main channels).
    raw_channels = board.get("channels") if isinstance(board.get("channels"), dict) else {}
    channels_out: list[dict[str, Any]] = []
    general_entry: dict[str, Any] | None = None
    for ch_key, crec in sorted(raw_channels.items()):
        if not isinstance(crec, dict):
            continue
        disp = board_mod.channel_display_name(ch_key)
        is_general = str(ch_key).strip().lstrip("#").lower() == "general"
        entry_ch = {
            "name": "general" if is_general else disp,
            "topic": str(crec.get("topic") or crec.get("description") or ""),
            "createdBy": str(crec.get("createdBy") or "coordinator"),
            "created_by": str(crec.get("createdBy") or "coordinator"),
            "archived": bool(crec.get("archived", False)),
            "subscribers": list(crec.get("subscribers") or []),
        }
        if disp == "main":
            if is_general:
                general_entry = entry_ch
            continue
        channels_out.append(entry_ch)
    if channels_out and general_entry is not None:
        channels_out.insert(0, general_entry)

    # Normalize messages oldest-first with channel mention + via_channel expansion.
    raw_msgs = [m for m in (board.get("messages") or []) if isinstance(m, dict)]
    raw_msgs.sort(key=lambda m: int(m.get("seq") or 0))
    head = manager.board_service.head_seq(board)

    posts_count: dict[str, int] = {}
    control_posts_count: dict[str, int] = {}
    last_post_by: dict[str, dict[str, Any]] = {}
    last_control_by: dict[str, dict[str, Any]] = {}
    explicit_read_seqs: dict[str, set[int]] = {}
    messages_out: list[dict[str, Any]] = []
    for m in raw_msgs:
        seq = int(m.get("seq") or 0)
        sender = str(m.get("sender") or "worker")
        disp_ch = board_mod.channel_display_name(m.get("channel"))
        summary = str(m.get("summary") or "")
        details = str(m.get("details") or "")
        full_text = f"{summary}\n\n{details}" if (summary and details and details != summary) else (details or summary)
        raw_mentions = [str(x).strip().lstrip("@") for x in (m.get("mentions") or []) if str(x).strip()]
        is_all = any(x.lower() == "all" for x in raw_mentions)
        mentions = [x for x in raw_mentions if x.lower() != "all"]
        ch_mentions_disp = [
            board_mod.channel_display_name(c)
            for c in (m.get("channelMentions") or [])
            if board_mod.channel_display_name(c) != "main"
        ]
        via_channel: list[str] = []
        if ch_mentions_disp:
            for mname, mrec in members_map.items():
                if not isinstance(mrec, dict) or mname == sender or mname in mentions:
                    continue
                eff = board_mod.effective_member_channels(mrec.get("role"), mrec.get("channels"), mname)
                if "*" in eff or any(f"#{c}" in eff for c in ch_mentions_disp):
                    via_channel.append(mname)

        ts_iso = str(m.get("createdAt") or "")
        ts_ms = _iso_to_epoch_ms(ts_iso)
        entry: dict[str, Any] = {
            "seq": seq,
            "id": m.get("id"),
            "agent": sender,
            "sender": sender,
            "senderRole": m.get("senderRole") or models.Role.WORKER.value,
            "senderTier": int(m.get("senderTier") or 2),
            "kind": m.get("kind") or m.get("type") or models.MessageType.POST.value,
            "type": m.get("type") or models.MessageType.POST.value,
            "text": full_text,
            "summary": summary,
            "details": details,
            "files": list(m.get("files") or []),
            "refs": list(m.get("refs") or []),
            "status": m.get("status") or "open",
            "mentions": mentions,
            "all": is_all,
            "replyTo": m.get("replyTo"),
            "time": ts_iso,
            "ts": ts_ms,
        }
        if disp_ch != "main":
            entry["channel"] = disp_ch
        if ch_mentions_disp:
            entry["channelMentions"] = ch_mentions_disp
            entry["channel_mentions"] = ch_mentions_disp
            entry["viaChannel"] = via_channel
            entry["via_channel"] = via_channel
        if isinstance(m.get("control"), dict):
            entry["control"] = m["control"]
            control_posts_count[sender] = control_posts_count.get(sender, 0) + 1
            last_control_by[sender] = entry
        else:
            posts_count[sender] = posts_count.get(sender, 0) + 1
            last_post_by[sender] = entry
            for rb in m.get("readBy") or []:
                rb_low = str(rb or "").strip().lower()
                if rb_low:
                    explicit_read_seqs.setdefault(rb_low, set()).add(seq)
        messages_out.append(entry)

    # Members list for hive.js & app.js
    now_ms = int(time.time() * 1000)
    subagents_map = board.get("subagents") if isinstance(board.get("subagents"), dict) else {}
    members_out: list[dict[str, Any]] = []
    unseen_by: dict[str, list[int]] = {}
    system_attention: list[dict[str, Any]] = []
    stuck_keys: list[str] = []
    raw_task_by_id = {str(t.get("id") or ""): t for t in raw_tasks if isinstance(t, dict) and t.get("id")}

    for name, mem in members_map.items():
        if not isinstance(mem, dict):
            continue
        role = models.normalize_role(mem.get("role"))
        killed = bool(mem.get("killed")) or str(mem.get("state") or "").lower() == models.MemberState.KILLED.value
        trow = latest_task_by_worker.get(name)
        ui_status = _member_status_for_ui(str(mem.get("state") or ""), killed=killed)
        if not killed and trow:
            if trow["state"] in (models.TaskState.WORKING.value, models.TaskState.SUBMITTED.value):
                ui_status = "working"
            elif trow["state"] in models.TERMINAL_STATES and ui_status == "working":
                ui_status = "waiting"

        activity = ""
        activity_kind = ""
        cur_tool = (trow or {}).get("currentTool") if ui_status == "working" else None
        if ui_status == "working" and trow:
            if cur_tool and cur_tool.get("name"):
                prev_txt = str(cur_tool.get("preview") or "").strip()
                activity = f"Running {cur_tool['name']}({prev_txt})" if prev_txt else f"Running {cur_tool['name']}"
                activity_kind = "tool"
            elif trow.get("lastTool"):
                activity = f"Running {trow['lastTool']}"
                activity_kind = "tool"
            elif trow.get("lastTextPreview"):
                activity = str(trow["lastTextPreview"])[-180:]
                activity_kind = "thinking"

        is_coord = role == models.Role.COORDINATOR.value
        is_role_worker = role in (models.Role.MONITOR.value, models.Role.NARRATOR.value) or bool(_ROLE_WORKER_RE.match(name))
        conv_id = meta.get("coordinatorConversationId") if is_coord else (mem.get("conversationId") or "")
        eff_chs = board_mod.effective_member_channels(role, mem.get("channels"), name)
        disp_chs = ["*" if c == "*" else board_mod.channel_display_name(c) for c in eff_chs]
        last_read = int(mem.get("lastReadSeq") or 0)
        vis_unread, unseen_seqs = board_mod.compute_visible_unread(name, mem, raw_msgs, raw_channels)
        unseen_by[name] = unseen_seqs
        wait_since_raw = mem.get("waitSince")
        try:
            wait_since_seq = int(wait_since_raw) if wait_since_raw is not None else 0
        except (TypeError, ValueError):
            wait_since_seq = 0

        lp = last_post_by.get(name) or last_control_by.get(name)
        lp_seq = max(int(lp["seq"]) if lp else 0, int(mem.get("lastPostSeq") or 0))
        latest_mention_seq = 0
        for m_entry in messages_out:
            if isinstance(m_entry.get("control"), dict):
                continue
            if str(m_entry.get("sender") or "").lower() == name.lower():
                continue
            m_seq = int(m_entry.get("seq") or 0)
            m_ments = [str(x).lower() for x in (m_entry.get("mentions") or [])]
            if m_entry.get("all") or name.lower() in m_ments or name.lower() in [str(x).lower() for x in (m_entry.get("viaChannel") or [])]:
                if m_seq > latest_mention_seq:
                    latest_mention_seq = m_seq
        replied = bool(latest_mention_seq == 0 or lp_seq > latest_mention_seq)
        reply_ask = latest_mention_seq if not replied else None

        member_failure: dict[str, Any] | None = None
        if not killed and trow and trow.get("state") == models.TaskState.FAILED.value and ui_status != "working":
            raw_t = raw_task_by_id.get(str(trow.get("taskId") or "")) or {}
            t_meta_fail = (raw_t.get("metadata") or {}).get("failure")
            err_txt = str((t_meta_fail or {}).get("text") if isinstance(t_meta_fail, dict) else trow.get("lastError") or "")
            ended_ms = _iso_to_epoch_ms(trow.get("endedAt") or (t_meta_fail or {}).get("since") if isinstance(t_meta_fail, dict) else None)
            if (now_ms - ended_ms) >= STEP_ERROR_SETTLE_MS or isinstance(t_meta_fail, dict):
                f_kind = (
                    str(t_meta_fail.get("kind"))
                    if isinstance(t_meta_fail, dict) and t_meta_fail.get("kind")
                    else ("quota" if QUOTA_RE.search(err_txt) else "error")
                )
                member_failure = {"kind": f_kind, "text": err_txt[:300], "since": ended_ms}
                system_attention.append({
                    "id": f"fail:{name}:{trow.get('taskId') or ended_ms}",
                    "kind": f_kind,
                    "member": name,
                    "conversation_id": conv_id or "",
                    "text": f"@{name} {f_kind}: {err_txt[:140]}" if err_txt else f"@{name} {f_kind}",
                    "detail": err_txt[:300],
                    "since": ended_ms,
                })

        raw_subs = list(mem.get("subagents") or subagents_map.get(name) or [])
        if not killed and ui_status == "working" and trow:
            cand_ts = [
                (cur_tool or {}).get("startedAt"),
                trow.get("lastEventAt"),
                mem.get("lastHeartbeatAt"),
                mem.get("updatedAt"),
                trow.get("createdAt"),
            ]
            has_fresh_working_sub = False
            for s in raw_subs:
                if not isinstance(s, dict):
                    continue
                if str(s.get("status") or s.get("state") or "") == "working":
                    sub_ts = s.get("updatedAt")
                    if sub_ts:
                        cand_ts.append(sub_ts)
                        if (now_ms - _iso_to_epoch_ms(sub_ts)) < STUCK_MS:
                            has_fresh_working_sub = True
                    else:
                        has_fresh_working_sub = True
            act_ms_list = [_iso_to_epoch_ms(ts_v) for ts_v in cand_ts if ts_v]
            act_ms = max(act_ms_list) if act_ms_list else now_ms
            if not has_fresh_working_sub and (now_ms - act_ms) >= STUCK_MS:
                stuck_keys.append(f"{name}:{act_ms}")
                system_attention.append({
                    "id": f"stuck:{name}:{act_ms}",
                    "kind": "stuck",
                    "member": name,
                    "conversation_id": conv_id or "",
                    "text": f"@{name} has been working without progress for {int((now_ms - act_ms) // 60000)}m",
                    "detail": activity or "",
                    "since": act_ms,
                })
        member_subs: list[dict[str, Any]] = []
        for s in raw_subs:
            if not isinstance(s, dict):
                continue
            sub_tok_raw = s.get("tokens") if isinstance(s.get("tokens"), dict) else {}
            sub_in = int(sub_tok_raw.get("input") or sub_tok_raw.get("input_tokens") or 0)
            sub_out = int(sub_tok_raw.get("output") or sub_tok_raw.get("output_tokens") or 0)
            sub_pure = int(
                sub_tok_raw["pureInput"]
                if sub_tok_raw.get("pureInput") is not None
                else (sub_tok_raw["uncachedInput"] if sub_tok_raw.get("uncachedInput") is not None else sub_in)
            )
            sub_cached = int(
                sub_tok_raw["cachedInput"]
                if sub_tok_raw.get("cachedInput") is not None
                else (sub_tok_raw.get("cacheRead") or 0)
            )
            sub_cw = int(sub_tok_raw.get("cacheWrite") or 0)
            sub_tot = int(sub_tok_raw.get("total") or sub_tok_raw.get("total_tokens") or (sub_in + sub_out))
            sub_proc = int(
                sub_tok_raw["totalProcessed"]
                if sub_tok_raw.get("totalProcessed") is not None
                else (sub_tot or (sub_pure + sub_cached + sub_cw + sub_out))
            )
            sub_ctx_lim = int(sub_tok_raw.get("contextLimit") or 0)
            sub_el = int(s.get("elapsedMs") or 0)
            sub_tps = float(
                s.get("tps") if s.get("tps") is not None else runtime_mod._compute_tps(sub_out, sub_el)
            )
            sub_tps_est = bool(s.get("tpsEstimated")) if "tpsEstimated" in s else (s.get("tps") is None and sub_out > 0 and sub_el > 0)
            member_subs.append({
                "id": str(s.get("id") or ""),
                "name": str(s.get("name") or s.get("label") or s.get("id") or "subagent"),
                "task": str(s.get("task") or s.get("label") or s.get("summary") or ""),
                "status": str(s.get("status") or s.get("state") or "working"),
                "model": s.get("model"),
                "steps": int(s.get("steps") or 0),
                "toolCalls": int(s.get("toolCalls") or 0),
                "recentTools": _normalize_recent_tools(s.get("recentTools"), limit=5),
                "tokens": {
                    "input": sub_in,
                    "output": sub_out,
                    "total": sub_tot,
                    "pureInput": sub_pure,
                    "cachedInput": sub_cached,
                    "cacheWrite": sub_cw,
                    "totalProcessed": sub_proc,
                    "contextLimit": sub_ctx_lim,
                },
                "tps": sub_tps,
                "tpsEstimated": sub_tps_est,
                "elapsedMs": sub_el,
                "summary": str(s.get("summary") or ""),
            })
        member_tokens = dict((trow or {}).get("tokens") or runtime_mod._normalize_tokens_dict({}))
        m_pure, m_cached, m_cw, m_out, m_proc, _ = _extract_token_economy(member_tokens)
        member_tokens.setdefault("pureInput", m_pure)
        member_tokens.setdefault("cachedInput", m_cached)
        member_tokens.setdefault("totalProcessed", m_proc)
        member_tokens.setdefault("contextLimit", int(member_tokens.get("contextLimit") or 0))
        post_count = posts_count.get(name, 0) or control_posts_count.get(name, 0) or (1 if lp_seq > 0 else 0)
        name_low = name.lower()
        seen_by_rb = explicit_read_seqs.get(name_low, set())
        read_count = sum(
            1
            for m_entry in messages_out
            if not isinstance(m_entry.get("control"), dict)
            and (
                (last_read > 0 and 0 < int(m_entry.get("seq") or 0) <= last_read)
                or int(m_entry.get("seq") or 0) in seen_by_rb
            )
        )
        members_out.append({
            "name": name,
            "role": role,
            "tier": int(mem.get("tier") or 2),
            "runtime": mem.get("runtime") or ("antigravity" if is_coord else "hermes"),
            "model": (trow or {}).get("model") or mem.get("model") or "",
            "provider": (trow or {}).get("provider") or mem.get("provider") or "",
            "status": ui_status,
            "killed": killed,
            "assignment": mem.get("assignment") or "",
            "activity": activity,
            "activityKind": activity_kind,
            "steps": int((trow or {}).get("steps") or 0),
            "toolCalls": int((trow or {}).get("toolCalls") or 0),
            "currentTool": cur_tool,
            "recentTools": list((trow or {}).get("recentTools") or []),
            "tps": float((trow or {}).get("tps") or 0.0),
            "tpsEstimated": bool((trow or {}).get("tpsEstimated")),
            "elapsedMs": int((trow or {}).get("elapsedMs") or 0),
            "posts": post_count,
            "postCount": post_count,
            "readCount": read_count,
            "lastPostSeq": lp_seq,
            "lastPostTime": lp["time"] if lp else "",
            "lastPostTs": lp["ts"] if lp else None,
            "lastReadSeq": last_read,
            "last_read_seq": last_read,
            "unread": vis_unread,
            "visibleUnread": vis_unread,
            "waitSince": wait_since_seq,
            "wait_since": wait_since_seq,
            "channels": disp_chs,
            "roleWorker": is_role_worker,
            "narrator": role == models.Role.NARRATOR.value,
            "replied": replied,
            "replyAsk": reply_ask,
            "failure": member_failure,
            "conversationId": conv_id or "",
            "conversation_id": conv_id or "",
            "taskId": (trow or {}).get("taskId") or mem.get("taskId"),
            "contextId": (trow or {}).get("contextId") or mem.get("contextId"),
            "worktreePath": (trow or {}).get("worktreePath") or mem.get("worktreePath"),
            "worktreeBranch": (trow or {}).get("worktreeBranch") or mem.get("worktreeBranch"),
            "tokens": member_tokens,
            "costUsd": (trow or {}).get("costUsd"),
            "subagents": member_subs,
        })

    members_out.sort(key=lambda m: (0 if m["role"] == models.Role.COORDINATOR.value else 1, m["name"]))

    narrator_member = _pick_narrator(members_out)
    narrator_conv_id = str((narrator_member or {}).get("conversationId") or "").strip() or None
    view_info = _read_narrator_view(paths.swarm_dir, narrator_conv_id=narrator_conv_id)
    frames = board_mod.extract_screenshots(
        board,
        raw_tasks,
        workspace_path=meta.get("workspace"),
    )
    narrator_state = "absent"
    if narrator_member:
        narrator_trow = latest_task_by_worker.get(str(narrator_member.get("name") or ""))
        if narrator_member["killed"]:
            narrator_state = "killed"
        elif (
            narrator_trow
            and narrator_trow.get("state") == models.TaskState.FAILED.value
            and narrator_member["status"] != "working"
        ):
            narrator_state = "paused"
            err_txt = str(narrator_trow.get("lastError") or "")
            narrator_member["reason"] = "quota" if QUOTA_RE.search(err_txt) else "error"
            narrator_member["detail"] = err_txt[:300]
        else:
            narrator_state = "working" if narrator_member["status"] == "working" else "waiting"
        narrator_member["state"] = narrator_state
        if narrator_trow and narrator_trow.get("endedAt"):
            narrator_member["transcriptMtime"] = _iso_to_epoch_ms(narrator_trow["endedAt"])
        if view_info.get("exists") and view_info.get("jsonMtime"):
            observed_by_view = max(
                (int(m.get("seq") or 0) for m in messages_out if (m.get("ts") or 0) <= view_info["jsonMtime"]),
                default=0,
            )
            if observed_by_view > narrator_member["lastReadSeq"]:
                narrator_member["lastReadSeq"] = observed_by_view
                narrator_member["last_read_seq"] = observed_by_view
                narrator_member["unread"] = max(0, head - observed_by_view)

    # Edges graph (@mention / @all / via_channel)
    edges_out: list[dict[str, Any]] = []
    for m_entry in messages_out:
        if isinstance(m_entry.get("control"), dict):
            continue
        seq = int(m_entry.get("seq") or 0)
        sender_name = str(m_entry.get("sender") or "")
        for tgt in m_entry.get("mentions") or []:
            edges_out.append({"from": sender_name, "to": tgt, "seq": seq, "kind": "mention"})
        if m_entry.get("all"):
            edges_out.append({"from": sender_name, "to": "all", "seq": seq, "kind": "all"})
        for tgt in m_entry.get("viaChannel") or []:
            edges_out.append({"from": sender_name, "to": tgt, "seq": seq, "kind": "via_channel"})

    allowed_files: dict[str, str] = {}
    for cand_af in [
        str(paths.swarm_dir),
        str(meta.get("workspace") or ""),
        str(view_info.get("dir") or ""),
        *[str(f.get("path") or "") for f in frames if isinstance(f, dict)],
    ]:
        clean_af = str(cand_af or "").strip()
        if not clean_af:
            continue
        norm_af = store.normalize_local_path(clean_af)
        if not norm_af:
            continue
        try:
            file_uri = Path(norm_af).resolve().as_uri()
        except Exception:
            continue
        allowed_files.setdefault(norm_af, file_uri)
        slash_af = norm_af.replace("\\", "/")
        if slash_af != norm_af:
            allowed_files.setdefault(slash_af, file_uri)

    url_hosts = _load_url_hosts()
    builtin_overview = _env_flag("SWARM_OVERVIEW", True)
    graph_tab = _env_flag("SWARM_GRAPH_TAB", True)
    raw_tab = _env_flag("SWARM_RAW_TAB", True)

    telemetry_rollup = compute_swarm_telemetry(members_out)
    archive_count = board_mod.count_archived_messages(paths.board_file)
    plan = _normalize_plan_for_snapshot(
        board.get("plan") if isinstance(board.get("plan"), dict) else None,
        members_out=members_out,
        latest_task_by_worker=latest_task_by_worker,
        last_post_by=last_post_by,
        messages_out=messages_out,
    )

    title = str(meta.get("title") or board.get("title") or sid)
    req_member = str(member or "").strip()
    etag_src = json.dumps(
        {
            "sid": sid,
            "member": req_member,
            "title": title,
            "head": head,
            "boardUpdated": board.get("updatedAt"),
            "metaUpdated": meta.get("updatedAt"),
            "planUpdated": (plan or {}).get("updatedAt"),
            "allowedFiles": sorted(allowed_files.keys()),
            "stuckKeys": stuck_keys,
            "urlHosts": url_hosts,
            "members": [
                (
                    m["name"],
                    m["status"],
                    m["killed"],
                    m["steps"],
                    m["toolCalls"],
                    (m.get("currentTool") or {}).get("name"),
                    (m.get("currentTool") or {}).get("startedAt"),
                    (m.get("tokens") or {}).get("total"),
                    (m.get("tokens") or {}).get("contextUsed"),
                    m.get("tps"),
                    len(m.get("subagents") or []),
                    [
                        (
                            s.get("id"),
                            s.get("status"),
                            s.get("steps"),
                            s.get("toolCalls"),
                            (s.get("tokens") or {}).get("total"),
                        )
                        for s in (m.get("subagents") or [])
                    ],
                )
                for m in members_out
            ],
            "tasks": [
                (
                    r["taskId"],
                    r["state"],
                    r["steps"],
                    r["toolCalls"],
                    r["lastTool"],
                    (r.get("tokens") or {}).get("total"),
                )
                for r in task_rows[:30]
            ],
            "telemetry": telemetry_rollup,
            "viewExists": view_info.get("exists"),
            "viewMtime": view_info.get("jsonMtime"),
            "viewJsonErr": view_info.get("jsonError"),
            "viewHtmlKey": view_info.get("htmlKey"),
            "viewHtmlErr": view_info.get("htmlError"),
            "framesCount": len(frames),
            "archiveCount": archive_count,
        },
        sort_keys=True,
    )
    etag_digest = hashlib.sha1(etag_src.encode("utf-8")).hexdigest()[:16]
    etag = f"{sid}|{req_member}|{etag_digest}"
    known_clean = str(known_etag or "").strip().strip('"')
    known_digest = known_clean.rsplit("|", 1)[-1] if "|" in known_clean else ""
    if known_clean.startswith(f"{sid}|") and known_digest == etag_digest:
        raw_blob: dict[str, Any] = {}
    else:
        archived_tail = board_mod.read_archived_messages(paths.board_file)[-200:] if archive_count else []
        raw_blob = {
            "board": json.dumps(board, indent=2, ensure_ascii=False),
            "meta": json.dumps(meta, indent=2, ensure_ascii=False),
            "members": json.dumps({m["name"]: m for m in members_out}, indent=2, ensure_ascii=False),
            "view": json.dumps(view_info["json"], indent=2, ensure_ascii=False) if view_info.get("json") else "",
            "archive": json.dumps(archived_tail, indent=2, ensure_ascii=False) if archive_count else "",
        }

    return {
        "schemaVersion": models.SCHEMA_VERSION,
        "swarm": sid,
        "id": sid,
        "swarmId": sid,
        "title": title,
        "convTitle": title,
        "goal": meta.get("goal") or board.get("goal") or "",
        "mission": meta.get("mission") or board.get("mission") or "",
        "meta": meta,
        "workspace": meta.get("workspace"),
        "dir": str(paths.swarm_dir),
        "primaryConversationId": meta.get("primaryConversationId"),
        "coordinatorConversationId": meta.get("coordinatorConversationId"),
        "conversationId": meta.get("primaryConversationId"),
        "conversationIds": list(meta.get("conversationIds") or []),
        "projectId": meta.get("projectId"),
        "head": head,
        "etag": etag,
        "telemetry": telemetry_rollup,
        "members": members_out,
        "messages": messages_out,
        "channels": channels_out,
        "claims": dict(board.get("claims") or {}),
        "unseenBy": unseen_by,
        "edges": edges_out,
        "systemAttention": system_attention,
        "allowedFiles": allowed_files,
        "urlHosts": url_hosts,
        "plan": plan,
        "frames": frames,
        "view": view_info,
        "narrator": narrator_member,
        "narratorState": narrator_state,
        "builtinOverview": builtin_overview,
        "graphTab": graph_tab,
        "rawTab": raw_tab,
        "archiveCount": archive_count,
        "tasks": task_rows,
        "activeTasks": active_tasks,
        "recentCompletedTasks": recent_completed,
        "raw": raw_blob,
    }


def _resolve_frame_candidate_path(
    raw_path: str,
    *,
    swarm_dir: Path,
    ws_root: str | None = None,
) -> Path | None:
    """Resolve a frame path (absolute or relative) against ws_root -> .worktrees/* -> swarm_dir without process CWD."""
    norm = store.normalize_local_path(str(raw_path or "").strip())
    if not norm:
        return None
    cand_path = Path(norm)
    ws_clean = str(ws_root or "").strip()
    ws_norm = Path(store.normalize_local_path(ws_clean)) if ws_clean else None

    if not cand_path.is_absolute():
        if ws_norm is not None:
            ws_cand = ws_norm / cand_path
            if ws_cand.is_file():
                return ws_cand
            wt_dir = ws_norm / ".worktrees"
            if wt_dir.is_dir():
                try:
                    for wt_entry in sorted(wt_dir.iterdir()):
                        if wt_entry.is_dir():
                            wt_cand = wt_entry / cand_path
                            if wt_cand.is_file():
                                return wt_cand
                except OSError:
                    pass
        swarm_cand = swarm_dir / cand_path
        if swarm_cand.is_file():
            return swarm_cand
        return (ws_norm / cand_path) if ws_norm is not None else swarm_cand

    if not cand_path.is_file() and ws_norm is not None:
        try:
            real_cand = Path(os.path.realpath(cand_path))
            real_ws = Path(os.path.realpath(ws_norm))
            wt_base = real_ws / ".worktrees"
            if real_cand.is_relative_to(wt_base):
                rel_from_wt = real_cand.relative_to(wt_base)
                if len(rel_from_wt.parts) >= 2:
                    main_cand = real_ws / Path(*rel_from_wt.parts[1:])
                    if main_cand.is_file():
                        return main_cand
            elif real_cand.is_relative_to(real_ws) and wt_base.is_dir():
                rel_from_ws = real_cand.relative_to(real_ws)
                for wt_entry in sorted(wt_base.iterdir()):
                    if wt_entry.is_dir():
                        wt_cand = wt_entry / rel_from_ws
                        if wt_cand.is_file():
                            return wt_cand
        except (ValueError, OSError):
            pass
    return cand_path


def _resolve_authorized_frame_paths(
    manager: runtime_mod.SwarmManager,
    swarm_id: str,
) -> set[str]:
    sid = store.sanitize_swarm_id(swarm_id) or "default"
    paths = manager.store.paths(sid)
    meta = manager.store.read_meta(sid) or {}
    ws_root = meta.get("workspace")
    board = manager.board_service.read_board(sid)
    tasks = manager.get_runtime(sid).get_task_snapshots()

    authorized: set[str] = set()
    for s in board_mod.extract_screenshots(board, tasks, workspace_path=ws_root, limit=200):
        raw_p = str(s.get("path") or "")
        if raw_p:
            resolved_p = _resolve_frame_candidate_path(raw_p, swarm_dir=paths.swarm_dir, ws_root=ws_root)
            for p_cand in (Path(raw_p), resolved_p):
                if p_cand is None:
                    continue
                try:
                    authorized.add(os.path.normcase(os.path.realpath(p_cand)))
                except OSError:
                    pass
    view_info = _read_narrator_view(paths.swarm_dir)
    vjson = view_info.get("json") if isinstance(view_info.get("json"), dict) else {}
    for fr in ((vjson.get("reel") or {}).get("frames") or []):
        if isinstance(fr, dict) and isinstance(fr.get("path"), str):
            resolved_fr = _resolve_frame_candidate_path(fr["path"], swarm_dir=paths.swarm_dir, ws_root=ws_root)
            if resolved_fr is not None:
                try:
                    authorized.add(os.path.normcase(os.path.realpath(resolved_fr)))
                except OSError:
                    pass
    return authorized


async def _sse_task_stream(
    runtime: runtime_mod.SwarmRuntime,
    task_id: str,
    req_id: Any,
    *,
    initial_task: dict[str, Any],
    is_rest: bool = False,
) -> AsyncIterator[str]:
    q = runtime.subscribe(task_id)
    with _SSE_SUBSCRIBERS_LOCK:
        _ACTIVE_SSE_QUEUES.add(q)
    try:
        if is_rest:
            yield f"event: snapshot\ndata: {json.dumps({'task': initial_task}, ensure_ascii=False)}\n\n"
        else:
            yield f"data: {json.dumps({'jsonrpc': '2.0', 'id': req_id, 'result': {'task': initial_task}}, ensure_ascii=False)}\n\n"

        if ((initial_task.get("status") or {}).get("state")) in models.TERMINAL_STATES:
            return
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            except asyncio.CancelledError:
                break
            if ev is None:
                try:
                    final_task = await runtime.run_on_loop(runtime.get_task, task_id, include_artifacts=True)
                    if is_rest:
                        yield f"event: done\ndata: {json.dumps({'task': final_task}, ensure_ascii=False)}\n\n"
                    else:
                        yield f"data: {json.dumps({'jsonrpc': '2.0', 'id': req_id, 'result': {'task': final_task}}, ensure_ascii=False)}\n\n"
                except Exception:
                    pass
                break
            if is_rest:
                yield f"event: update\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
            else:
                yield f"data: {json.dumps({'jsonrpc': '2.0', 'id': req_id, 'result': ev}, ensure_ascii=False)}\n\n"
    finally:
        with _SSE_SUBSCRIBERS_LOCK:
            _ACTIVE_SSE_QUEUES.discard(q)
        runtime.unsubscribe(task_id, q)


def _resolve_member_for_conversation(snap: dict[str, Any], conv_id: str | None) -> str | None:
    if not conv_id:
        return None
    clean = str(conv_id).strip()
    if not clean:
        return None
    if str(snap.get("coordinatorConversationId") or "").strip() == clean:
        return models.Role.COORDINATOR.value
    for m in snap.get("members") or []:
        if isinstance(m, dict) and str(m.get("conversationId") or "").strip() == clean:
            name = str(m.get("name") or "").strip()
            if name:
                return name
    return None


async def _sse_board_events_stream(
    manager: runtime_mod.SwarmManager,
    *,
    swarm_id: str | None = None,
    conversation_id: str | None = None,
    since_seq: int = 0,
) -> AsyncIterator[str]:
    sid: str | None = store.sanitize_swarm_id(swarm_id) or None if swarm_id else None
    clean_conv = str(conversation_id or "").strip() or None
    if sid is None and clean_conv:
        sid = await asyncio.to_thread(manager.store.resolve_swarm_for_conversation, clean_conv)

    if sid is None:
        empty_payload = {"conversationId": clean_conv, "swarm": None, "member": None}
        yield f"id: 0\nevent: snapshot\ndata: {json.dumps(empty_payload, ensure_ascii=False)}\n\n"
        last_keepalive = time.monotonic()
        poll_delay = 0.5
        while sid is None:
            try:
                await asyncio.sleep(poll_delay)
            except asyncio.CancelledError:
                return
            poll_delay = min(5.0, poll_delay * 1.5)
            if clean_conv:
                sid = await asyncio.to_thread(manager.store.resolve_swarm_for_conversation, clean_conv)
            if sid is None and (time.monotonic() - last_keepalive) >= 15.0:
                last_keepalive = time.monotonic()
                yield ": keepalive\n\n"

    q = manager.subscribe_board(sid)
    with _SSE_SUBSCRIBERS_LOCK:
        _ACTIVE_SSE_QUEUES.add(q)
    try:
        snap = await asyncio.to_thread(build_observer_snapshot, manager, sid)
        if clean_conv:
            snap["member"] = _resolve_member_for_conversation(snap, clean_conv)
        last_etag = snap.get("etag") or ""
        yield f"id: {snap.get('head', 0)}\nevent: snapshot\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"

        if since_seq > 0:
            for missed in manager.get_wake_history(sid, since_seq=since_seq):
                wtype = str(missed.get("type") or "")
                if wtype in ("coordinator_wake", "primary_wake"):
                    yield f"id: {missed.get('seq', 0)}\nevent: {wtype}\ndata: {json.dumps(missed, ensure_ascii=False)}\n\n"

        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            except asyncio.CancelledError:
                break

            if ev is None:
                break

            ev_type = str(ev.get("type") or "")
            if ev_type in ("coordinator_wake", "primary_wake"):
                yield f"id: {ev.get('seq', 0)}\nevent: {ev_type}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                continue

            # Coalesce rapid updates before emitting snapshot delta.
            shutting_down = False
            while not q.empty():
                try:
                    nxt = q.get_nowait()
                    if nxt is None:
                        shutting_down = True
                        break
                    nt = str(nxt.get("type") or "")
                    if nt in ("coordinator_wake", "primary_wake"):
                        yield f"id: {nxt.get('seq', 0)}\nevent: {nt}\ndata: {json.dumps(nxt, ensure_ascii=False)}\n\n"
                except asyncio.QueueEmpty:
                    break
            if shutting_down:
                break

            snap = await asyncio.to_thread(build_observer_snapshot, manager, sid)
            if clean_conv:
                snap["member"] = _resolve_member_for_conversation(snap, clean_conv)
            cur_etag = snap.get("etag") or ""
            if cur_etag != last_etag:
                last_etag = cur_etag
                yield f"id: {snap.get('head', 0)}\nevent: update\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
    finally:
        with _SSE_SUBSCRIBERS_LOCK:
            _ACTIVE_SSE_QUEUES.discard(q)
        manager.unsubscribe_board(sid, q)


async def _sse_internal_wake_events_stream(
    manager: runtime_mod.SwarmManager,
    *,
    since_map: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    q = manager.subscribe_wake_events()
    with _SSE_SUBSCRIBERS_LOCK:
        _ACTIVE_SSE_QUEUES.add(q)
    try:
        active_swarms = await asyncio.to_thread(manager.list_active_wake_swarms)
        ready_payload = {"ok": True, "swarms": active_swarms}
        yield f"event: ready\ndata: {json.dumps(ready_payload, ensure_ascii=False)}\n\n"

        sent_keys: OrderedDict[tuple[str, str, int, int], None] = OrderedDict()
        sent_board_keys: OrderedDict[tuple[str, str, int], None] = OrderedDict()

        def _remember(ev: dict[str, Any]) -> bool:
            sid = str(ev.get("swarmId") or "")
            etype = str(ev.get("type") or "")
            seq_v = int(ev.get("seq") or 0)
            wseq_v = int(ev.get("wakeSeq") or 0)
            full_key = (sid, etype, seq_v, wseq_v)
            if full_key in sent_keys:
                return False
            if not ev.get("synthetic") and seq_v > 0:
                bkey = (sid, etype, seq_v)
                if bkey in sent_board_keys:
                    return False
                sent_board_keys[bkey] = None
                sent_board_keys.move_to_end(bkey)
                while len(sent_board_keys) > 4096:
                    sent_board_keys.popitem(last=False)
            sent_keys[full_key] = None
            sent_keys.move_to_end(full_key)
            while len(sent_keys) > 4096:
                sent_keys.popitem(last=False)
            return True

        if since_map:
            catchup = await asyncio.to_thread(manager.get_wake_catchup_events, since_map)
            for ev in catchup:
                etype = str(ev.get("type") or "")
                if etype not in ("coordinator_wake", "primary_wake"):
                    continue
                if not _remember(ev):
                    continue
                ev_id = int(ev.get("wakeSeq") or ev.get("seq") or 0)
                yield f"id: {ev_id}\nevent: {etype}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            except asyncio.CancelledError:
                break

            if ev is None:
                break

            etype = str(ev.get("type") or "")
            if etype not in ("coordinator_wake", "primary_wake"):
                continue
            if not _remember(ev):
                continue
            ev_id = int(ev.get("wakeSeq") or ev.get("seq") or 0)
            yield f"id: {ev_id}\nevent: {etype}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
    finally:
        with _SSE_SUBSCRIBERS_LOCK:
            _ACTIVE_SSE_QUEUES.discard(q)
        manager.unsubscribe_wake_events(q)


def create_a2a_app(
    manager: runtime_mod.SwarmManager,
    *,
    http_url: str = "http://127.0.0.1:9910",
    bearer_token: str | None = None,
) -> Starlette:
    """Create a Starlette ASGI app exposing A2A v1.0, read-only observer routes, and internal CLI hook."""

    async def _resolve_scope(
        request: Request,
        body: dict[str, Any] | None = None,
    ) -> tuple[str, runtime_mod.SwarmRuntime]:
        s_hint, c_hint, w_hint = _extract_scope_hints(request, body)
        sid = await asyncio.to_thread(
            manager.resolve_swarm_id,
            swarm_id=s_hint,
            conversation_id=c_hint,
            workspace=w_hint,
        )
        rt = await asyncio.to_thread(manager.get_runtime, sid)
        return sid, rt

    def _wake_all_sse_subscribers() -> None:
        with _SSE_SUBSCRIBERS_LOCK:
            qs = list(_ACTIVE_SSE_QUEUES)
        for q in qs:
            try:
                q.put_nowait(None)
            except Exception:
                pass

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            manager.set_host_loop(asyncio.get_running_loop())
        except RuntimeError:
            pass
        try:
            yield
        finally:
            _wake_all_sse_subscribers()
            await manager.shutdown()

    async def agent_card(request: Request) -> Response:
        if (denied := _check_auth(request, bearer_token)) is not None:
            return denied
        profile = request.query_params.get("profile") or "default"
        cat = await asyncio.to_thread(catalog_mod.get_hermes_catalog, profile)
        return JSONResponse(models.build_hermes_agent_card(cat, profile=profile, http_url=http_url))

    async def rpc_endpoint(request: Request) -> Response:
        if (denied := _check_auth(request, bearer_token)) is not None:
            return denied
        raw_bytes, size_err = await _read_bounded_body(request)
        if size_err is not None:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request body too large"}},
                status_code=413,
            )
        assert raw_bytes is not None
        try:
            body = json.loads(raw_bytes.decode("utf-8"))
        except Exception:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}, status_code=400)

        req_id = body.get("id")
        method = str(body.get("method") or "")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        _, runtime = await _resolve_scope(request, params)

        def _rpc_ok(result: Any) -> JSONResponse:
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})

        def _rpc_err(code: int, message: str) -> JSONResponse:
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

        try:
            if method in ("SendMessage", "message/send"):
                msg = params.get("message") or {}
                parts = msg.get("parts") or ([models.text_part(str(msg["text"]))] if "text" in msg else [])
                conf = params.get("configuration") if isinstance(params.get("configuration"), dict) else {}
                wcfg = _extract_worker_config(params)
                task = await runtime.run_on_loop(
                    runtime.send_message,
                    message_parts=parts,
                    context_id=msg.get("contextId") or params.get("contextId"),
                    task_id=msg.get("taskId") or params.get("taskId"),
                    config=wcfg,
                    return_immediately=bool(conf.get("returnImmediately", False)),
                    history_length=conf.get("historyLength"),
                )
                return _rpc_ok({"task": task})

            if method in ("SendStreamingMessage", "message/stream"):
                msg = params.get("message") or {}
                parts = msg.get("parts") or ([models.text_part(str(msg["text"]))] if "text" in msg else [])
                conf = params.get("configuration") if isinstance(params.get("configuration"), dict) else {}
                wcfg = _extract_worker_config(params)
                task = await runtime.run_on_loop(
                    runtime.send_message,
                    message_parts=parts,
                    context_id=msg.get("contextId") or params.get("contextId"),
                    task_id=msg.get("taskId") or params.get("taskId"),
                    config=wcfg,
                    return_immediately=True,
                    history_length=conf.get("historyLength"),
                )
                with _SSE_SUBSCRIBERS_LOCK:
                    if len(_ACTIVE_SSE_QUEUES) >= _MAX_SSE_SUBSCRIBERS:
                        return JSONResponse({"error": "Too many active SSE subscribers"}, status_code=503)
                return StreamingResponse(
                    _sse_task_stream(runtime, task["id"], req_id, initial_task=task, is_rest=False),
                    media_type="text/event-stream",
                )

            if method in ("GetTask", "tasks/get"):
                task_id = str(params.get("id") or params.get("taskId") or "")
                task = await runtime.run_on_loop(
                    runtime.get_task,
                    task_id,
                    history_length=params.get("historyLength"),
                    include_artifacts=bool(params.get("includeArtifacts", True)),
                )
                return _rpc_ok(task)

            if method in ("ListTasks", "tasks/list"):
                res = await runtime.run_on_loop(
                    runtime.list_tasks,
                    context_id=params.get("contextId"),
                    state=params.get("status"),
                    include_artifacts=bool(params.get("includeArtifacts", False)),
                    history_length=params.get("historyLength"),
                    page_size=int(params.get("pageSize") or 50),
                    page_token=str(params.get("pageToken") or ""),
                )
                return _rpc_ok(res)

            if method in ("CancelTask", "tasks/cancel"):
                task_id = str(params.get("id") or params.get("taskId") or "")
                task = await runtime.run_on_loop(runtime.cancel_task, task_id)
                return _rpc_ok(task)

            if method in ("SubscribeToTask", "tasks/subscribe"):
                with _SSE_SUBSCRIBERS_LOCK:
                    if len(_ACTIVE_SSE_QUEUES) >= _MAX_SSE_SUBSCRIBERS:
                        return JSONResponse({"error": "Too many active SSE subscribers"}, status_code=503)
                task_id = str(params.get("id") or params.get("taskId") or "")
                task = await runtime.run_on_loop(runtime.get_task, task_id, include_artifacts=True)
                return StreamingResponse(
                    _sse_task_stream(runtime, task_id, req_id, initial_task=task, is_rest=False),
                    media_type="text/event-stream",
                )

            return _rpc_err(-32601, f"Method not found: {method}")
        except KeyError as exc:
            return _rpc_err(-32001, str(exc))
        except RuntimeError as exc:
            msg = str(exc)
            code = -32002 if "TaskNotCancelableError" in msg else -32000
            return _rpc_err(code, msg)
        except ValueError as exc:
            return _rpc_err(-32602, str(exc))
        except Exception as exc:
            return _rpc_err(-32603, str(exc))

    async def rest_send_message(request: Request) -> Response:
        if (denied := _check_auth(request, bearer_token)) is not None:
            return denied
        params, err_resp = await _read_json_body(request)
        if err_resp is not None:
            return err_resp
        assert params is not None
        _, runtime = await _resolve_scope(request, params)
        msg = params.get("message") if isinstance(params.get("message"), dict) else {}
        parts = msg.get("parts") or ([models.text_part(str(msg["text"]))] if "text" in msg else [])
        conf = params.get("configuration") if isinstance(params.get("configuration"), dict) else {}
        try:
            task = await runtime.run_on_loop(
                runtime.send_message,
                message_parts=parts,
                context_id=msg.get("contextId") or params.get("contextId"),
                task_id=msg.get("taskId") or params.get("taskId"),
                config=_extract_worker_config(params),
                return_immediately=bool(conf.get("returnImmediately", False)),
                history_length=conf.get("historyLength"),
            )
            return JSONResponse({"task": task})
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def rest_list_tasks(request: Request) -> Response:
        if (denied := _check_auth(request, bearer_token)) is not None:
            return denied
        _, runtime = await _resolve_scope(request)
        qp = request.query_params
        raw_ps = qp.get("pageSize")
        try:
            page_size = max(1, min(500, int(raw_ps) if raw_ps is not None and str(raw_ps).strip() != "" else 50))
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid pageSize"}, status_code=400)
        res = await runtime.run_on_loop(
            runtime.list_tasks,
            context_id=qp.get("contextId"),
            state=qp.get("status"),
            include_artifacts=qp.get("includeArtifacts", "false").lower() == "true",
            page_size=page_size,
            page_token=qp.get("pageToken") or "",
        )
        return JSONResponse(res)

    async def rest_get_task(request: Request) -> Response:
        if (denied := _check_auth(request, bearer_token)) is not None:
            return denied
        _, runtime = await _resolve_scope(request)
        task_id = request.path_params["task_id"]
        try:
            task = await runtime.run_on_loop(runtime.get_task, task_id, include_artifacts=True)
            return JSONResponse(task)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def rest_cancel_task(request: Request) -> Response:
        if (denied := _check_auth(request, bearer_token)) is not None:
            return denied
        _, runtime = await _resolve_scope(request)
        task_id = request.path_params["task_id"]
        try:
            task = await runtime.run_on_loop(runtime.cancel_task, task_id)
            return JSONResponse(task)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def rest_subscribe_task(request: Request) -> Response:
        if (denied := _check_auth(request, bearer_token)) is not None:
            return denied
        with _SSE_SUBSCRIBERS_LOCK:
            if len(_ACTIVE_SSE_QUEUES) >= _MAX_SSE_SUBSCRIBERS:
                return JSONResponse({"error": "Too many active SSE subscribers"}, status_code=503)
        _, runtime = await _resolve_scope(request)
        task_id = request.path_params["task_id"]
        try:
            task = await runtime.run_on_loop(runtime.get_task, task_id, include_artifacts=True)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        return StreamingResponse(
            _sse_task_stream(runtime, task_id, None, initial_task=task, is_rest=True),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- Read-only Observer Board Routes (1:1 session-bound) ---

    async def board_get(request: Request) -> Response:
        if (denied := _check_auth(request, bearer_token)) is not None:
            return denied
        _, conv_id, _ = _extract_scope_hints(request)
        if not conv_id:
            return JSONResponse({
                "conversationId": None,
                "swarm": None,
                "member": None,
            })

        sid = await asyncio.to_thread(manager.store.resolve_swarm_for_conversation, conv_id)
        if not sid:
            return JSONResponse({
                "conversationId": conv_id,
                "swarm": None,
                "member": None,
            })

        q_etag = (request.query_params.get("etag") or "").strip().strip('"')
        hdr_etag = (request.headers.get("if-none-match") or "").strip().strip('"')
        snap = await asyncio.to_thread(build_observer_snapshot, manager, sid, known_etag=(q_etag or hdr_etag))
        member_obj = _resolve_member_for_conversation(snap, conv_id)
        snap["member"] = member_obj
        member_name = str(member_obj or "")
        if member_name and "|" in str(snap.get("etag") or ""):
            digest_part = str(snap["etag"]).rsplit("|", 1)[-1]
            snap["etag"] = f"{sid}|{member_name}|{digest_part}"
        if q_etag and q_etag == snap.get("etag"):
            unchanged_body: dict[str, Any] = {
                "unchanged": True,
                "swarm": sid,
                "etag": snap["etag"],
                "primaryConversationId": snap.get("primaryConversationId") or conv_id,
            }
            if snap.get("coordinatorConversationId"):
                unchanged_body["coordinatorConversationId"] = snap["coordinatorConversationId"]
            return JSONResponse(unchanged_body)
        if hdr_etag and hdr_etag == snap.get("etag"):
            return Response(status_code=304, headers={"ETag": str(snap.get("etag") or "")})
        return JSONResponse(snap, headers={"ETag": str(snap.get("etag") or "")})

    async def board_events(request: Request) -> Response:
        if (denied := _check_auth(request, bearer_token)) is not None:
            return denied
        with _SSE_SUBSCRIBERS_LOCK:
            if len(_ACTIVE_SSE_QUEUES) >= _MAX_SSE_SUBSCRIBERS:
                return JSONResponse({"error": "Too many active SSE subscribers"}, status_code=503)
        manager._ensure_host_loop(asyncio.get_running_loop())
        s_hint, c_hint, _ = _extract_scope_hints(request)
        if not s_hint and not c_hint:
            return JSONResponse({"error": "conversationId or swarm is required"}, status_code=400)
        raw_since = request.query_params.get("since") or request.headers.get("last-event-id") or "0"
        try:
            since_seq = max(0, int(raw_since))
        except ValueError:
            since_seq = 0
        return StreamingResponse(
            _sse_board_events_stream(
                manager,
                swarm_id=s_hint,
                conversation_id=c_hint,
                since_seq=since_seq,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def board_member_history(request: Request) -> Response:
        if (denied := _check_auth(request, bearer_token)) is not None:
            return denied
        qp = request.query_params
        member_name = str(qp.get("member") or "").strip()
        if not member_name:
            return JSONResponse({"error": "member is required"}, status_code=400)
        _, conv_id, _ = _extract_scope_hints(request)
        if not conv_id:
            return JSONResponse({"error": "conversationId is required", "messages": []}, status_code=404)
        sid = await asyncio.to_thread(manager.store.resolve_swarm_for_conversation, conv_id)
        if not sid:
            return JSONResponse({"error": "Swarm not found for conversation", "messages": []}, status_code=404)
        raw_after = qp.get("after_id")
        try:
            after_val = max(0, int(raw_after)) if raw_after is not None and str(raw_after).strip() != "" else 0
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid after_id"}, status_code=400)
        raw_sub = str(qp.get("subagent_session_id") or "").strip() or None
        raw_limit = qp.get("limit")
        try:
            limit_val = max(1, min(500, int(raw_limit) if raw_limit is not None and str(raw_limit).strip() != "" else 100))
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid limit"}, status_code=400)
        try:
            res = await asyncio.to_thread(
                runtime_mod.query_member_session_history,
                manager.store.state_dir,
                sid,
                member_name,
                after_id=after_val,
                limit=limit_val,
                subagent_session_id=raw_sub,
            )
            return JSONResponse(res)
        except Exception as exc:
            store.append_swarm_log(
                sid,
                "http",
                {"event": "member_history_error", "member": member_name, "error": str(exc)},
                state_dir=manager.store.state_dir,
            )
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def board_raw(request: Request) -> Response:
        if (denied := _check_auth(request, bearer_token)) is not None:
            return denied
        qp = request.query_params
        kind = str(qp.get("kind") or "archive").strip().lower()
        _, conv_id, _ = _extract_scope_hints(request)
        if not conv_id:
            return JSONResponse({"error": "conversationId is required"}, status_code=404)
        sid = await asyncio.to_thread(manager.store.resolve_swarm_for_conversation, conv_id)
        if not sid:
            return JSONResponse({"error": "Swarm not found for conversation"}, status_code=404)
        if kind == "archive":
            paths = manager.store.paths(sid)
            raw_limit = qp.get("limit")
            try:
                limit_val = max(1, min(2000, int(raw_limit) if raw_limit is not None and str(raw_limit).strip() != "" else 500))
            except (TypeError, ValueError):
                return JSONResponse({"error": "Invalid limit"}, status_code=400)
            raw_since = qp.get("since")
            raw_before = qp.get("before_seq")
            try:
                since_val = int(raw_since) if raw_since is not None and str(raw_since).strip() != "" else None
                before_val = int(raw_before) if raw_before is not None and str(raw_before).strip() != "" else None
            except (TypeError, ValueError):
                return JSONResponse({"error": "Invalid seq filter"}, status_code=400)
            archived = await asyncio.to_thread(board_mod.read_archived_messages, paths.board_file)
            filtered = archived
            if since_val is not None:
                filtered = [m for m in filtered if int(m.get("seq") or 0) > since_val]
            if before_val is not None:
                filtered = [m for m in filtered if int(m.get("seq") or 0) < before_val]
            selected = filtered[-limit_val:] if since_val is None else filtered[:limit_val]
            return JSONResponse({
                "swarm": sid,
                "kind": "archive",
                "archiveCount": len(archived),
                "count": len(selected),
                "messages": selected,
            })
        snap = await asyncio.to_thread(build_observer_snapshot, manager, sid)
        raw_map = snap.get("raw") if isinstance(snap.get("raw"), dict) else {}
        if kind not in raw_map:
            return JSONResponse({"error": f"Unknown raw kind: {kind!r}"}, status_code=400)
        return JSONResponse({"swarm": sid, "kind": kind, "content": raw_map.get(kind, "")})

    async def board_frame(request: Request) -> Response:
        if (denied := _check_auth(request, bearer_token)) is not None:
            return denied
        raw_path = str(request.query_params.get("path") or "").strip()
        if not raw_path or "\x00" in raw_path or "%" in raw_path:
            return JSONResponse({"error": "Invalid path"}, status_code=400)

        cleaned = raw_path.strip().strip("`\"'")
        raw_parts = cleaned.replace("\\", "/").split("/")
        if ".." in raw_parts:
            return JSONResponse({"error": "Forbidden path traversal"}, status_code=403)

        ext = Path(cleaned).suffix.lower()
        if ext not in _FRAME_CONTENT_TYPES:
            return JSONResponse({"error": f"Unsupported extension '{ext}'"}, status_code=403)

        # Resolve swarm strictly from conversationId (or explicit swarm for internal/CLI; never query `workspace`).
        s_hint, c_hint, _ = _extract_scope_hints(request)
        if c_hint:
            sid = await asyncio.to_thread(manager.store.resolve_swarm_for_conversation, c_hint)
            if not sid:
                return JSONResponse({"error": "Swarm not found for conversation"}, status_code=404)
        elif s_hint:
            sid = store.sanitize_swarm_id(s_hint)
            if not sid:
                return JSONResponse({"error": "Invalid swarm"}, status_code=400)
        else:
            return JSONResponse({"error": "conversationId or swarm is required"}, status_code=400)

        swarm_dir = manager.store.paths(sid).swarm_dir
        meta = await asyncio.to_thread(manager.store.read_meta, sid) or {}
        ws_root = str(meta.get("workspace") or "").strip()

        def _load_frame_sync() -> tuple[bytes | None, int, str]:
            cand_path = _resolve_frame_candidate_path(cleaned, swarm_dir=swarm_dir, ws_root=ws_root)
            if cand_path is None:
                return None, 400, "Invalid path"

            allowed_roots: list[Path] = [swarm_dir, *_allowed_app_data_brain_roots()]
            if ws_root:
                allowed_roots.append(Path(store.normalize_local_path(ws_root)))
            if not store.is_path_within(cand_path, allowed_roots):
                return None, 403, "Forbidden frame root"

            try:
                real_cand = Path(os.path.realpath(cand_path))
            except OSError:
                return None, 404, "Frame not found"

            authorized = _resolve_authorized_frame_paths(manager, sid)
            if os.path.normcase(str(real_cand)) not in authorized:
                return None, 403, "Frame not referenced on board"

            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            try:
                fd = os.open(str(real_cand), flags)
            except OSError:
                return None, 404, "Frame not found"
            try:
                st = os.fstat(fd)
                if st.st_size > _MAX_FRAME_BYTES:
                    return None, 413, "Frame exceeds 8 MB limit"
                with os.fdopen(fd, "rb") as fh:
                    fd = -1
                    data = fh.read(_MAX_FRAME_BYTES + 1)
                if len(data) > _MAX_FRAME_BYTES:
                    return None, 413, "Frame exceeds 8 MB limit"
                return data, 200, ""
            except OSError:
                return None, 404, "Unable to read frame"
            finally:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

        data, status_code, err_msg = await asyncio.to_thread(_load_frame_sync)
        if status_code != 200 or data is None:
            return JSONResponse({"error": err_msg}, status_code=status_code)

        return Response(
            content=data,
            media_type=_FRAME_CONTENT_TYPES[ext],
            headers={"Cache-Control": "private, max-age=3600"},
        )

    async def board_ui(request: Request) -> Response:
        if (denied := _check_auth(request, bearer_token)) is not None:
            return denied
        idx = _BOARD_UI_DIR / "index.html"
        if not await asyncio.to_thread(idx.is_file):
            return JSONResponse({"error": "Board UI not found"}, status_code=404)
        html_text = await asyncio.to_thread(idx.read_text, encoding="utf-8", errors="replace")
        return Response(
            content=html_text,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    async def board_preload_js(request: Request) -> Response:
        if (denied := _check_auth(request, bearer_token)) is not None:
            return denied
        shim = (
            "(function(){\n"
            "  var params = new URLSearchParams(location.search || '');\n"
            "  var conv = params.get('conversationId') || '';\n"
            "  var token = params.get('token') || '';\n"
            "  var origFetch = window.fetch.bind(window);\n"
            "  window.sidecar = window.sidecar || {\n"
            "    conversationId: conv,\n"
            "    fetch: function(url, init) {\n"
            "      var opts = Object.assign({}, init || {});\n"
            "      if (token) {\n"
            "        var hdrs = new Headers(opts.headers || {});\n"
            "        if (!hdrs.has('Authorization')) hdrs.set('Authorization', 'Bearer ' + token);\n"
            "        opts.headers = hdrs;\n"
            "      }\n"
            "      return origFetch(url, opts);\n"
            "    }\n"
            "  };\n"
            "})();\n"
        )
        return Response(
            content=shim,
            media_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    async def board_ui_asset(request: Request) -> Response:
        if (denied := _check_auth(request, bearer_token)) is not None:
            return denied
        rel = str(request.path_params.get("asset_name") or "").strip().lstrip("/")
        if not rel or "/" in rel or "\\" in rel or ".." in rel:
            return JSONResponse({"error": "Not found"}, status_code=404)
        target = _BOARD_UI_DIR / rel
        if not store.is_path_within(target, [_BOARD_UI_DIR]) or not await asyncio.to_thread(target.is_file):
            return JSONResponse({"error": "Not found"}, status_code=404)
        ctype = _ASSET_CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        raw_bytes = await asyncio.to_thread(target.read_bytes)
        return Response(content=raw_bytes, media_type=ctype, headers={"Cache-Control": "no-cache"})

    # --- Internal Loopback CLI Hook ---

    async def internal_board_post(request: Request) -> Response:
        if (denied := _check_loopback_internal_auth(request, manager)) is not None:
            return denied
        manager._ensure_host_loop(asyncio.get_running_loop())
        body, err_resp = await _read_json_body(request)
        if err_resp is not None:
            return err_resp
        assert body is not None

        summary = str(body.get("summary") or "").strip()
        if not summary:
            return JSONResponse({"error": "summary is required"}, status_code=400)

        raw_sid = body.get("swarmId") or body.get("swarm_id") or body.get("swarm")
        raw_conv = body.get("conversationId") or body.get("conversation_id")
        if raw_sid and str(raw_sid).strip():
            sid = store.sanitize_swarm_id(str(raw_sid)) or "default"
        elif raw_conv and str(raw_conv).strip():
            conv_str = str(raw_conv).strip()
            sid = (
                await asyncio.to_thread(manager.store.resolve_swarm_for_conversation, conv_str)
                or store.derive_swarm_id(conversation_id=conv_str)
            )
        else:
            sid = store.sanitize_swarm_id(os.environ.get("HERMES_A2A_SWARM_ID")) or "default"
        raw_kind = str(body.get("kind") or body.get("type") or models.MessageType.POST.value).strip().lower() or models.MessageType.POST.value
        raw_control = body.get("control") if isinstance(body.get("control"), dict) else None
        try:
            msg = await asyncio.to_thread(
                manager.board_service.post_message,
                sender=str(body.get("sender") or body.get("agent") or "worker"),
                summary=summary,
                msg_type=raw_kind,
                channel=str(body.get("channel") or "main"),
                to=body.get("to") if isinstance(body.get("to"), list) else None,
                details=str(body.get("details") or ""),
                files=body.get("files") if isinstance(body.get("files"), list) else None,
                reply_to=body.get("replyTo") or body.get("reply_to"),
                thread_id=body.get("threadId") or body.get("thread_id"),
                task_id=body.get("taskId") or body.get("task_id"),
                context_id=body.get("contextId") or body.get("context_id"),
                sender_role=body.get("senderRole") or body.get("sender_role"),
                control=raw_control,
                swarm_id=sid,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        if raw_kind and not msg.get("kind") and raw_kind != msg.get("type"):
            def _persist_raw_kind() -> None:
                with manager.board_service._locked_board(sid, mutate=True) as (_, brd, _):
                    for item in brd.get("messages") or []:
                        if isinstance(item, dict) and item.get("seq") == msg.get("seq"):
                            item["kind"] = raw_kind
                            break
            await asyncio.to_thread(_persist_raw_kind)
            msg["kind"] = raw_kind
        return JSONResponse(msg)

    async def _resolve_internal_swarm_id(body: dict[str, Any]) -> tuple[str, Path | None]:
        raw_sid = body.get("swarmId") or body.get("swarm_id") or body.get("swarm")
        raw_conv = body.get("conversationId") or body.get("conversation_id")
        raw_bdir = body.get("swarm_dir") or body.get("board_dir")
        bdir_path = Path(store.normalize_local_path(str(raw_bdir))) if raw_bdir and str(raw_bdir).strip() else None
        if bdir_path is not None and not store.is_path_within(bdir_path, [manager.store.state_dir]):
            raise PermissionError("board_dir must be within state_dir")
        if raw_sid and str(raw_sid).strip():
            sid = store.sanitize_swarm_id(str(raw_sid)) or "default"
        elif raw_conv and str(raw_conv).strip():
            conv_str = str(raw_conv).strip()
            sid = (
                await asyncio.to_thread(manager.store.resolve_swarm_for_conversation, conv_str)
                or store.derive_swarm_id(conversation_id=conv_str)
            )
        else:
            sid = store.sanitize_swarm_id(os.environ.get("HERMES_A2A_SWARM_ID")) or "default"
        return sid, bdir_path

    async def internal_board_claim(request: Request) -> Response:
        if (denied := _check_loopback_internal_auth(request, manager)) is not None:
            return denied
        manager._ensure_host_loop(asyncio.get_running_loop())
        body, err_resp = await _read_json_body(request)
        if err_resp is not None:
            return err_resp
        assert body is not None
        try:
            sid, bdir_path = await _resolve_internal_swarm_id(body)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        try:
            res = await asyncio.to_thread(
                manager.board_service.claim_scope,
                scope=str(body.get("scope") or ""),
                holder=str(body.get("agent") or body.get("holder") or "worker"),
                notes=str(body.get("notes") or ""),
                task_id=body.get("task_id") or body.get("taskId"),
                worktree_path=body.get("worktree_path") or body.get("worktreePath"),
                force=bool(body.get("force", False)),
                swarm_id=sid,
                board_dir=bdir_path,
            )
            claims = await asyncio.to_thread(manager.board_service.list_claims, swarm_id=sid, board_dir=bdir_path)
            return JSONResponse({"ok": True, "claim": res.get("claimed"), "claims": claims, **res})
        except board_mod.ScopeConflictError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)

    async def internal_board_release(request: Request) -> Response:
        if (denied := _check_loopback_internal_auth(request, manager)) is not None:
            return denied
        manager._ensure_host_loop(asyncio.get_running_loop())
        body, err_resp = await _read_json_body(request)
        if err_resp is not None:
            return err_resp
        assert body is not None
        try:
            sid, bdir_path = await _resolve_internal_swarm_id(body)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        res = await asyncio.to_thread(
            manager.board_service.release_scope,
            scope=body.get("scope"),
            holder=body.get("agent") or body.get("holder"),
            swarm_id=sid,
            board_dir=bdir_path,
        )
        claims = await asyncio.to_thread(manager.board_service.list_claims, swarm_id=sid, board_dir=bdir_path)
        return JSONResponse({"ok": True, "claims": claims, **res})

    async def internal_board_channel(request: Request) -> Response:
        if (denied := _check_loopback_internal_auth(request, manager)) is not None:
            return denied
        manager._ensure_host_loop(asyncio.get_running_loop())
        body, err_resp = await _read_json_body(request)
        if err_resp is not None:
            return err_resp
        assert body is not None
        try:
            sid, bdir_path = await _resolve_internal_swarm_id(body)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        try:
            res = await asyncio.to_thread(
                manager.board_service.channel_action,
                str(body.get("action") or body.get("op") or "list"),
                channel=body.get("channel"),
                topic=str(body.get("topic") or ""),
                description=str(body.get("description") or ""),
                members=body.get("members") if isinstance(body.get("members"), list) else None,
                actor=str(body.get("agent") or body.get("actor") or "coordinator"),
                swarm_id=sid,
                board_dir=bdir_path,
            )
            ctl = res.get("controlMessage") if isinstance(res.get("controlMessage"), dict) else None
            return JSONResponse({"ok": True, "control_seq": ctl.get("seq") if ctl else None, **res})
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)

    async def internal_board_read(request: Request) -> Response:
        if (denied := _check_loopback_internal_auth(request, manager)) is not None:
            return denied
        manager._ensure_host_loop(asyncio.get_running_loop())
        if request.method == "POST":
            body, err_resp = await _read_json_body(request)
            if err_resp is not None:
                return err_resp
            assert body is not None
        else:
            qp = request.query_params
            body = dict(qp)
            if qp.get("seqs"):
                try:
                    body["seqs"] = [int(x.strip()) for x in str(qp.get("seqs")).split(",") if x.strip()]
                except ValueError:
                    return JSONResponse({"error": "Invalid seqs"}, status_code=400)
        try:
            sid, bdir_path = await _resolve_internal_swarm_id(body)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        raw_status = body.get("status")
        is_status = raw_status is True or str(raw_status or "").strip().lower() in ("1", "true", "yes")
        if bdir_path is None:
            paths = manager.store.paths(sid)
            exists_on_disk = await asyncio.to_thread(
                lambda: paths.swarm_dir.is_dir() or paths.board_file.is_file() or paths.meta_file.is_file()
            )
            if not exists_on_disk:
                if is_status:
                    return JSONResponse({
                        "ok": True,
                        "swarmId": sid,
                        "head": 0,
                        "members": {},
                        "channels": {},
                        "claims": {},
                    })
                return JSONResponse({
                    "ok": True,
                    "swarmId": sid,
                    "head": 0,
                    "count": 0,
                    "messages": [],
                    "members": [],
                })
        raw_since = body.get("since")
        try:
            since_val = int(raw_since) if raw_since is not None and str(raw_since).strip() != "" else None
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid since"}, status_code=400)
        raw_before = body.get("before_seq") if "before_seq" in body else body.get("beforeSeq")
        try:
            before_val = int(raw_before) if raw_before is not None and str(raw_before).strip() != "" else None
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid before_seq"}, status_code=400)
        raw_inc_arch = body.get("include_archive") if "include_archive" in body else body.get("includeArchive")
        is_inc_arch = raw_inc_arch is True or str(raw_inc_arch or "").strip().lower() in ("1", "true", "yes")
        raw_full = body.get("full")
        is_full = raw_full is True or str(raw_full or "").strip().lower() in ("1", "true", "yes")
        max_lim = 2000 if is_full else 500
        raw_limit = body.get("limit")
        try:
            limit_val = max(1, min(max_lim, int(raw_limit) if raw_limit is not None and str(raw_limit).strip() != "" else 100))
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid limit"}, status_code=400)
        raw_seqs = body.get("seqs")
        try:
            seqs_val = [int(x) for x in raw_seqs] if isinstance(raw_seqs, list) else None
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid seqs"}, status_code=400)
        raw_search = body.get("search")
        search_val = str(raw_search) if raw_search is not None and str(raw_search).strip() else None
        res = await asyncio.to_thread(
            manager.board_service.read_messages,
            swarm_id=sid,
            since=since_val,
            seqs=seqs_val,
            status=is_status,
            agent=body.get("agent"),
            channel=body.get("channel"),
            mention=body.get("mention"),
            sender=body.get("sender"),
            msg_type=body.get("kind") or body.get("type"),
            limit=limit_val,
            mark_read_by=body.get("mark_read_by"),
            include_archive=is_inc_arch,
            before_seq=before_val,
            search=search_val,
            full=is_full,
            board_dir=bdir_path,
        )
        return JSONResponse({"ok": True, **res})

    async def internal_wake_swarms(request: Request) -> Response:
        if (denied := _check_loopback_wake_auth(request, bearer_token, manager)) is not None:
            return denied
        manager._ensure_host_loop(asyncio.get_running_loop())
        swarms = await asyncio.to_thread(manager.list_active_wake_swarms)
        return JSONResponse({"swarms": swarms})

    async def internal_wake_events(request: Request) -> Response:
        if (denied := _check_loopback_wake_auth(request, bearer_token, manager)) is not None:
            return denied
        with _SSE_SUBSCRIBERS_LOCK:
            if len(_ACTIVE_SSE_QUEUES) >= _MAX_SSE_SUBSCRIBERS:
                return JSONResponse({"error": "Too many active SSE subscribers"}, status_code=503)
        manager._ensure_host_loop(asyncio.get_running_loop())
        since_map: dict[str, Any] = {}
        if request.method == "POST":
            body, err_resp = await _read_json_body(request)
            if err_resp is not None:
                return err_resp
            assert body is not None
            raw_sm = body.get("since_map") if body.get("since_map") is not None else body.get("sinceMap")
            if raw_sm is not None:
                if not isinstance(raw_sm, dict):
                    return JSONResponse({"error": "since_map must be a JSON object"}, status_code=400)
                since_map = raw_sm
            if body.get("active_only"):
                active_ids = set(await asyncio.to_thread(manager._active_swarm_ids))
                since_map = {k: v for k, v in since_map.items() if store.sanitize_swarm_id(str(k)) in active_ids}
        else:
            raw_since_map = (
                request.query_params.get("since_map")
                or request.query_params.get("sinceMap")
                or ""
            ).strip()
            if raw_since_map:
                try:
                    parsed = json.loads(raw_since_map)
                    if not isinstance(parsed, dict):
                        return JSONResponse({"error": "since_map must be a JSON object"}, status_code=400)
                    since_map = parsed
                except Exception:
                    return JSONResponse({"error": "Invalid since_map JSON"}, status_code=400)
        return StreamingResponse(
            _sse_internal_wake_events_stream(manager, since_map=since_map),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
            Route("/.well-known/agent.json", agent_card, methods=["GET"]),
            Route("/", rpc_endpoint, methods=["POST"]),
            Route("/v1/message:send", rest_send_message, methods=["POST"]),
            Route("/v1/tasks", rest_list_tasks, methods=["GET"]),
            Route("/v1/tasks/{task_id}:cancel", rest_cancel_task, methods=["POST"]),
            Route("/v1/tasks/{task_id}:subscribe", rest_subscribe_task, methods=["GET"]),
            Route("/v1/tasks/{task_id}", rest_get_task, methods=["GET"]),
            Route("/board", board_get, methods=["GET"]),
            Route("/board/events", board_events, methods=["GET"]),
            Route("/board/frame", board_frame, methods=["GET"]),
            Route("/board/raw", board_raw, methods=["GET"]),
            Route("/board/member/history", board_member_history, methods=["GET"]),
            Route("/board/preload.js", board_preload_js, methods=["GET"]),
            Route("/board/api/swarm", board_get, methods=["GET"]),
            Route("/board/api/frame", board_frame, methods=["GET"]),
            Route("/board/api/raw", board_raw, methods=["GET"]),
            Route("/board/api/member/history", board_member_history, methods=["GET"]),
            Route("/board/api/events", board_events, methods=["GET"]),
            Route("/board/ui", board_ui, methods=["GET"]),
            Route("/board/{asset_name}", board_ui_asset, methods=["GET"]),
            Route("/internal/board/post", internal_board_post, methods=["POST"]),
            Route("/internal/board/claim", internal_board_claim, methods=["POST"]),
            Route("/internal/board/release", internal_board_release, methods=["POST"]),
            Route("/internal/board/channel", internal_board_channel, methods=["POST"]),
            Route("/internal/board/read", internal_board_read, methods=["GET", "POST"]),
            Route("/internal/wake/swarms", internal_wake_swarms, methods=["GET"]),
            Route("/internal/wake/events", internal_wake_events, methods=["GET", "POST"]),
        ],
    )


def _bind_available_socket(host: str, port: int) -> tuple[socket.socket, int]:
    """Bind `host:port` exclusively and verify loopback handshake so Windows blackholed ports are skipped."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    bind_host = "127.0.0.1" if host == "localhost" else host
    if port == 0:
        candidates = [
            *random.sample(range(52000, 53000), 12),
            *random.sample(range(18000, 20000), 12),
            *random.sample(range(9920, 10999), 12),
            0,
        ]
    else:
        candidates = [port, *[p for p in range(max(port + 1, 9911), 10000)], 0]

    last_err: Exception | None = None
    for cand in candidates:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            elif os.name != "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind_host, cand))
            sock.listen(2048)
            bound_port = int(sock.getsockname()[1])
            if bind_host in ("127.0.0.1", "::1"):
                probe = socket.socket(family, socket.SOCK_STREAM)
                conn = None
                try:
                    probe.settimeout(0.25)
                    probe.connect((bind_host, bound_port))
                    sock.settimeout(0.25)
                    conn, _ = sock.accept()
                finally:
                    sock.settimeout(None)
                    if conn is not None:
                        try:
                            conn.close()
                        except OSError:
                            pass
                    try:
                        probe.close()
                    except OSError:
                        pass
            sock.setblocking(False)
            return sock, bound_port
        except OSError as exc:
            last_err = exc
            try:
                sock.close()
            except OSError:
                pass
    raise RuntimeError(f"Failed to bind A2A HTTP server socket on {host}: {last_err}")


class EmbeddedA2AHttpController:
    """Runs the A2A + Observer HTTP server in a background thread sharing one SwarmManager."""

    def __init__(self, manager: runtime_mod.SwarmManager) -> None:
        self._manager = manager
        self._thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None
        self._sock: socket.socket | None = None
        self.url: str | None = None
        self._atexit_registered = False

    def is_running(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._server is not None
            and not self._server.should_exit
        )

    def start(self, host: str = "127.0.0.1", port: int = 9910) -> str:
        if self.is_running() and self.url:
            os.environ["HERMES_A2A_HTTP_URL"] = self.url
            self._manager.set_http_url(self.url)
            self._register_self()
            return self.url

        token = os.environ.get("HERMES_A2A_HTTP_TOKEN") or None
        if host not in ("127.0.0.1", "localhost", "::1") and not token:
            raise ValueError("Refusing non-loopback bind without HERMES_A2A_HTTP_TOKEN set.")

        self._manager.get_internal_token()
        sock, bound_port = _bind_available_socket(host, int(port))
        client_host = "127.0.0.1" if host in ("0.0.0.0", "") else ("::1" if host in ("::", "[::]") else host)
        host_fmt = f"[{client_host}]" if ":" in client_host and not client_host.startswith("[") else client_host
        self.url = f"http://{host_fmt}:{bound_port}"
        os.environ["HERMES_A2A_HTTP_URL"] = self.url
        self._manager.set_http_url(self.url)
        self._sock = sock

        app = create_a2a_app(self._manager, http_url=self.url, bearer_token=token)
        config = uvicorn.Config(app, host=host, port=bound_port, log_level="warning")
        self._server = uvicorn.Server(config)

        def _run() -> None:
            assert self._server is not None
            try:
                asyncio.run(self._server.serve(sockets=[sock]))
            finally:
                try:
                    sock.close()
                except OSError:
                    pass

        self._thread = threading.Thread(target=_run, name="hermes-a2a-http", daemon=True)
        self._thread.start()
        self._register_self()
        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True
        return self.url

    def _register_self(self, swarm_id: str | None = None) -> None:
        if not self.url:
            return
        sid = store.sanitize_swarm_id(swarm_id or os.environ.get("HERMES_A2A_SWARM_ID")) or "default"
        meta = self._manager.store.read_meta(sid) or {}
        ws = self._manager.get_default_workdir(sid) or meta.get("workspace")
        try:
            store.register_server_instance(
                http_url=self.url,
                swarm_id=sid,
                workspace=ws,
                conversation_id=meta.get("primaryConversationId") or meta.get("coordinatorConversationId"),
                state_dir=self._manager._state_dir,
            )
        except Exception:
            pass

    def stop(self) -> bool:
        old_url = self.url
        had_server = self._server is not None
        with _SSE_SUBSCRIBERS_LOCK:
            qs = list(_ACTIVE_SSE_QUEUES)
        for q in qs:
            try:
                q.put_nowait(None)
            except Exception:
                pass
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._server = None
        self._thread = None
        self.url = None
        self._manager.set_http_url(None)
        if old_url:
            try:
                store.unregister_server_instance(http_url=old_url, state_dir=self._manager._state_dir)
            except Exception:
                pass
        return had_server
