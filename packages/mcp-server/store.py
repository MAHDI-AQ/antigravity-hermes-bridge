"""1:1 session-bound Swarm v2 filesystem store, atomic locking, session isolation, and server PID registry."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import sys
import threading
import time
from typing import Any, Iterator
import uuid
import weakref

import models

_BASE_DIR = Path(__file__).resolve().parent
_DRIVE_PATH_RE = re.compile(r"^([A-Za-z]):[\\/]?(.*)$")
_SWARM_ID_CLEAN_RE = re.compile(r"[^a-z0-9_-]+")
_CONV_RESOLVE_CACHE: dict[tuple[str, str], tuple[float, str | None]] = {}
_CONV_RESOLVE_CACHE_LOCK = threading.Lock()
_CONV_RESOLVE_TTL_S = 1.5


class _LockBox:
    """Weak-referenceable wrapper around a threading lock."""

    __slots__ = ("lock", "__weakref__")

    def __init__(self, *, reentrant: bool = False) -> None:
        self.lock: threading.Lock | threading.RLock = threading.RLock() if reentrant else threading.Lock()


_THREAD_LOCKS: weakref.WeakValueDictionary[str, _LockBox] = weakref.WeakValueDictionary()
_SWARM_RLOCKS: weakref.WeakValueDictionary[str, _LockBox] = weakref.WeakValueDictionary()
_LOCKS_GUARD = threading.Lock()


def _acquire_lock_box(path: Path) -> _LockBox:
    key = os.path.normcase(os.path.abspath(str(path)))
    with _LOCKS_GUARD:
        box = _THREAD_LOCKS.get(key)
        if box is None:
            box = _LockBox(reentrant=False)
            _THREAD_LOCKS[key] = box
        return box


def get_swarm_thread_lock(swarm_id: str) -> threading.RLock:
    """Return a bounded weak-referenced RLock scoped to a single swarm."""
    sid = sanitize_swarm_id(swarm_id) or "default"
    with _LOCKS_GUARD:
        box = _SWARM_RLOCKS.get(sid)
        if box is None:
            box = _LockBox(reentrant=True)
            _SWARM_RLOCKS[sid] = box
        return box.lock  # type: ignore[return-value]


normalize_local_path = models.normalize_local_path
to_wsl_path = models.to_wsl_path


def _norm_compare_key(raw: str | Path | None) -> str:
    if not raw:
        return ""
    norm = normalize_local_path(str(raw))
    if not norm:
        return ""
    if sys.platform == "win32" or _DRIVE_PATH_RE.match(norm):
        return norm.replace("\\", "/").rstrip("/").lower()
    return norm.rstrip("/") or "/"


def paths_equal(a: str | Path | None, b: str | Path | None) -> bool:
    """Compare two local paths with platform-appropriate normalization and case handling."""
    ka = _norm_compare_key(a)
    kb = _norm_compare_key(b)
    return bool(ka and kb and ka == kb)


def is_path_within(candidate: str | Path, allowed_roots: list[str | Path]) -> bool:
    """Return True if `candidate` resolves inside one of `allowed_roots` (symlink-safe)."""
    if not candidate or not allowed_roots:
        return False
    try:
        cand_real = os.path.realpath(normalize_local_path(str(candidate)))
    except OSError:
        return False
    cand_norm = os.path.normcase(cand_real)
    for root in allowed_roots:
        if not root:
            continue
        try:
            root_real = os.path.realpath(normalize_local_path(str(root)))
        except OSError:
            continue
        root_norm = os.path.normcase(root_real)
        try:
            if os.path.commonpath([cand_norm, root_norm]) == root_norm:
                return True
        except ValueError:
            continue
    return False


def get_state_dir() -> Path:
    """Resolve the dynamic root state directory (honors `HERMES_A2A_STATE_DIR` at call time)."""
    raw = os.environ.get("HERMES_A2A_STATE_DIR", "").strip()
    if raw:
        return Path(normalize_local_path(raw))
    return _BASE_DIR / "state"


def get_swarms_dir() -> Path:
    return get_state_dir() / "swarms"


def get_registry_file() -> Path:
    return get_state_dir() / "registry.json"


def default_hermes_home() -> Path:
    """Resolve the active Hermes home directory dynamically from env/platform conventions."""
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        return Path(normalize_local_path(env_home))
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "").strip()
        local_base = Path(local_app) if local_app else (Path.home() / "AppData" / "Local")
        win_candidate = local_base / "hermes"
        home_candidate = Path.home() / ".hermes"
        if win_candidate.exists():
            return win_candidate
        if home_candidate.exists():
            return home_candidate
        return win_candidate
    return Path.home() / ".hermes"


def default_hermes_exe() -> Path:
    """Resolve the Hermes CLI executable dynamically without hardcoded user paths."""
    env_exe = os.environ.get("HERMES_EXE", "").strip()
    if env_exe:
        return Path(normalize_local_path(env_exe))
    bin_name = "hermes.exe" if sys.platform == "win32" else "hermes"
    home_bin = default_hermes_home() / "bin" / bin_name
    if home_bin.is_file():
        return home_bin
    which_exe = shutil.which("hermes")
    if which_exe:
        return Path(which_exe)
    return home_bin


if sys.platform == "win32":
    import msvcrt

    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _K32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _K32.OpenProcess.restype = wintypes.HANDLE
    _K32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _K32.GetExitCodeProcess.restype = wintypes.BOOL
    _K32.CloseHandle.argtypes = [wintypes.HANDLE]
    _K32.CloseHandle.restype = wintypes.BOOL
    _K32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _K32.CreateFileW.restype = wintypes.HANDLE
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
else:
    _K32 = None
    _INVALID_HANDLE_VALUE = -1


def _read_text_shared(path: Path) -> str:
    """Read UTF-8 text from `path`, using Windows shared-delete/write/read flags when available."""
    if sys.platform == "win32" and _K32 is not None:
        generic_read = 0x80000000
        share_all = 0x00000001 | 0x00000002 | 0x00000004  # FILE_SHARE_READ | WRITE | DELETE
        open_existing = 3
        file_attr_normal = 0x80
        handle = _K32.CreateFileW(
            str(path),
            generic_read,
            share_all,
            None,
            open_existing,
            file_attr_normal,
            None,
        )
        if not handle or handle == _INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            if err in (2, 3):  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
                raise FileNotFoundError(str(path))
            if err == 5:  # ERROR_ACCESS_DENIED
                raise PermissionError(f"Access denied reading {path}")
            raise ctypes.WinError(err)
        fd = -1
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except Exception:
            _K32.CloseHandle(handle)
            raise
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def pid_alive(pid: int) -> bool:
    """Return True if a process with `pid` is currently running."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32" and _K32 is not None:
        process_query_limited_info = 0x1000
        still_active = 259
        error_access_denied = 5
        handle = _K32.OpenProcess(process_query_limited_info, False, int(pid))
        if not handle or handle == _INVALID_HANDLE_VALUE:
            return ctypes.get_last_error() == error_access_denied
        try:
            code = wintypes.DWORD()
            ok = _K32.GetExitCodeProcess(handle, ctypes.byref(code))
            if not ok:
                return False
            return code.value == still_active
        finally:
            _K32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _read_lock_holder(lock_path: Path) -> tuple[int | None, float | None, bool, str | None]:
    try:
        raw = _read_text_shared(lock_path).strip()
        if raw.startswith("{"):
            data = json.loads(raw)
            if isinstance(data, dict):
                pid_raw = data.get("pid")
                ts_raw = data.get("ts")
                released = bool(data.get("released", False))
                token_raw = data.get("token")
                pid_val = int(pid_raw) if pid_raw is not None else None
                ts_val = float(ts_raw) if ts_raw is not None else None
                token_val = str(token_raw) if token_raw is not None else None
                return pid_val, ts_val, released, token_val
    except Exception:
        pass
    try:
        return None, lock_path.stat().st_mtime, False, None
    except OSError:
        return None, None, False, None


def _unlink_with_retries(path: Path, attempts: int = 20) -> bool:
    for i in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return True
        except PermissionError:
            time.sleep(min(0.05, 0.005 * (i + 1)))
        except OSError:
            return False
    return not path.exists()


def _reap_lock_file(lock_path: Path, expected_token: str | None) -> bool:
    """Re-verify holder token and rename dead/stale/released lock aside before unlinking."""
    _, _, _, cur_token = _read_lock_holder(lock_path)
    if expected_token is not None and cur_token != expected_token:
        return False
    reap_path = lock_path.with_name(f"{lock_path.name}.reap.{uuid.uuid4().hex[:8]}")
    try:
        os.replace(str(lock_path), str(reap_path))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    _unlink_with_retries(reap_path, attempts=4)
    return True


@contextmanager
def atomic_file_lock(
    lock_path: Path | str,
    timeout: float = 5.0,
    stale_seconds: float = 30.0,
) -> Iterator[None]:
    """Acquire an exclusive thread + O_CREAT|O_EXCL file lock with Windows unlink resilience."""
    lpath = Path(lock_path)
    lpath.parent.mkdir(parents=True, exist_ok=True)
    wait_timeout = max(0.05, float(timeout))
    deadline = time.monotonic() + wait_timeout
    box = _acquire_lock_box(lpath)
    if not box.lock.acquire(timeout=wait_timeout):
        raise TimeoutError(f"Timed out after {timeout:.2f}s acquiring lock {lpath}")

    acquired = False
    lock_token = uuid.uuid4().hex
    attempt = 0
    try:
        while True:
            try:
                fd = os.open(str(lpath), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                try:
                    payload = json.dumps({
                        "pid": os.getpid(),
                        "ts": time.time(),
                        "token": lock_token,
                        "released": False,
                    }).encode("utf-8")
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                acquired = True
                break
            except FileExistsError:
                now_mono = time.monotonic()
                pid_val, ts_val, released_flag, cur_token = _read_lock_holder(lpath)
                if ts_val is None:
                    try:
                        ts_val = lpath.stat().st_mtime
                    except OSError:
                        ts_val = None

                is_released = released_flag or pid_val == 0
                is_dead_pid = pid_val is not None and pid_val > 0 and not pid_alive(pid_val)
                is_stale = ts_val is not None and (time.time() - ts_val) > stale_seconds
                if is_released or is_dead_pid or (is_stale and (pid_val is None or not pid_alive(pid_val))):
                    if _reap_lock_file(lpath, cur_token):
                        attempt = 0
                        continue

                if now_mono >= deadline:
                    raise TimeoutError(f"Timed out after {timeout:.2f}s acquiring lock {lpath}")
                remaining = deadline - now_mono
                base_sleep = min(0.05, 0.003 * (1.4 ** min(attempt, 10)))
                jittered = base_sleep * random.uniform(0.5, 1.5)
                attempt += 1
                time.sleep(min(remaining, max(0.002, jittered)))

        yield
    finally:
        try:
            if acquired:
                _, _, _, on_disk_token = _read_lock_holder(lpath)
                if on_disk_token == lock_token:
                    removed = _unlink_with_retries(lpath, attempts=20)
                    if not removed and lpath.exists():
                        _, _, _, still_token = _read_lock_holder(lpath)
                        if still_token == lock_token:
                            try:
                                lpath.write_text(
                                    json.dumps({"pid": 0, "ts": 0.0, "token": lock_token, "released": True}),
                                    encoding="utf-8",
                                )
                            except OSError:
                                pass
        finally:
            box.lock.release()


def _sweep_stale_temp_files(target: Path, max_age_seconds: float = 60.0) -> None:
    """Best-effort cleanup of stale `*.tmp.*` and `*.reap.*` files older than `max_age_seconds`."""
    now = time.time()
    prefixes = (f"{target.name}.tmp.", f"{target.name}.reap.")
    try:
        for entry in target.parent.iterdir():
            name = entry.name
            if not any(name.startswith(p) for p in prefixes):
                continue
            try:
                st = entry.stat()
                if (now - st.st_mtime) > max_age_seconds:
                    entry.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def atomic_write_json(path: Path | str, data: Any) -> None:
    """Atomically write JSON via unique temp file and `os.replace` with transient Windows retries."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_temp_files(target)
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    encoded = json.dumps(data, indent=2, ensure_ascii=False)
    tmp.write_text(encoded, encoding="utf-8")
    last_exc: Exception | None = None
    try:
        for attempt in range(16):
            try:
                os.replace(str(tmp), str(target))
                return
            except (PermissionError, OSError) as exc:
                last_exc = exc
                delay = min(0.25, 0.008 * (1.4 ** attempt)) * random.uniform(0.7, 1.3)
                time.sleep(delay)
        if last_exc is not None:
            raise last_exc
    finally:
        if tmp.exists():
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def read_json_safe(
    path: Path | str,
    default: Any = None,
    *,
    raise_on_error: bool = False,
) -> Any:
    """Read JSON from disk safely with shared-read flags and brief retry across concurrent replace."""
    target = Path(path)
    if not target.is_file():
        return default
    last_exc: Exception | None = None
    for attempt in range(8):
        try:
            raw = _read_text_shared(target)
            return json.loads(raw)
        except FileNotFoundError:
            return default
        except (OSError, ValueError) as exc:
            last_exc = exc
            if attempt < 7:
                time.sleep(min(0.05, 0.008 * (attempt + 1)) * random.uniform(0.7, 1.3))
    if raise_on_error and last_exc is not None:
        raise last_exc
    return default


def sanitize_swarm_id(raw: str | None) -> str:
    """Sanitize a swarm identifier to lowercase `[a-z0-9_-]`."""
    if not raw:
        return ""
    cleaned = _SWARM_ID_CLEAN_RE.sub("-", str(raw).strip().lower()).strip("-")
    return cleaned[:64].strip("-")


def _workspace_hash_key(norm_ws: str) -> str:
    return _norm_compare_key(norm_ws)


def derive_swarm_id(
    swarm_id: str | None = None,
    conversation_id: str | None = None,
    workspace: str | None = None,
    workspace_path: str | None = None,
) -> str:
    """Derive canonical 1:1 swarm_id: conversation_id > explicit swarm_id > ws-<slug>-<sha1[:8]> > 'default'."""
    conv_slug = sanitize_swarm_id(conversation_id)
    if conv_slug:
        return conv_slug

    explicit = sanitize_swarm_id(swarm_id)
    if explicit:
        return explicit

    raw_ws = workspace if workspace is not None else workspace_path
    if raw_ws and str(raw_ws).strip():
        norm = normalize_local_path(raw_ws)
        if norm:
            hkey = _workspace_hash_key(norm)
            leaf = sanitize_swarm_id(Path(norm).name)[:24] or "workspace"
            digest = hashlib.sha1(hkey.encode("utf-8")).hexdigest()[:8]
            return f"ws-{leaf}-{digest}"

    return "default"


@dataclass(frozen=True)
class SwarmPaths:
    """Scoped filesystem paths for a single swarm under `<state_dir>/swarms/<swarm_id>/`."""

    swarm_id: str
    swarm_dir: Path
    meta_file: Path
    meta_lock: Path
    tasks_file: Path
    tasks_lock: Path
    archive_file: Path
    archive_lock: Path
    board_file: Path
    board_lock: Path
    frames_dir: Path
    logs_dir: Path


_MAX_SWARM_LOG_BYTES = 5 * 1024 * 1024
_LOG_STREAM_CLEAN_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def resolve_swarm_paths(
    swarm_id: str = "default",
    *,
    state_dir: Path | None = None,
    create: bool = False,
) -> SwarmPaths:
    """Return scoped filesystem paths for `swarm_id` (never creates directories when `create=False`)."""
    sid = sanitize_swarm_id(swarm_id) or "default"
    base = (state_dir / "swarms") if state_dir is not None else get_swarms_dir()
    sdir = base / sid
    if create:
        sdir.mkdir(parents=True, exist_ok=True)
    return SwarmPaths(
        swarm_id=sid,
        swarm_dir=sdir,
        meta_file=sdir / "meta.json",
        meta_lock=sdir / "meta.lock",
        tasks_file=sdir / "tasks.json",
        tasks_lock=sdir / "tasks.lock",
        archive_file=sdir / "tasks_archive.json",
        archive_lock=sdir / "tasks_archive.lock",
        board_file=sdir / "board.json",
        board_lock=sdir / "board.lock",
        frames_dir=sdir / "frames",
        logs_dir=sdir / "logs",
    )


def append_swarm_log(
    swarm_id: str,
    stream: str,
    event: Any,
    *,
    state_dir: Path | None = None,
    max_bytes: int = _MAX_SWARM_LOG_BYTES,
) -> Path | None:
    """Append a structured JSONL event to `<swarm_dir>/logs/<stream>.jsonl` with ~5 MiB rotation."""
    paths = resolve_swarm_paths(swarm_id, state_dir=state_dir, create=False)
    safe_stream = _LOG_STREAM_CLEAN_RE.sub("_", str(stream or "events").strip()).strip("._-") or "events"
    log_file = paths.logs_dir / f"{safe_stream}.jsonl"
    if isinstance(event, dict):
        record = dict(event)
        record.setdefault("ts", models.now_iso())
        record.setdefault("swarmId", paths.swarm_id)
    else:
        record = {"ts": models.now_iso(), "swarmId": paths.swarm_id, "event": event}
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    limit = max(1024, int(max_bytes))
    try:
        with get_swarm_thread_lock(paths.swarm_id):
            paths.logs_dir.mkdir(parents=True, exist_ok=True)
            try:
                if log_file.is_file() and log_file.stat().st_size >= limit:
                    rotated = log_file.with_name(f"{safe_stream}.jsonl.1")
                    os.replace(log_file, rotated)
            except OSError:
                pass
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(line)
        return log_file
    except OSError:
        return None


def migrate_meta_v1_to_v2(raw: dict[str, Any] | None, swarm_id: str) -> dict[str, Any]:
    """Normalize a v1 or partial `meta.json` record into the canonical v2 schema without mutating disk."""
    base = models.default_swarm_meta(swarm_id)
    if not isinstance(raw, dict):
        return base

    ws_raw = raw.get("workspace") or raw.get("workspacePath")
    norm_ws = normalize_local_path(str(ws_raw)) if ws_raw else None
    primary_cid = raw.get("primaryConversationId") or raw.get("conversationId")
    coord_cid = raw.get("coordinatorConversationId")

    conv_ids: list[str] = []
    for item in [*(raw.get("conversationIds") or []), primary_cid, coord_cid]:
        clean = str(item or "").strip()
        if clean and clean not in conv_ids:
            conv_ids.append(clean)

    title = str(raw.get("title") or "").strip()
    if not title:
        title = Path(norm_ws).name if norm_ws else ("Default Swarm" if swarm_id == "default" else swarm_id)

    return {
        "schemaVersion": models.SCHEMA_VERSION,
        "swarmId": swarm_id,
        "title": title,
        "goal": str(raw.get("goal") or ""),
        "mission": str(raw.get("mission") or ""),
        "workspace": norm_ws,
        "primaryConversationId": str(primary_cid).strip() if primary_cid else None,
        "coordinatorConversationId": str(coord_cid).strip() if coord_cid else None,
        "conversationIds": conv_ids,
        "projectId": str(raw.get("projectId")).strip() if raw.get("projectId") else None,
        "createdAt": str(raw.get("createdAt") or base["createdAt"]),
        "updatedAt": str(raw.get("updatedAt") or base["updatedAt"]),
        "archived": bool(raw.get("archived", False)),
        "wakeSeq": max(0, int(raw.get("wakeSeq") or 0)),
    }


def migrate_board_v1_to_v2(raw: dict[str, Any] | None, swarm_id: str) -> dict[str, Any]:
    """Normalize a v1 or partial `board.json` payload into the canonical v2 schema in memory."""
    base = models.default_board_state(swarm_id)
    if not isinstance(raw, dict):
        return base

    base["title"] = str(raw.get("title") or base["title"])
    base["goal"] = str(raw.get("goal") or "")
    base["mission"] = str(raw.get("mission") or "")

    if isinstance(raw.get("channels"), dict):
        for ch_key, ch_val in raw["channels"].items():
            norm_ch = models.normalize_channel_name(str(ch_key))
            if isinstance(ch_val, dict):
                rec = models.new_channel_record(
                    norm_ch,
                    description=str(ch_val.get("description") or ""),
                    topic=str(ch_val.get("topic") or ch_val.get("description") or ""),
                    subscribers=[str(s) for s in (ch_val.get("subscribers") or []) if str(s).strip()],
                    archived=bool(ch_val.get("archived", False)),
                )
                if ch_val.get("createdAt"):
                    rec["createdAt"] = str(ch_val["createdAt"])
                if ch_val.get("updatedAt"):
                    rec["updatedAt"] = str(ch_val["updatedAt"])
                base["channels"][norm_ch] = rec

    if isinstance(raw.get("members"), dict):
        for mname, mval in raw["members"].items():
            if not isinstance(mval, dict):
                continue
            name = str(mval.get("name") or mname).strip()
            if not name:
                continue
            raw_role = str(mval.get("role") or (models.Role.COORDINATOR.value if name == "coordinator" else models.Role.WORKER.value))
            role = models.normalize_role(raw_role, default=models.Role.WORKER.value)
            tier = int(mval.get("tier") or (3 if role == models.Role.SUBAGENT.value else 2))
            raw_chs = mval.get("channels")
            if raw_chs is None:
                ch_list = None
            else:
                ch_list = ["*" if str(c).strip() == "*" else models.normalize_channel_name(str(c)) for c in raw_chs]
            member = models.new_board_member(
                name,
                role=role,
                tier=tier,
                runtime=str(mval.get("runtime") or ("antigravity" if role == models.Role.COORDINATOR.value else "hermes")),
                state=str(mval.get("state") or models.MemberState.WAITING.value),
                assignment=str(mval.get("assignment") or ""),
                channels=ch_list,
                parent=mval.get("parent"),
                parent_task_id=mval.get("parentTaskId"),
                task_id=mval.get("taskId"),
                context_id=mval.get("contextId"),
                session_id=mval.get("sessionId"),
                conversation_id=mval.get("conversationId"),
                model=mval.get("model"),
                provider=mval.get("provider"),
                workdir=mval.get("workdir"),
                worktree_path=mval.get("worktreePath"),
                worktree_branch=mval.get("worktreeBranch"),
                wait_since=mval.get("waitSince"),
                last_read_seq=int(mval.get("lastReadSeq") or 0),
                last_post_seq=int(mval.get("lastPostSeq") or 0),
                killed=bool(mval.get("killed", False)),
                subagents=list(mval.get("subagents") or []) if isinstance(mval.get("subagents"), list) else [],
            )
            if mval.get("lastHeartbeatAt"):
                member["lastHeartbeatAt"] = str(mval["lastHeartbeatAt"])
            if mval.get("updatedAt"):
                member["updatedAt"] = str(mval["updatedAt"])
            base["members"][name] = member

    if isinstance(raw.get("subagents"), dict):
        base["subagents"] = dict(raw["subagents"])

    if isinstance(raw.get("claims"), dict):
        clean_claims: dict[str, Any] = {}
        for scope_key, cval in raw["claims"].items():
            if not isinstance(cval, dict):
                continue
            holder = str(cval.get("holder") or cval.get("member") or cval.get("taskId") or "unknown")
            rec = models.new_scope_claim(
                str(cval.get("scope") or scope_key),
                holder=holder,
                task_id=cval.get("taskId"),
                worktree_path=cval.get("worktreePath"),
                notes=str(cval.get("notes") or ""),
                ttl_seconds=cval.get("ttlSeconds") if "ttlSeconds" in cval else 3600.0,
                expires_at=cval.get("expiresAt"),
            )
            if cval.get("claimedAt"):
                rec["claimedAt"] = str(cval["claimedAt"])
            clean_claims[str(scope_key)] = rec
        base["claims"] = clean_claims

    if isinstance(raw.get("plan"), dict):
        raw_plan = raw["plan"]
        base["plan"] = {
            "summary": str(raw_plan.get("summary") or ""),
            "threads": [dict(t) for t in (raw_plan.get("threads") or []) if isinstance(t, dict)],
            "tasks": [dict(t) for t in (raw_plan.get("tasks") or []) if isinstance(t, dict)],
            "updatedAt": str(raw_plan.get("updatedAt") or base["plan"]["updatedAt"]),
        }

    if isinstance(raw.get("pendingMentions"), dict):
        base["pendingMentions"] = {
            str(k): [dict(item) for item in v if isinstance(item, dict)]
            for k, v in raw["pendingMentions"].items()
            if isinstance(v, list)
        }

    if isinstance(raw.get("frames"), list):
        base["frames"] = [dict(f) for f in raw["frames"] if isinstance(f, dict)]

    raw_msgs = raw.get("messages")
    messages_out: list[dict[str, Any]] = []
    max_seq = 0
    if isinstance(raw_msgs, list):
        for idx, item in enumerate(raw_msgs, start=1):
            if not isinstance(item, dict):
                continue
            seq_val = int(item.get("seq") or idx)
            if seq_val > max_seq:
                max_seq = seq_val
            ch = models.normalize_channel_name(item.get("channel"))
            raw_kind = item.get("kind")
            raw_control = item.get("control")
            msg = models.new_board_message(
                seq=seq_val,
                sender=str(item.get("sender") or item.get("agent") or "worker"),
                summary=str(item.get("summary") or item.get("text") or ""),
                channel=ch,
                channels=item.get("channels") if isinstance(item.get("channels"), list) else [ch],
                msg_type=models.normalize_message_type(item.get("type") or raw_kind),
                kind=str(raw_kind).strip().lower() if raw_kind is not None and str(raw_kind).strip() else None,
                control=dict(raw_control) if isinstance(raw_control, dict) else None,
                sender_role=models.normalize_role(item.get("senderRole")),
                sender_tier=int(item.get("senderTier") or 2),
                mentions=[str(m) for m in (item.get("mentions") or []) if str(m).strip()],
                channel_mentions=[str(c) for c in (item.get("channelMentions") or []) if str(c).strip()],
                reply_to=item.get("replyTo"),
                thread_id=item.get("threadId"),
                task_id=item.get("taskId"),
                context_id=item.get("contextId"),
                details=str(item.get("details") or ""),
                files=[str(f) for f in (item.get("files") or []) if str(f).strip()],
                refs=[dict(r) for r in (item.get("refs") or []) if isinstance(r, dict)],
                status=str(item.get("status") or "open"),
                msg_id=str(item.get("id") or models.new_id("bmsg-")),
            )
            if isinstance(item.get("readBy"), list):
                msg["readBy"] = [str(r) for r in item["readBy"] if str(r).strip()]
            if item.get("createdAt"):
                msg["createdAt"] = str(item["createdAt"])
            if item.get("updatedAt"):
                msg["updatedAt"] = str(item["updatedAt"])
            messages_out.append(msg)

    base["messages"] = messages_out
    next_seq_raw = int(raw.get("nextSeq") or (max_seq + 1))
    base["nextSeq"] = max(next_seq_raw, max_seq + 1)
    base["updatedAt"] = str(raw.get("updatedAt") or base["updatedAt"])
    return base


def read_swarm_meta(swarm_id: str, *, state_dir: Path | None = None) -> dict[str, Any] | None:
    paths = resolve_swarm_paths(swarm_id, state_dir=state_dir)
    data = read_json_safe(paths.meta_file, None)
    if not isinstance(data, dict):
        return None
    return migrate_meta_v1_to_v2(data, paths.swarm_id)


def upsert_swarm_meta(
    swarm_id: str,
    *,
    title: str | None = None,
    goal: str | None = None,
    mission: str | None = None,
    workspace: str | None = None,
    workspace_path: str | None = None,
    primary_conversation_id: str | None = None,
    coordinator_conversation_id: str | None = None,
    conversation_id: str | None = None,
    project_id: str | None = None,
    archived: bool | None = None,
    wake_seq: int | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Create or update a swarm's v2 `meta.json` atomically."""
    paths = resolve_swarm_paths(swarm_id, state_dir=state_dir)
    ts = models.now_iso()
    ws_input = workspace if workspace is not None else workspace_path
    primary_input = primary_conversation_id if primary_conversation_id is not None else conversation_id

    with atomic_file_lock(paths.meta_lock):
        existing = read_json_safe(paths.meta_file, None, raise_on_error=True)
        meta = migrate_meta_v1_to_v2(existing, paths.swarm_id)

        if ws_input is not None and str(ws_input).strip():
            norm_ws = normalize_local_path(ws_input)
            meta["workspace"] = norm_ws
            if title is None and meta.get("title") in ("", paths.swarm_id, "Default Swarm"):
                meta["title"] = Path(norm_ws).name or paths.swarm_id

        if title is not None and str(title).strip():
            meta["title"] = str(title).strip()

        if goal is not None:
            meta["goal"] = str(goal)
        if mission is not None:
            meta["mission"] = str(mission)

        cids = [str(x) for x in (meta.get("conversationIds") or []) if str(x).strip()]
        if primary_input is not None and str(primary_input).strip():
            pcid = str(primary_input).strip()
            meta["primaryConversationId"] = pcid
            if pcid not in cids:
                cids.append(pcid)

        if coordinator_conversation_id is not None and str(coordinator_conversation_id).strip():
            ccid = str(coordinator_conversation_id).strip()
            meta["coordinatorConversationId"] = ccid
            if ccid not in cids:
                cids.append(ccid)

        meta["conversationIds"] = cids

        if project_id is not None and str(project_id).strip():
            meta["projectId"] = str(project_id).strip()

        if archived is not None:
            meta["archived"] = bool(archived)

        if wake_seq is not None:
            meta["wakeSeq"] = max(int(meta.get("wakeSeq") or 0), int(wake_seq))
        else:
            meta["wakeSeq"] = max(0, int(meta.get("wakeSeq") or 0))

        meta["schemaVersion"] = models.SCHEMA_VERSION
        meta["swarmId"] = paths.swarm_id
        meta["updatedAt"] = ts
        if not meta.get("createdAt"):
            meta["createdAt"] = ts
        atomic_write_json(paths.meta_file, meta)
        with _CONV_RESOLVE_CACHE_LOCK:
            _CONV_RESOLVE_CACHE.clear()
        return meta


def read_board_raw(swarm_id: str, *, state_dir: Path | None = None) -> dict[str, Any]:
    """Read and migrate `board.json` for `swarm_id` (returns default v2 state if absent)."""
    paths = resolve_swarm_paths(swarm_id, state_dir=state_dir)
    raw = read_json_safe(paths.board_file, None)
    return migrate_board_v1_to_v2(raw, paths.swarm_id)


def write_board_raw(swarm_id: str, board: dict[str, Any], *, state_dir: Path | None = None) -> None:
    """Atomically write v2 `board.json` under the swarm board lock."""
    paths = resolve_swarm_paths(swarm_id, state_dir=state_dir)
    with atomic_file_lock(paths.board_lock):
        payload = migrate_board_v1_to_v2(board, paths.swarm_id)
        payload["updatedAt"] = models.now_iso()
        atomic_write_json(paths.board_file, payload)


def archive_swarm(swarm_id: str, *, archived: bool = True, state_dir: Path | None = None) -> dict[str, Any]:
    """Archive or unarchive a swarm."""
    sid = sanitize_swarm_id(swarm_id) or "default"
    if sid == "default" and archived:
        raise ValueError("Cannot archive the default swarm.")
    paths = resolve_swarm_paths(sid, state_dir=state_dir)
    if not paths.swarm_dir.is_dir() and not paths.meta_file.is_file():
        raise KeyError(f"Swarm '{sid}' not found.")
    return upsert_swarm_meta(sid, archived=bool(archived), state_dir=state_dir)


def rename_swarm(swarm_id: str, title: str, *, state_dir: Path | None = None) -> dict[str, Any]:
    """Rename an existing swarm."""
    sid = sanitize_swarm_id(swarm_id) or "default"
    clean_title = str(title or "").strip()
    if not clean_title:
        raise ValueError("Swarm title must be non-empty.")
    paths = resolve_swarm_paths(sid, state_dir=state_dir)
    if sid != "default" and not paths.swarm_dir.is_dir() and not paths.meta_file.is_file():
        raise KeyError(f"Swarm '{sid}' not found.")
    return upsert_swarm_meta(sid, title=clean_title, state_dir=state_dir)


def _iter_existing_swarm_metas(*, state_dir: Path | None = None) -> list[dict[str, Any]]:
    swarms_dir = (state_dir / "swarms") if state_dir is not None else get_swarms_dir()
    if not swarms_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    try:
        for entry in sorted(swarms_dir.iterdir()):
            if not entry.is_dir():
                continue
            sid = entry.name
            meta = read_swarm_meta(sid, state_dir=state_dir) or models.default_swarm_meta(sid)
            out.append(meta)
    except OSError:
        pass
    return out


def _registry_paths(state_dir: Path | None = None) -> tuple[Path, Path]:
    base = state_dir if state_dir is not None else get_state_dir()
    return base / "registry.json", base / "registry.lock"


def _prune_registry_unlocked(reg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    changed = False
    if "activeSwarmId" in reg:
        reg.pop("activeSwarmId", None)
        changed = True
    servers = reg.get("servers")
    if not isinstance(servers, dict):
        servers = {}
        reg["servers"] = servers
        changed = True
    for key in list(servers.keys()):
        entry = servers.get(key)
        if not isinstance(entry, dict):
            servers.pop(key, None)
            changed = True
            continue
        try:
            pid_val = int(entry.get("pid") or key)
        except Exception:
            pid_val = 0
        if not pid_alive(pid_val):
            servers.pop(key, None)
            changed = True
    return reg, changed


def get_registry(*, state_dir: Path | None = None) -> dict[str, Any]:
    """Read `registry.json` and prune entries whose process PID is no longer alive."""
    rfile, rlock = _registry_paths(state_dir)
    if not rfile.is_file():
        return {"servers": {}, "updatedAt": models.now_iso()}
    with atomic_file_lock(rlock):
        raw = read_json_safe(rfile, {}, raise_on_error=True)
        reg = dict(raw) if isinstance(raw, dict) else {}
        reg, changed = _prune_registry_unlocked(reg)
        if changed:
            reg["updatedAt"] = models.now_iso()
            atomic_write_json(rfile, reg)
        return reg


def register_server_instance(
    *,
    http_url: str,
    swarm_id: str = "default",
    workspace: str | None = None,
    workspace_path: str | None = None,
    conversation_id: str | None = None,
    pid: int | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Register or update a live HTTP server entry in `registry.json`."""
    proc_pid = int(pid or os.getpid())
    sid = sanitize_swarm_id(swarm_id) or "default"
    raw_ws = workspace if workspace is not None else workspace_path
    norm_ws = normalize_local_path(raw_ws) if raw_ws else None
    ts = models.now_iso()
    rfile, rlock = _registry_paths(state_dir)
    with atomic_file_lock(rlock):
        raw = read_json_safe(rfile, {}, raise_on_error=True)
        reg = dict(raw) if isinstance(raw, dict) else {}
        reg, _ = _prune_registry_unlocked(reg)
        entry = {
            "pid": proc_pid,
            "swarmId": sid,
            "workspace": norm_ws,
            "conversationId": str(conversation_id).strip() if conversation_id else None,
            "httpUrl": str(http_url).rstrip("/"),
            "updatedAt": ts,
        }
        reg.setdefault("servers", {})[str(proc_pid)] = entry
        reg["updatedAt"] = ts
        atomic_write_json(rfile, reg)
        return entry


def unregister_server_instance(
    *,
    pid: int | None = None,
    http_url: str | None = None,
    state_dir: Path | None = None,
) -> None:
    """Remove this server instance from `registry.json` and prune dead PIDs."""
    proc_pid = str(int(pid or os.getpid()))
    rfile, rlock = _registry_paths(state_dir)
    if not rfile.is_file():
        return
    try:
        with atomic_file_lock(rlock, timeout=2.0):
            raw = read_json_safe(rfile, {}, raise_on_error=True)
            if not isinstance(raw, dict):
                return
            reg, changed = _prune_registry_unlocked(dict(raw))
            servers = reg.get("servers") or {}
            if proc_pid in servers:
                servers.pop(proc_pid, None)
                changed = True
            if http_url:
                clean_url = str(http_url).rstrip("/")
                for k in [k for k, v in servers.items() if isinstance(v, dict) and v.get("httpUrl") == clean_url]:
                    servers.pop(k, None)
                    changed = True
            if changed:
                reg["updatedAt"] = models.now_iso()
                atomic_write_json(rfile, reg)
    except Exception:
        pass


def is_conversation_in_swarm(meta: dict[str, Any] | None, conversation_id: str | None) -> bool:
    """Return True if `conversation_id` belongs to `meta` (primary, coordinator, or conversationIds)."""
    if not isinstance(meta, dict) or not conversation_id:
        return False
    clean = str(conversation_id).strip()
    if not clean:
        return False
    if str(meta.get("primaryConversationId") or "").strip() == clean:
        return True
    if str(meta.get("coordinatorConversationId") or "").strip() == clean:
        return True
    if str(meta.get("conversationId") or "").strip() == clean:
        return True
    cids = [str(x).strip() for x in (meta.get("conversationIds") or [])]
    return clean in cids


def resolve_swarm_for_conversation(
    conversation_id: str | None,
    *,
    state_dir: Path | str | None = None,
) -> str | None:
    """Resolve the single swarm ID bound to `conversation_id` (strict 1:1; never falls back to workspace or other sessions)."""
    clean_conv = str(conversation_id or "").strip()
    if not clean_conv:
        return None
    sdir = Path(normalize_local_path(str(state_dir))) if state_dir is not None else None
    conv_sid = sanitize_swarm_id(clean_conv)
    swarms_dir = (sdir / "swarms") if sdir is not None else get_swarms_dir()
    cache_key = (str(swarms_dir), clean_conv)
    now_m = time.monotonic()
    with _CONV_RESOLVE_CACHE_LOCK:
        cached = _CONV_RESOLVE_CACHE.get(cache_key)
        if cached is not None and (now_m - cached[0]) < _CONV_RESOLVE_TTL_S:
            return cached[1]
    if not swarms_dir.is_dir():
        return None

    def _match_rank(meta: dict[str, Any]) -> int:
        if str(meta.get("primaryConversationId") or "").strip() == clean_conv:
            return 4
        if str(meta.get("coordinatorConversationId") or "").strip() == clean_conv:
            return 3
        if conv_sid and sanitize_swarm_id(meta.get("swarmId")) == conv_sid:
            return 2
        if is_conversation_in_swarm(meta, clean_conv):
            return 1
        return 0

    matches: list[tuple[int, str, str]] = []
    try:
        for entry in sorted(swarms_dir.iterdir()):
            if not entry.is_dir():
                continue
            sid = sanitize_swarm_id(entry.name)
            if not sid:
                continue
            paths = resolve_swarm_paths(sid, state_dir=sdir)
            if not (paths.meta_file.is_file() or paths.board_file.is_file() or paths.tasks_file.is_file()):
                continue
            meta = read_swarm_meta(sid, state_dir=sdir) or models.default_swarm_meta(sid)
            if meta.get("archived"):
                continue
            rank = _match_rank(meta)
            if rank > 0:
                matches.append((rank, str(meta.get("updatedAt") or ""), sid))
    except OSError:
        return None

    if not matches:
        with _CONV_RESOLVE_CACHE_LOCK:
            _CONV_RESOLVE_CACHE[cache_key] = (time.monotonic(), None)
        return None
    matches.sort(key=lambda r: (r[0], r[1]), reverse=True)
    res_sid = matches[0][2]
    with _CONV_RESOLVE_CACHE_LOCK:
        if len(_CONV_RESOLVE_CACHE) > 256:
            _CONV_RESOLVE_CACHE.clear()
        _CONV_RESOLVE_CACHE[cache_key] = (time.monotonic(), res_sid)
    return res_sid


def resolve_swarm_id(
    *,
    swarm_id: str | None = None,
    conversation_id: str | None = None,
    workspace: str | None = None,
    workspace_path: str | None = None,
    state_dir: Path | None = None,
) -> str:
    """Resolve swarm_id strictly: explicit swarm_id > conversation_id (1:1) > workspace > env > 'default'."""
    explicit = sanitize_swarm_id(swarm_id)
    if explicit:
        return explicit

    clean_conv = str(conversation_id).strip() if conversation_id and str(conversation_id).strip() else None
    if clean_conv:
        matched = resolve_swarm_for_conversation(clean_conv, state_dir=state_dir)
        if matched:
            return matched
        return derive_swarm_id(conversation_id=clean_conv)

    raw_ws = workspace if workspace is not None else workspace_path
    if raw_ws and str(raw_ws).strip():
        norm_ws = normalize_local_path(raw_ws)
        if norm_ws:
            return derive_swarm_id(workspace=norm_ws)

    env_sid = sanitize_swarm_id(os.environ.get("HERMES_A2A_SWARM_ID"))
    if env_sid:
        return env_sid

    return "default"


def resolve_scoped_swarms(
    *,
    conversation_id: str | None = None,
    workspace: str | None = None,
    swarm_id: str | None = None,
    include_archived: bool = False,
    state_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return ONLY swarms matching the caller's session/workspace/explicit scope (never leaks unrelated swarms)."""
    metas = _iter_existing_swarm_metas(state_dir=state_dir)
    if not include_archived:
        metas = [m for m in metas if not m.get("archived")]

    explicit = sanitize_swarm_id(swarm_id)
    if explicit:
        return [m for m in metas if sanitize_swarm_id(m.get("swarmId")) == explicit]

    clean_conv = str(conversation_id).strip() if conversation_id and str(conversation_id).strip() else None
    if clean_conv:
        matched = [m for m in metas if is_conversation_in_swarm(m, clean_conv)]
        if matched:
            return matched

    if workspace and str(workspace).strip():
        norm_ws = normalize_local_path(workspace)
        if norm_ws:
            return [m for m in metas if m.get("workspace") and paths_equal(str(m["workspace"]), norm_ws)]

    return []


def _summarize_swarm_counts(
    paths: SwarmPaths,
    in_memory_tasks: list[dict[str, Any]] | None = None,
) -> tuple[int, int, int, str]:
    tasks_list: list[dict[str, Any]] = []
    if in_memory_tasks is not None:
        tasks_list = list(in_memory_tasks)
    else:
        raw_t = read_json_safe(paths.tasks_file, None)
        if isinstance(raw_t, dict) and isinstance(raw_t.get("tasks"), dict):
            tasks_list = [v for v in raw_t["tasks"].values() if isinstance(v, dict)]

    active_count = 0
    latest_ts = ""
    for task in tasks_list:
        st = ((task.get("status") or {}).get("state")) or ""
        if st in models.ACTIVE_STATES or (st and st not in models.TERMINAL_STATES):
            active_count += 1
        ts = str(((task.get("status") or {}).get("timestamp")) or "")
        if ts > latest_ts:
            latest_ts = ts

    raw_b = read_json_safe(paths.board_file, None)
    msg_count = 0
    if isinstance(raw_b, dict):
        msgs = raw_b.get("messages")
        if isinstance(msgs, list):
            msg_count = len(msgs)
        bts = str(raw_b.get("updatedAt") or "")
        if bts > latest_ts:
            latest_ts = bts

    return active_count, len(tasks_list), msg_count, latest_ts


def list_swarms(
    *,
    include_archived: bool = True,
    current_http_url: str | None = None,
    runtime_tasks_by_swarm: dict[str, list[dict[str, Any]]] | None = None,
    conversation_id: str | None = None,
    workspace: str | None = None,
    state_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """List swarms with live task/message counts, optionally filtered to a session/workspace scope."""
    reg = get_registry(state_dir=state_dir)
    servers = reg.get("servers") or {}
    live_by_swarm: dict[str, str] = {}
    fallback_live_url: str | None = str(current_http_url).rstrip("/") if current_http_url else None
    for srv in servers.values():
        if not isinstance(srv, dict) or not srv.get("httpUrl"):
            continue
        url = str(srv["httpUrl"]).rstrip("/")
        sid = sanitize_swarm_id(srv.get("swarmId")) or "default"
        live_by_swarm.setdefault(sid, url)
        if fallback_live_url is None:
            fallback_live_url = url

    swarm_ids: list[str] = ["default"]
    swarms_dir = (state_dir / "swarms") if state_dir is not None else get_swarms_dir()
    if swarms_dir.is_dir():
        try:
            for entry in sorted(swarms_dir.iterdir()):
                if entry.is_dir():
                    sid = sanitize_swarm_id(entry.name)
                    if sid and sid not in swarm_ids:
                        swarm_ids.append(sid)
        except OSError:
            pass
    for sid in (runtime_tasks_by_swarm or {}).keys():
        clean = sanitize_swarm_id(sid)
        if clean and clean not in swarm_ids:
            swarm_ids.append(clean)

    items: list[dict[str, Any]] = []
    clean_conv = str(conversation_id).strip() if conversation_id and str(conversation_id).strip() else None
    norm_ws = normalize_local_path(workspace) if workspace and str(workspace).strip() else None

    for sid in swarm_ids:
        paths = resolve_swarm_paths(sid, state_dir=state_dir)
        meta = read_swarm_meta(sid, state_dir=state_dir) or models.default_swarm_meta(sid)
        if not include_archived and meta.get("archived"):
            continue
        if clean_conv and not is_conversation_in_swarm(meta, clean_conv):
            if not (norm_ws and meta.get("workspace") and paths_equal(str(meta["workspace"]), norm_ws)):
                continue
        elif not clean_conv and norm_ws:
            if not (meta.get("workspace") and paths_equal(str(meta["workspace"]), norm_ws)):
                continue

        in_mem = (runtime_tasks_by_swarm or {}).get(sid)
        active_tasks, total_tasks, msg_count, latest_ts = _summarize_swarm_counts(paths, in_mem)
        updated_at = max(str(meta.get("updatedAt") or ""), latest_ts) or str(meta.get("createdAt") or "")
        http_url = live_by_swarm.get(sid) or fallback_live_url
        items.append({
            "swarmId": sid,
            "title": meta.get("title") or ("Default Swarm" if sid == "default" else sid),
            "goal": meta.get("goal") or "",
            "mission": meta.get("mission") or "",
            "workspace": meta.get("workspace"),
            "primaryConversationId": meta.get("primaryConversationId"),
            "coordinatorConversationId": meta.get("coordinatorConversationId"),
            "conversationIds": list(meta.get("conversationIds") or []),
            "projectId": meta.get("projectId"),
            "createdAt": meta.get("createdAt"),
            "updatedAt": updated_at,
            "archived": bool(meta.get("archived", False)),
            "activeTasks": active_tasks,
            "totalTasks": total_tasks,
            "messageCount": msg_count,
            "httpUrl": http_url,
            "online": bool(http_url),
        })

    default_items = [x for x in items if x["swarmId"] == "default"]
    other_active = sorted(
        [x for x in items if x["swarmId"] != "default" and not x.get("archived")],
        key=lambda x: str(x.get("updatedAt") or ""),
        reverse=True,
    )
    other_archived = sorted(
        [x for x in items if x["swarmId"] != "default" and x.get("archived")],
        key=lambda x: str(x.get("updatedAt") or ""),
        reverse=True,
    )
    return default_items + other_active + other_archived


class SwarmStore:
    """Object-oriented facade over v2 swarm metadata, board files, locks, and registry."""

    def __init__(self, state_dir: Path | str | None = None) -> None:
        self._state_dir = Path(normalize_local_path(str(state_dir))) if state_dir else None

    @property
    def state_dir(self) -> Path:
        return self._state_dir if self._state_dir is not None else get_state_dir()

    def paths(self, swarm_id: str = "default") -> SwarmPaths:
        return resolve_swarm_paths(swarm_id, state_dir=self.state_dir)

    def thread_lock(self, swarm_id: str = "default") -> threading.RLock:
        return get_swarm_thread_lock(swarm_id)

    def read_meta(self, swarm_id: str) -> dict[str, Any] | None:
        return read_swarm_meta(swarm_id, state_dir=self.state_dir)

    def upsert_meta(self, swarm_id: str, **kwargs: Any) -> dict[str, Any]:
        return upsert_swarm_meta(swarm_id, state_dir=self.state_dir, **kwargs)

    def read_board(self, swarm_id: str) -> dict[str, Any]:
        return read_board_raw(swarm_id, state_dir=self.state_dir)

    def write_board(self, swarm_id: str, board: dict[str, Any]) -> None:
        write_board_raw(swarm_id, board, state_dir=self.state_dir)

    def resolve_swarm_for_conversation(self, conversation_id: str | None) -> str | None:
        return resolve_swarm_for_conversation(conversation_id, state_dir=self.state_dir)

    def resolve_swarm_id(self, **kwargs: Any) -> str:
        return resolve_swarm_id(state_dir=self.state_dir, **kwargs)

    def resolve_scoped_swarms(self, **kwargs: Any) -> list[dict[str, Any]]:
        return resolve_scoped_swarms(state_dir=self.state_dir, **kwargs)

    def list_swarms(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list_swarms(state_dir=self.state_dir, **kwargs)

    def archive_swarm(self, swarm_id: str, *, archived: bool = True) -> dict[str, Any]:
        return archive_swarm(swarm_id, archived=archived, state_dir=self.state_dir)

    def rename_swarm(self, swarm_id: str, title: str) -> dict[str, Any]:
        return rename_swarm(swarm_id, title, state_dir=self.state_dir)

    def allocate_wake_seq(self, swarm_id: str) -> int:
        """Atomically increment and persist monotonic `wakeSeq` in `meta.json`."""
        paths = self.paths(swarm_id)
        with atomic_file_lock(paths.meta_lock):
            existing = read_json_safe(paths.meta_file, None, raise_on_error=True)
            meta = migrate_meta_v1_to_v2(existing, paths.swarm_id)
            next_seq = max(0, int(meta.get("wakeSeq") or 0)) + 1
            meta["wakeSeq"] = next_seq
            meta["updatedAt"] = models.now_iso()
            atomic_write_json(paths.meta_file, meta)
            return next_seq
