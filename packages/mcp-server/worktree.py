"""Isolated git worktree creation, diff inspection, removal, and pruning."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any

import store
import win_launcher

_SLUG_CLEAN_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_REPO_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


def _repo_lock(repo_root: str | Path) -> asyncio.Lock:
    """Return a per-event-loop, per-repo asyncio.Lock for serializing mutating git worktree ops."""
    loop = asyncio.get_running_loop()
    key = (id(loop), os.path.normcase(store.normalize_local_path(str(repo_root))))
    lock = _REPO_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _REPO_LOCKS[key] = lock
    return lock


@dataclass(frozen=True)
class WorktreeInfo:
    """Metadata describing a provisioned git worktree."""

    repo_root: str
    worktree_path: str
    branch: str
    created: bool = True


def _sanitize_slug(raw: str) -> str:
    cleaned = _SLUG_CLEAN_RE.sub("-", str(raw or "").strip()).strip("-.")
    return cleaned[:48] or "worker"


async def _run_git(
    cwd: str | Path,
    *git_args: str,
    timeout: float = 20.0,
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *git_args,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **win_launcher.hidden_subprocess_kwargs(new_process_group=True),
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return int(proc.returncode or 0), out_b, err_b
    except asyncio.CancelledError:
        await win_launcher.kill_process_tree(proc)
        raise
    except (asyncio.TimeoutError, TimeoutError):
        await win_launcher.kill_process_tree(proc)
        return -1, b"", f"git {' '.join(git_args[:2])} timed out after {timeout:.1f}s".encode("utf-8")


async def resolve_repo_root(workdir: str | Path, *, timeout: float = 10.0) -> Path:
    """Resolve the git repository top-level root for `workdir`, raising ValueError if invalid."""
    norm = store.normalize_local_path(str(workdir))
    if not norm or not Path(norm).is_dir():
        raise ValueError(f"worktree requires an existing directory (got: {workdir!r}).")
    rc, out_b, _ = await _run_git(norm, "rev-parse", "--show-toplevel", timeout=timeout)
    if rc != 0:
        raise ValueError(f"worktree requires a git repository directory (got: {workdir!r}).")
    root_str = out_b.decode("utf-8", errors="replace").strip()
    if not root_str:
        raise ValueError(f"Could not determine git repository root for: {workdir!r}.")
    return Path(store.normalize_local_path(root_str))


async def create_worktree(
    workdir: str | Path,
    name_or_id: str,
    *,
    base_ref: str = "HEAD",
    prefix: str = "a2a",
    timeout: float = 30.0,
) -> WorktreeInfo:
    """Create or reuse an isolated git worktree at `<repo_root>/.worktrees/<prefix>-<slug>`."""
    repo_root = await resolve_repo_root(workdir, timeout=min(10.0, timeout))
    slug = _sanitize_slug(name_or_id)
    clean_prefix = _sanitize_slug(prefix)
    wt_dir = repo_root / ".worktrees" / f"{clean_prefix}-{slug}"
    branch = f"{clean_prefix}/{slug}"
    wt_dir.parent.mkdir(parents=True, exist_ok=True)

    async with _repo_lock(repo_root):
        if wt_dir.is_dir() and (wt_dir / ".git").exists():
            rc_st, st_out, _ = await _run_git(wt_dir, "status", "--porcelain", timeout=min(10.0, timeout))
            rc_br, br_out, _ = await _run_git(wt_dir, "rev-parse", "--abbrev-ref", "HEAD", timeout=min(10.0, timeout))
            actual_branch = br_out.decode("utf-8", errors="replace").strip() if rc_br == 0 else branch
            if rc_st == 0 and not st_out.decode("utf-8", errors="replace").strip():
                return WorktreeInfo(
                    repo_root=str(repo_root),
                    worktree_path=str(wt_dir),
                    branch=actual_branch or branch,
                    created=False,
                )
            # Existing worktree is dirty (or detached): reset to clean base_ref before reusing for a new worker.
            rc_rs, _, err_rs = await _run_git(wt_dir, "reset", "--hard", base_ref, timeout=min(15.0, timeout))
            if rc_rs != 0:
                err_txt = err_rs.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"Failed to reset dirty worktree at {wt_dir}: {err_txt}")
            rc_cl, _, err_cl = await _run_git(wt_dir, "clean", "-fd", timeout=min(15.0, timeout))
            if rc_cl != 0:
                err_txt = err_cl.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"Failed to clean dirty worktree at {wt_dir}: {err_txt}")
            return WorktreeInfo(
                repo_root=str(repo_root),
                worktree_path=str(wt_dir),
                branch=actual_branch or branch,
                created=False,
            )

        await _run_git(repo_root, "worktree", "prune", timeout=min(15.0, timeout))
        rc, _, err_b = await _run_git(
            repo_root,
            "worktree",
            "add",
            "-B",
            branch,
            str(wt_dir),
            base_ref,
            timeout=timeout,
        )
        if rc != 0:
            err_text = err_b.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Failed to create git worktree at {wt_dir}: {err_text}")

    return WorktreeInfo(
        repo_root=str(repo_root),
        worktree_path=str(wt_dir),
        branch=branch,
        created=True,
    )


async def capture_worktree_summary(
    worktree_path: str | Path | None,
    *,
    timeout: float = 15.0,
) -> dict[str, str]:
    """Capture compact `git status --short` and `git diff --stat HEAD` summaries for a worktree."""
    if not worktree_path or not Path(worktree_path).is_dir():
        return {}
    try:
        (rc_s, out_s, _), (rc_d, out_d, _) = await asyncio.gather(
            _run_git(worktree_path, "status", "--short", timeout=timeout),
            _run_git(worktree_path, "diff", "--stat", "HEAD", timeout=timeout),
        )
        return {
            "status": out_s.decode("utf-8", errors="replace").strip() if rc_s == 0 else "",
            "diffStat": out_d.decode("utf-8", errors="replace").strip() if rc_d == 0 else "",
        }
    except Exception:
        return {}


async def inspect_git_diff(
    target_dir: str | Path | None,
    *,
    include_patch: bool = True,
    max_bytes: int = 200_000,
    branch_hint: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Inspect git status, diff stat, untracked files, and bounded patch in `target_dir`."""
    norm_dir = store.normalize_local_path(str(target_dir)) if target_dir else ""
    if not norm_dir or not Path(norm_dir).is_dir():
        return {
            "ok": False,
            "error": "No valid worktreePath or workdir for inspection.",
            "path": norm_dir or None,
            "branch": branch_hint,
            "status": "",
            "diffStat": "",
            "patch": "",
            "untracked": [],
            "truncated": False,
        }

    try:
        rc_br, out_br, err_br = await _run_git(norm_dir, "rev-parse", "--abbrev-ref", "HEAD", timeout=timeout)
        if rc_br != 0:
            rc_root, _, _ = await _run_git(norm_dir, "rev-parse", "--is-inside-work-tree", timeout=timeout)
            if rc_root != 0:
                return {
                    "ok": False,
                    "error": err_br.decode("utf-8", errors="replace").strip() or "Not a git repository.",
                    "path": norm_dir,
                    "branch": branch_hint,
                    "status": "",
                    "diffStat": "",
                    "patch": "",
                    "untracked": [],
                    "truncated": False,
                }
            has_head = False
            branch_name = branch_hint or "HEAD"
        else:
            has_head = True
            branch_name = out_br.decode("utf-8", errors="replace").strip() or branch_hint

        diff_ref = ("HEAD",) if has_head else ()
        coros = [
            _run_git(norm_dir, "status", "--short", "--branch", timeout=timeout),
            _run_git(norm_dir, "diff", "--stat", *diff_ref, timeout=timeout),
            _run_git(norm_dir, "ls-files", "--others", "--exclude-standard", timeout=timeout),
        ]
        if include_patch:
            coros.append(_run_git(norm_dir, "diff", *diff_ref, timeout=timeout))

        results = await asyncio.gather(*coros)
        status_txt = results[0][1].decode("utf-8", errors="replace").strip()
        diff_stat_txt = results[1][1].decode("utf-8", errors="replace").strip()
        untracked_list = [
            ln.strip() for ln in results[2][1].decode("utf-8", errors="replace").splitlines() if ln.strip()
        ]
        patch_txt = ""
        truncated = False
        if include_patch and len(results) > 3:
            raw_patch = results[3][1]
            limit = max(1024, int(max_bytes))
            if len(raw_patch) > limit:
                truncated = True
                raw_patch = raw_patch[:limit]
            patch_txt = raw_patch.decode("utf-8", errors="replace")

        return {
            "ok": True,
            "path": norm_dir,
            "branch": branch_name,
            "status": status_txt,
            "diffStat": diff_stat_txt,
            "patch": patch_txt,
            "untracked": untracked_list,
            "truncated": truncated,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "path": norm_dir,
            "branch": branch_hint,
            "status": "",
            "diffStat": "",
            "patch": "",
            "untracked": [],
            "truncated": False,
        }


def _rmtree_readonly_handler(func: Any, path: str, _exc_info: Any) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError:
        pass


def _rmtree_safe(path: Path) -> None:
    try:
        shutil.rmtree(path, onexc=_rmtree_readonly_handler)
    except TypeError:
        shutil.rmtree(path, onerror=_rmtree_readonly_handler)


async def _cleanup_leftover_worktree_dir(
    wt_path: Path,
    *,
    force_tree: bool = False,
    attempts: int = 8,
    base_delay: float = 0.08,
) -> bool:
    """Retry removing any leftover `.worktrees/<name>` directory after Windows file handles release."""
    if not wt_path.exists():
        return True
    for attempt in range(max(1, attempts)):
        if not wt_path.exists():
            return True
        try:
            if wt_path.is_dir():
                try:
                    os.rmdir(wt_path)
                except OSError:
                    if force_tree:
                        await asyncio.to_thread(_rmtree_safe, wt_path)
                    else:
                        raise
            else:
                wt_path.unlink(missing_ok=True)
            if not wt_path.exists():
                return True
        except OSError:
            pass
        if attempt + 1 < attempts:
            await asyncio.sleep(base_delay * (attempt + 1))
    return not wt_path.exists()


async def remove_worktree(
    workdir: str | Path,
    worktree_path: str | Path,
    *,
    branch: str | None = None,
    force: bool = True,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Remove a single git worktree and optionally delete its branch."""
    repo_root = await resolve_repo_root(workdir, timeout=min(10.0, timeout))
    wt_norm = store.normalize_local_path(str(worktree_path))
    await win_launcher.find_and_kill_orphan_worker_processes(
        swarm_id="",
        worker_name="",
        worktree_path=wt_norm,
    )
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(wt_norm)

    async with _repo_lock(repo_root):
        rc, _, err_b = await _run_git(repo_root, *args, timeout=timeout)
        if rc != 0:
            await asyncio.sleep(0.15)
            await _run_git(repo_root, "worktree", "prune", timeout=min(10.0, timeout))
            rc, _, err_b = await _run_git(repo_root, *args, timeout=timeout)
        if rc == 0:
            await _run_git(repo_root, "worktree", "prune", timeout=min(10.0, timeout))
        await _cleanup_leftover_worktree_dir(Path(wt_norm), force_tree=True)
        wt_parent = Path(wt_norm).parent
        if wt_parent.name == ".worktrees" and wt_parent.is_dir():
            await _cleanup_leftover_worktree_dir(wt_parent, force_tree=False, attempts=8)

        removed_branch = False
        if rc == 0 and branch:
            rc_br, _, _ = await _run_git(repo_root, "branch", "-D", branch, timeout=min(10.0, timeout))
            removed_branch = rc_br == 0

    return {
        "ok": rc == 0,
        "repoRoot": str(repo_root),
        "worktreePath": wt_norm,
        "branch": branch,
        "removedBranch": removed_branch,
        "error": err_b.decode("utf-8", errors="replace").strip() if rc != 0 else "",
    }


async def prune_worktrees(
    workdir: str | Path,
    *,
    active_ids: set[str] | None = None,
    prefix: str = "a2a",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Prune non-active `.worktrees/<prefix>-*` worktrees and orphaned `<prefix>/*` branches."""
    norm_wd = store.normalize_local_path(str(workdir))
    if not norm_wd or not Path(norm_wd).is_dir():
        raise ValueError(f"Directory does not exist: {workdir}")

    rc_root, out_root, _ = await _run_git(norm_wd, "rev-parse", "--show-toplevel", timeout=10.0)
    repo_root = store.normalize_local_path(out_root.decode("utf-8", errors="replace").strip()) if rc_root == 0 else norm_wd
    active = set(active_ids or ())
    clean_prefix = _sanitize_slug(prefix)
    wt_prefix = f"{clean_prefix}-"
    br_prefix = f"{clean_prefix}/"

    async with _repo_lock(repo_root):
        rc_wt, out_wt, _ = await _run_git(repo_root, "worktree", "list", "--porcelain", timeout=timeout)
        removed_wt: list[str] = []
        if rc_wt == 0:
            for line in out_wt.decode("utf-8", errors="replace").splitlines():
                if not line.startswith("worktree "):
                    continue
                wt_p = line.split("worktree ", 1)[1].strip()
                name = Path(wt_p).name
                if not name.startswith(wt_prefix):
                    continue
                ident = name[len(wt_prefix):]
                if ident not in active:
                    await win_launcher.find_and_kill_orphan_worker_processes(
                        swarm_id="",
                        worker_name="",
                        worktree_path=wt_p,
                    )
                    rc_rm, _, _ = await _run_git(repo_root, "worktree", "remove", "--force", wt_p, timeout=timeout)
                    if rc_rm != 0:
                        await asyncio.sleep(0.15)
                        rc_rm, _, _ = await _run_git(repo_root, "worktree", "remove", "--force", wt_p, timeout=timeout)
                    if rc_rm == 0:
                        removed_wt.append(wt_p)

        rc_prune, _, _ = await _run_git(repo_root, "worktree", "prune", timeout=timeout)

        removed_norm_set = {os.path.normcase(store.normalize_local_path(p)) for p in removed_wt}
        for wt_p in removed_wt:
            await _cleanup_leftover_worktree_dir(Path(store.normalize_local_path(wt_p)), force_tree=True)

        wt_parent = Path(repo_root) / ".worktrees"
        leftover_dirs: list[str] = []
        if wt_parent.is_dir():
            try:
                entries = list(wt_parent.iterdir())
            except OSError:
                entries = []
            for entry in entries:
                if not entry.is_dir() or not entry.name.startswith(wt_prefix):
                    continue
                ident = entry.name[len(wt_prefix):]
                if ident not in active:
                    await win_launcher.find_and_kill_orphan_worker_processes(
                        swarm_id="",
                        worker_name="",
                        worktree_path=str(entry),
                    )
                    norm_entry = os.path.normcase(store.normalize_local_path(str(entry)))
                    cleaned = await _cleanup_leftover_worktree_dir(entry, force_tree=(norm_entry in removed_norm_set))
                    if not cleaned and entry.exists():
                        leftover_dirs.append(str(entry))
            if wt_parent.is_dir():
                try:
                    remaining = list(wt_parent.iterdir())
                except OSError:
                    remaining = []
                if not remaining:
                    removed_parent = await _cleanup_leftover_worktree_dir(wt_parent, force_tree=False, attempts=8)
                    if not removed_parent and wt_parent.exists():
                        leftover_dirs.append(str(wt_parent))

        rc_refs, out_refs, _ = await _run_git(
            repo_root,
            "for-each-ref",
            "--format=%(refname:short)",
            f"refs/heads/{br_prefix}",
            timeout=timeout,
        )
        removed_branches: list[str] = []
        if rc_refs == 0:
            for ref in out_refs.decode("utf-8", errors="replace").splitlines():
                br = ref.strip()
                if not br.startswith(br_prefix):
                    continue
                ident = br[len(br_prefix):]
                if ident not in active:
                    rc_del, _, _ = await _run_git(repo_root, "branch", "-D", br, timeout=15.0)
                    if rc_del == 0:
                        removed_branches.append(br)

    return {
        "ok": True,
        "repoRoot": repo_root,
        "removed": removed_wt,
        "removedBranches": removed_branches,
        "leftoverDirs": leftover_dirs,
        "pruned": rc_prune == 0,
    }
