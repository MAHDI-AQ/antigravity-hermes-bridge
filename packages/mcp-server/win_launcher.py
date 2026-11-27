"""Windows launcher and cross-platform hidden subprocess/tree-kill utilities for Hermes workers."""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import runpy
import signal
import socket
import subprocess
import sys
from typing import Any


def _install_safe_socketpair(timeout_s: float = 10.0) -> None:
    if sys.platform != "win32":
        return

    def _safe_socketpair(family: int | None = None, type: int = socket.SOCK_STREAM, proto: int = 0):
        af_unix = getattr(socket, "AF_UNIX", None)
        if family is None or (af_unix is not None and family == af_unix):
            family = socket.AF_INET
        if family == socket.AF_INET:
            host = "127.0.0.1"
        elif family == socket.AF_INET6:
            host = "::1"
        else:
            raise ValueError("Only AF_INET and AF_INET6 socket address families are supported")
        if type != socket.SOCK_STREAM:
            raise ValueError("Only SOCK_STREAM socket type is supported")
        if proto != 0:
            raise ValueError("Only protocol zero is supported")

        lsock = socket.socket(family, type, proto)
        try:
            lsock.bind((host, 0))
            lsock.listen(1)
            addr, port = lsock.getsockname()[:2]
            csock = socket.socket(family, type, proto)
            try:
                # Blocking connect ensures the peer is queued before accept() runs.
                csock.settimeout(timeout_s)
                csock.connect((addr, port))
                csock.settimeout(None)
                lsock.settimeout(timeout_s)
                ssock, _ = lsock.accept()
            except BaseException:
                csock.close()
                raise
        finally:
            lsock.close()

        try:
            if ssock.getsockname() != csock.getpeername() or csock.getsockname() != ssock.getpeername():
                raise ConnectionError("Unexpected peer connection")
        except BaseException:
            ssock.close()
            csock.close()
            raise

        ssock.setblocking(True)
        csock.setblocking(True)
        return ssock, csock

    socket.socketpair = _safe_socketpair
    if hasattr(socket, "_fallback_socketpair"):
        socket._fallback_socketpair = _safe_socketpair


def resolve_hermes_win_runtime(hermes_home: Path | None = None) -> tuple[Path, Path] | None:
    """Return `(python_exe, hermes_agent_dir)` on Windows so workers bypass stdlib `_fallback_socketpair`."""
    if sys.platform != "win32":
        return None
    if hermes_home is None:
        env_home = os.environ.get("HERMES_HOME", "").strip()
        if env_home:
            hermes_home = Path(env_home)
        else:
            local_app = os.environ.get("LOCALAPPDATA", "").strip()
            local_base = Path(local_app) if local_app else (Path.home() / "AppData" / "Local")
            hermes_home = local_base / "hermes"

    agent_dir = hermes_home / "hermes-agent"
    if not (agent_dir / "hermes_cli" / "main.py").is_file():
        return None

    py_exe: Path | None = None
    cmd_shim = hermes_home / "bin" / "hermes.cmd"
    if cmd_shim.is_file():
        try:
            text = cmd_shim.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'"([^"]*python\.exe)"', text, flags=re.IGNORECASE)
            if m and Path(m.group(1)).is_file():
                py_exe = Path(m.group(1))
        except OSError:
            pass
    if py_exe is None:
        tools_dir = hermes_home / "tools"
        if tools_dir.is_dir():
            candidates = sorted(tools_dir.glob("python-*/python.exe"))
            if candidates:
                py_exe = candidates[-1]
    if py_exe is None or not py_exe.is_file():
        return None
    return py_exe, agent_dir


def build_hermes_worker_argv(
    hermes_args: list[str],
    *,
    hermes_home: Path | None = None,
    hermes_exe: Path | None = None,
    launcher_script: Path | None = None,
) -> list[str]:
    """Build the command argv to invoke `hermes` cleanly (via `win_launcher.py` on Windows when available)."""
    launcher = launcher_script or Path(__file__).resolve()
    win_rt = resolve_hermes_win_runtime(hermes_home) if launcher.is_file() else None
    if win_rt is not None:
        py_exe, agent_dir = win_rt
        return [
            str(py_exe),
            "-I",
            str(launcher),
            "--hermes-agent-dir",
            str(agent_dir),
            "--",
            *hermes_args,
        ]
    exe = str(hermes_exe) if hermes_exe else "hermes"
    return [exe, *hermes_args]


def hidden_subprocess_kwargs(*, new_process_group: bool = True) -> dict[str, Any]:
    """Return platform kwargs that hide console windows on Windows and isolate process groups."""
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        if new_process_group:
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": flags}
    if new_process_group:
        return {"start_new_session": True}
    return {}


async def spawn_hermes_subprocess(
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stream_limit: int = 8 * 1024 * 1024,
    hidden: bool = True,
    new_process_group: bool = True,
) -> asyncio.subprocess.Process:
    """Spawn an async worker/CLI subprocess with hidden console and clean stream buffers."""
    kwargs: dict[str, Any] = {}
    if hidden:
        kwargs.update(hidden_subprocess_kwargs(new_process_group=new_process_group))
    elif new_process_group and sys.platform != "win32":
        kwargs["start_new_session"] = True
    return await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd or None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=stream_limit,
        env=env,
        **kwargs,
    )


_K32_DLL: Any = None


def _get_kernel32() -> Any:
    """Return a private kernel32 WinDLL with explicit 64-bit-safe argtypes/restype."""
    global _K32_DLL
    if _K32_DLL is not None:
        return _K32_DLL
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    k32.GetProcessTimes.restype = wintypes.BOOL
    _K32_DLL = k32
    return k32


def _win32_snapshot_ppid_map() -> dict[int, list[int]]:
    """Return `{ppid: [child_pid, ...]}` using Win32 Toolhelp snapshot."""
    if sys.platform != "win32":
        return {}
    th32cs_snapprocess = 0x00000002
    invalid_handle = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32 = _get_kernel32()
    snap = k32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
    if not snap or snap == invalid_handle:
        return {}
    children: dict[int, list[int]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if k32.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                pid = int(entry.th32ProcessID)
                ppid = int(entry.th32ParentProcessID)
                if pid > 0 and pid != ppid:
                    children.setdefault(ppid, []).append(pid)
                if not k32.Process32NextW(snap, ctypes.byref(entry)):
                    break
    except Exception:
        pass
    finally:
        k32.CloseHandle(snap)
    return children


def _win32_pid_to_ppid_map() -> dict[int, int]:
    """Return `{pid: ppid}` using Win32 Toolhelp snapshot for complete ancestor walks."""
    tree = _win32_snapshot_ppid_map()
    mapping: dict[int, int] = {}
    for ppid, children in tree.items():
        for child in children:
            mapping[child] = ppid
    return mapping


def _win32_descendant_pids(root_pid: int) -> list[int]:
    """Return descendant PIDs of `root_pid` in bottom-up order."""
    if sys.platform != "win32" or root_pid <= 0:
        return []
    tree = _win32_snapshot_ppid_map()
    ordered: list[int] = []
    seen: set[int] = {root_pid}
    stack = [root_pid]
    while stack:
        cur = stack.pop()
        for child in tree.get(cur) or ():
            if child not in seen:
                seen.add(child)
                ordered.append(child)
                stack.append(child)
    ordered.reverse()
    return ordered


def _win32_handle_create_time(k32: Any, handle: Any) -> int | None:
    ct = wintypes.FILETIME()
    et = wintypes.FILETIME()
    kt = wintypes.FILETIME()
    ut = wintypes.FILETIME()
    if not k32.GetProcessTimes(handle, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)):
        return None
    return (int(ct.dwHighDateTime) << 32) | int(ct.dwLowDateTime)


def _win32_get_process_create_time(pid: int) -> int | None:
    """Return Win32 process creation FILETIME (100ns ticks) or None if unavailable."""
    if sys.platform != "win32" or pid <= 0:
        return None
    process_query_limited = 0x1000
    k32 = _get_kernel32()
    handle = k32.OpenProcess(process_query_limited, False, int(pid))
    if not handle:
        return None
    try:
        return _win32_handle_create_time(k32, handle)
    except Exception:
        return None
    finally:
        k32.CloseHandle(handle)


def _win32_descendant_pids_with_times(root_pid: int) -> list[tuple[int, int | None]]:
    """Return `[(pid, create_time), ...]` for descendants of `root_pid` in bottom-up order."""
    return [(cpid, _win32_get_process_create_time(cpid)) for cpid in _win32_descendant_pids(root_pid)]


def _win32_terminate_pid(pid: int, *, expected_create_time: int | None = None) -> None:
    """Terminate a single Windows PID via OpenProcess + TerminateProcess fallback, guarding against PID reuse."""
    if sys.platform != "win32" or pid <= 0:
        return
    process_terminate = 0x0001
    process_query_limited = 0x1000
    access = process_terminate | (process_query_limited if expected_create_time is not None else 0)
    k32 = _get_kernel32()
    handle = k32.OpenProcess(access, False, int(pid))
    if not handle:
        return
    try:
        if expected_create_time is not None:
            actual_ct = _win32_handle_create_time(k32, handle)
            if actual_ct is None or actual_ct != expected_create_time:
                return
        k32.TerminateProcess(handle, 1)
    except Exception:
        pass
    finally:
        k32.CloseHandle(handle)


def _psutil_kill_tree_sync(pid: int) -> None:
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return
    try:
        parent = psutil.Process(pid)
        procs = [*parent.children(recursive=True), parent]
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
        try:
            psutil.wait_procs(procs, timeout=1.0)
        except Exception:
            pass
    except Exception:
        pass


def kill_process_tree_sync(pid: int, *, timeout: float = 3.0) -> None:
    """Synchronously terminate a process and all its children (for atexit or sync cleanup)."""
    if not isinstance(pid, int) or pid <= 0:
        return
    try:
        if sys.platform == "win32":
            root_ct = _win32_get_process_create_time(pid)
            descendants = _win32_descendant_pids_with_times(pid)
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
                **hidden_subprocess_kwargs(new_process_group=False),
            )
            _psutil_kill_tree_sync(pid)
            for child_pid, child_ct in descendants:
                _win32_terminate_pid(child_pid, expected_create_time=child_ct)
            _win32_terminate_pid(pid, expected_create_time=root_ct)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
    except Exception:
        pass


async def kill_process_tree(
    proc_or_pid: asyncio.subprocess.Process | int,
    *,
    timeout: float = 5.0,
) -> None:
    """Asynchronously terminate a subprocess and its descendant process tree."""
    proc: asyncio.subprocess.Process | None = None
    if isinstance(proc_or_pid, int):
        pid = proc_or_pid
    else:
        proc = proc_or_pid
        if proc.returncode is not None:
            return
        pid = proc.pid

    if not pid or pid <= 0:
        return

    try:
        if sys.platform == "win32":
            root_ct = await asyncio.to_thread(_win32_get_process_create_time, pid)
            descendants = await asyncio.to_thread(_win32_descendant_pids_with_times, pid)
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **hidden_subprocess_kwargs(new_process_group=False),
            )
            try:
                await asyncio.wait_for(killer.wait(), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError):
                killer.kill()
                await killer.wait()
            await asyncio.to_thread(_psutil_kill_tree_sync, pid)
            for child_pid, child_ct in descendants:
                _win32_terminate_pid(child_pid, expected_create_time=child_ct)
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except (asyncio.TimeoutError, TimeoutError):
                    _win32_terminate_pid(pid, expected_create_time=root_ct)
            elif proc is None:
                _win32_terminate_pid(pid, expected_create_time=root_ct)
        else:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                if proc is not None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
            if proc is not None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=min(2.0, timeout))
                    return
                except (asyncio.TimeoutError, TimeoutError):
                    pass
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                if proc is not None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            if proc is not None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
    except Exception:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass


def _cmdline_has_flag_value(cmdline: str, flags: tuple[str, ...], value: str) -> bool:
    if not cmdline or not value:
        return False
    val_esc = re.escape(value.strip())
    flag_alt = "|".join(re.escape(f) for f in flags)
    pattern = rf"(?:{flag_alt})(?:\s*=|\s+)\\?[\"']?{val_esc}\\?[\"']?(?=$|[\s\"';|&),])"
    return bool(re.search(pattern, cmdline, flags=re.IGNORECASE))


def _is_protected_process(pid: int, name: str, cmdline: str, protected_pids: set[int]) -> bool:
    if pid <= 0 or pid in protected_pids:
        return True
    exe_low = str(name or "").strip().lower()
    cmd_slash = str(cmdline or "").replace("\\", "/").lower()
    if "get-ciminstance win32_process" in cmd_slash:
        return True
    if (
        "antigravity" in exe_low
        or "antigravity.exe" in cmd_slash
        or "language_server" in exe_low
        or "language_server" in cmd_slash
    ):
        return True
    if (
        "hermes-gateway" in exe_low
        or "hermes-gateway" in cmd_slash
        or "hermes_cli/gateway" in cmd_slash
        or "hermes_cli.gateway" in cmd_slash
        or bool(re.search(r"\bhermes(?:\.exe)?\s+gateway\b", cmd_slash))
    ):
        return True
    is_node = exe_low in ("node", "node.exe") or bool(
        re.search(r"(?:^|/|\s)node(?:\.exe)?(?:\s|$|\"|')", cmd_slash)
    )
    if is_node and any(
        tok in cmd_slash
        for tok in ("sidecar", "wakeup_bridge", "swarm_board", "main.mjs", "hermes-swarm", "mcp-servers")
    ):
        return True
    if "hermes-a2a-mcp" in cmd_slash and "server.py" in cmd_slash:
        return True
    return False


def _list_process_rows_psutil_sync() -> list[dict[str, Any]] | None:
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return None
    rows: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        try:
            info = proc.info
            cmd_parts = info.get("cmdline") or []
            cmdline = " ".join(str(x) for x in cmd_parts) if isinstance(cmd_parts, list) else str(cmd_parts or "")
            cwd = ""
            try:
                cwd = proc.cwd() or ""
            except Exception:
                pass
            rows.append({
                "pid": int(info.get("pid") or 0),
                "ppid": int(info.get("ppid") or 0),
                "name": str(info.get("name") or ""),
                "cmdline": cmdline,
                "cwd": cwd,
            })
        except Exception:
            continue
    return rows


async def _list_process_rows(timeout: float = 4.0) -> list[dict[str, Any]]:
    """Return `[{pid, ppid, name, cmdline, cwd}]` for running processes cleanly across platforms."""
    psutil_rows = await asyncio.to_thread(_list_process_rows_psutil_sync)
    if psutil_rows is not None:
        return psutil_rows

    if sys.platform == "win32":
        ps_cmd = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -and ($_.CommandLine -like '*board_cli*' -or $_.CommandLine -like '*.worktrees*' -or $_.CommandLine -like '*hermes*') } | "
            "Select-Object Name,ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                ps_cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **hidden_subprocess_kwargs(new_process_group=False),
            )
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            raw_txt = out_b.decode("utf-8", errors="replace").strip()
            if not raw_txt:
                return []
            parsed = json.loads(raw_txt)
            items = parsed if isinstance(parsed, list) else ([parsed] if isinstance(parsed, dict) else [])
            return [
                {
                    "pid": int(item.get("ProcessId") or 0),
                    "ppid": int(item.get("ParentProcessId") or 0),
                    "name": str(item.get("Name") or ""),
                    "cmdline": str(item.get("CommandLine") or ""),
                    "cwd": "",
                }
                for item in items
                if isinstance(item, dict) and item.get("ProcessId")
            ]
        except Exception:
            if proc is not None and proc.returncode is None:
                await kill_process_tree(proc, timeout=1.0)
            return []

    rows = []
    proc_root = Path("/proc")
    if proc_root.is_dir():
        try:
            for entry in proc_root.iterdir():
                if not entry.name.isdigit():
                    continue
                pid = int(entry.name)
                try:
                    raw_cmd = (entry / "cmdline").read_bytes()
                    cmdline = raw_cmd.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
                except OSError:
                    continue
                if "board_cli" not in cmdline and ".worktrees" not in cmdline:
                    continue
                exe_name = ""
                try:
                    exe_name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    pass
                cwd = ""
                try:
                    cwd = os.readlink(entry / "cwd")
                except OSError:
                    pass
                ppid = 0
                try:
                    stat_txt = (entry / "stat").read_text(encoding="utf-8", errors="replace")
                    rp = stat_txt.rfind(")")
                    if rp > 0:
                        ppid = int(stat_txt[rp + 2 :].split()[1])
                except Exception:
                    pass
                rows.append({"pid": pid, "ppid": ppid, "name": exe_name, "cmdline": cmdline, "cwd": cwd})
        except OSError:
            pass
    return rows


async def interrupt_hermes_gateway_session(
    session_id: str,
    *,
    hermes_home: Path | None = None,
    timeout: float = 4.0,
) -> bool:
    """Best-effort interrupt & close of a live Hermes session inside `tui_gateway`."""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    win_rt = resolve_hermes_win_runtime(hermes_home) if sys.platform == "win32" else None
    py_exe = str(win_rt[0]) if win_rt is not None else sys.executable
    agent_dir = str(win_rt[1]) if win_rt is not None else ""
    if not py_exe:
        return False
    script = (
        "import asyncio, os, sys\n"
        "sid = sys.argv[1]\n"
        "agent_dir = sys.argv[2] if len(sys.argv) > 2 else ''\n"
        "if agent_dir and os.path.isdir(agent_dir):\n"
        "    sys.path.insert(0, agent_dir)\n"
        "async def _run():\n"
        "    from hermes_cli.gateway_client import connect_gateway\n"
        "    ok = False\n"
        "    async with connect_gateway() as client:\n"
        "        for m in ('session.interrupt', 'session.close'):\n"
        "            try:\n"
        "                await client.rpc(m, session_id=sid)\n"
        "                ok = True\n"
        "            except Exception:\n"
        "                pass\n"
        "    return ok\n"
        "print('OK' if asyncio.run(_run()) else 'NO')\n"
    )
    env = dict(os.environ)
    if hermes_home is not None:
        env["HERMES_HOME"] = str(hermes_home)
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await spawn_hermes_subprocess(
            [py_exe, "-c", script, sid, agent_dir],
            env=env,
            hidden=True,
            new_process_group=True,
        )
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode == 0 and b"OK" in out_b
    except Exception:
        if proc is not None and proc.returncode is None:
            await kill_process_tree(proc, timeout=1.5)
        return False


async def find_and_kill_orphan_worker_processes(
    *,
    swarm_id: str,
    worker_name: str,
    worktree_path: str | None = None,
    task_ids: list[str] | None = None,
    timeout: float = 4.0,
) -> list[int]:
    """Sweep lingering `win_launcher.py`, `board_cli.py`, and worktree-locked subprocesses for a killed worker."""
    sid = str(swarm_id or "").strip()
    wname = str(worker_name or "").strip()
    clean_tids = [str(t or "").strip().lower() for t in (task_ids or ()) if str(t or "").strip()]
    if not sid and not wname and not worktree_path and not clean_tids:
        return []

    rows = await _list_process_rows(timeout=timeout)
    if not rows:
        return []

    self_pid = os.getpid()
    ppid_by_pid = _win32_pid_to_ppid_map() if sys.platform == "win32" else {}
    for r in rows:
        rpid = int(r.get("pid") or 0)
        if rpid > 0:
            ppid_by_pid[rpid] = int(r.get("ppid") or 0)
    protected_pids: set[int] = {self_pid}
    cur = self_pid
    for _ in range(32):
        p = ppid_by_pid.get(cur)
        if not p or p in protected_pids:
            break
        protected_pids.add(p)
        cur = p

    norm_wt = ""
    if worktree_path:
        cand_wt = str(worktree_path).replace("\\", "/").rstrip("/").lower()
        if len(cand_wt) > 3 and Path(cand_wt).name not in ("", ".", ".."):
            norm_wt = cand_wt

    sid_low = sid.lower()
    wname_low = wname.lower()
    wt_ref_re = (
        re.compile(rf"/\.worktrees/(?:a2a-)?{re.escape(wname_low)}(?=$|[/\s\"';|&),])", flags=re.IGNORECASE)
        if wname_low
        else None
    )
    norm_wt_re = (
        re.compile(rf"{re.escape(norm_wt)}(?=$|[/\s\"';|&),])", flags=re.IGNORECASE)
        if norm_wt
        else None
    )
    swarm_dir_re = (
        re.compile(rf"/swarms/{re.escape(sid_low)}(?=$|[/\s\"';|&),])", flags=re.IGNORECASE)
        if sid_low
        else None
    )

    matched_pids: list[int] = []
    for row in rows:
        pid = int(row.get("pid") or 0)
        name = str(row.get("name") or "")
        cmd = str(row.get("cmdline") or "")
        if _is_protected_process(pid, name, cmd, protected_pids):
            continue
        cwd = str(row.get("cwd") or "")
        cmd_slash = cmd.replace("\\", "/").lower()
        cwd_slash = cwd.replace("\\", "/").lower()

        is_board_cli = "board_cli.py" in cmd_slash or bool(re.search(r"\bboard_cli\b", cmd_slash))
        hit = False
        if clean_tids and "prompt-" in cmd_slash:
            for tid in clean_tids:
                stem = tid if tid.startswith("task-") else f"task-{tid}"
                if f"prompt-{stem}" in cmd_slash:
                    hit = True
                    break

        if not hit and is_board_cli and wname:
            agent_hit = _cmdline_has_flag_value(cmd, ("--agent", "--sender"), wname)
            swarm_hit = (
                _cmdline_has_flag_value(cmd, ("--swarm",), sid)
                or (swarm_dir_re is not None and bool(swarm_dir_re.search(cmd_slash)))
            )
            if agent_hit and (swarm_hit or not sid):
                hit = True

        if not hit and norm_wt:
            if (norm_wt_re is not None and bool(norm_wt_re.search(cmd_slash))) or (
                cwd_slash and (cwd_slash == norm_wt or cwd_slash.startswith(norm_wt + "/"))
            ):
                hit = True

        if not hit and wt_ref_re is not None:
            if wt_ref_re.search(cmd_slash) or (cwd_slash and wt_ref_re.search(cwd_slash)):
                hit = True

        if hit:
            matched_pids.append(pid)

    if not matched_pids:
        return []

    await asyncio.gather(*(kill_process_tree(pid, timeout=timeout) for pid in matched_pids), return_exceptions=True)
    return matched_pids


def main() -> None:
    argv = list(sys.argv[1:])
    hermes_agent_dir = ""
    if len(argv) >= 2 and argv[0] == "--hermes-agent-dir":
        hermes_agent_dir = argv[1]
        argv = argv[2:]
    if argv and argv[0] == "--":
        argv = argv[1:]

    _install_safe_socketpair()

    os.environ.pop("PYTHONHOME", None)
    os.environ.pop("PYTHONPATH", None)
    os.environ.pop("VIRTUAL_ENV", None)

    if hermes_agent_dir and Path(hermes_agent_dir).is_dir():
        sys.path.insert(0, hermes_agent_dir)
    sys._hermes_pin_default_home = True  # type: ignore[attr-defined]

    import hermes_bootstrap  # noqa: F401
    from hermes_constants import get_default_hermes_root

    if not os.environ.get("HERMES_HOME"):
        os.environ["HERMES_HOME"] = str(get_default_hermes_root())

    sys.argv = ["hermes", *argv]
    runpy.run_module("hermes_cli.main", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
