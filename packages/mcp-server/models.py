"""Canonical A2A v1.0 and Swarm v2 data models for the Hermes A2A bridge."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname
import uuid

SCHEMA_VERSION = 2
DEFAULT_CHANNEL = "#general"

_WSL_MNT_RE = re.compile(r"^[\\/]mnt[\\/]([A-Za-z])([\\/].*)?$")
_DRIVE_PATH_RE = re.compile(r"^([A-Za-z]):[\\/]?(.*)$")
_CHANNEL_CLEAN_RE = re.compile(r"[^a-z0-9_-]+")


class TaskState(str, Enum):
    """A2A v1.0 TaskState values."""

    UNSPECIFIED = "TASK_STATE_UNSPECIFIED"
    SUBMITTED = "TASK_STATE_SUBMITTED"
    WORKING = "TASK_STATE_WORKING"
    COMPLETED = "TASK_STATE_COMPLETED"
    FAILED = "TASK_STATE_FAILED"
    CANCELED = "TASK_STATE_CANCELED"
    INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
    REJECTED = "TASK_STATE_REJECTED"
    AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"


TERMINAL_STATES = frozenset({
    TaskState.COMPLETED.value,
    TaskState.FAILED.value,
    TaskState.CANCELED.value,
    TaskState.REJECTED.value,
})

INTERRUPTED_STATES = frozenset({
    TaskState.INPUT_REQUIRED.value,
    TaskState.AUTH_REQUIRED.value,
})

ACTIVE_STATES = frozenset({
    TaskState.WORKING.value,
    TaskState.SUBMITTED.value,
    TaskState.INPUT_REQUIRED.value,
    TaskState.AUTH_REQUIRED.value,
})


class Role(str, Enum):
    """Canonical 3-tier swarm participant roles."""

    PRIMARY = "primary"
    COORDINATOR = "coordinator"
    WORKER = "worker"
    VERIFIER = "verifier"
    RESEARCHER = "researcher"
    MONITOR = "monitor"
    NARRATOR = "narrator"
    SUBAGENT = "subagent"


PRIMARY_ROLE = Role.PRIMARY.value
COORDINATOR_ROLE = Role.COORDINATOR.value
SUBAGENT_ROLE = Role.SUBAGENT.value
WORKER_ROLES = frozenset({
    Role.WORKER.value,
    Role.VERIFIER.value,
    Role.RESEARCHER.value,
    Role.MONITOR.value,
    Role.NARRATOR.value,
})
ALL_ROLES = frozenset(r.value for r in Role)


class MemberState(str, Enum):
    """Lifecycle states for board roster members."""

    WORKING = "working"
    WAITING = "waiting"
    IDLE = "idle"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"
    KILLED = "killed"
    STOPPED = "stopped"
    INPUT_REQUIRED = "input_required"


class MessageType(str, Enum):
    """Canonical v2 swarm board message types."""

    POST = "post"
    QUESTION = "question"
    DECISION = "decision"
    HANDOFF = "handoff"
    ESCALATION = "escalation"
    DIGEST = "digest"
    ACK = "ack"
    SYSTEM = "system"


MessageKind = MessageType
MESSAGE_TYPES = frozenset(m.value for m in MessageType)

_MESSAGE_TYPE_ALIASES: dict[str, str] = {
    "post": MessageType.POST.value,
    "progress": MessageType.POST.value,
    "status": MessageType.POST.value,
    "result": MessageType.POST.value,
    "brief": MessageType.POST.value,
    "info": MessageType.POST.value,
    "question": MessageType.QUESTION.value,
    "ask": MessageType.QUESTION.value,
    "approval_request": MessageType.QUESTION.value,
    "decision": MessageType.DECISION.value,
    "directive": MessageType.DECISION.value,
    "plan": MessageType.DECISION.value,
    "handoff": MessageType.HANDOFF.value,
    "handoff_ready": MessageType.HANDOFF.value,
    "escalation": MessageType.ESCALATION.value,
    "blocker": MessageType.ESCALATION.value,
    "alert": MessageType.ESCALATION.value,
    "digest": MessageType.DIGEST.value,
    "ack": MessageType.ACK.value,
    "system": MessageType.SYSTEM.value,
}


def now_iso() -> str:
    """Return an ISO 8601 UTC timestamp with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str = "") -> str:
    """Generate a compact UUID-based identifier."""
    uid = uuid.uuid4().hex
    return f"{prefix}{uid}" if prefix else uid


def normalize_local_path(raw: str | None) -> str:
    """Convert file:// URIs, WSL /mnt/<drive>/... paths, and ~ into normalized local paths."""
    if not raw:
        return ""
    s = str(raw).strip()
    if not s:
        return ""

    is_uri_or_mnt = False
    if s.lower().startswith("file://"):
        is_uri_or_mnt = True
        parsed = urlparse(s)
        path_str = url2pathname(unquote(parsed.path))
        if parsed.netloc and parsed.netloc.lower() not in ("", "localhost"):
            path_str = f"\\\\{parsed.netloc}{path_str}"
        s = path_str

    m = _WSL_MNT_RE.match(s)
    if m:
        is_uri_or_mnt = True
        drive = m.group(1).upper()
        rest = (m.group(2) or "/").replace("/", "\\")
        if not rest.startswith("\\"):
            rest = "\\" + rest
        s = f"{drive}:{rest}"

    if s.startswith("~"):
        s = os.path.expanduser(s)
        is_uri_or_mnt = True

    if is_uri_or_mnt or os.path.isabs(s) or _DRIVE_PATH_RE.match(s):
        try:
            return str(Path(s).resolve())
        except OSError:
            return os.path.normpath(os.path.abspath(s))
    return os.path.normpath(s)


def to_wsl_path(raw: str | None) -> str:
    """Convert a Windows local path (or file:// URI) to a WSL /mnt/<drive>/... path."""
    if not raw:
        return ""
    norm = normalize_local_path(raw)
    m = _DRIVE_PATH_RE.match(norm)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/").lstrip("/")
        return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"
    return norm.replace("\\", "/")


def normalize_channel_name(raw: str | None) -> str:
    """Normalize a swarm channel name to a canonical `#slug` (defaults to `#general`, preserves `*`)."""
    text = str(raw or "").strip().lower()
    if text == "*":
        return "*"
    if text.startswith("@"):
        text = text[1:]
    if text.startswith("#"):
        text = text[1:]
    if not text or text == "main":
        return DEFAULT_CHANNEL
    slug = _CHANNEL_CLEAN_RE.sub("-", text).strip("-")[:48].strip("-")
    if not slug or slug == "main":
        return DEFAULT_CHANNEL
    return f"#{slug}"


_WORKER_ROLE_ALIASES: dict[str, str] = {
    "implementer": Role.WORKER.value,
    "impl": Role.WORKER.value,
}


def normalize_role(raw: str | None, default: str = Role.WORKER.value) -> str:
    """Normalize a participant role to a canonical Role value."""
    text = str(raw or "").strip().lower()
    if text in _WORKER_ROLE_ALIASES:
        return _WORKER_ROLE_ALIASES[text]
    if text in ALL_ROLES:
        return text
    return default


def validate_worker_role(raw: str | None) -> str:
    """Validate and return a canonical Tier-2 Hermes worker role."""
    if raw is None or not str(raw).strip():
        return Role.WORKER.value
    role = str(raw).strip().lower()
    role = _WORKER_ROLE_ALIASES.get(role, role)
    if role not in WORKER_ROLES:
        allowed = ", ".join(sorted(set(WORKER_ROLES) | {"implementer"}))
        raise ValueError(f"Unsupported worker role {raw!r}; expected one of: {allowed}")
    return role


def normalize_message_type(raw: str | None, default: str = MessageType.POST.value) -> str:
    """Normalize a board message type to a canonical MessageType value."""
    text = str(raw or "").strip().lower()
    if text in _MESSAGE_TYPE_ALIASES:
        return _MESSAGE_TYPE_ALIASES[text]
    return default


def text_part(text: str, media_type: str = "text/plain", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    part: dict[str, Any] = {"text": text, "mediaType": media_type}
    if metadata:
        part["metadata"] = metadata
    return part


def data_part(data: Any, media_type: str = "application/json", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    part: dict[str, Any] = {"data": data, "mediaType": media_type}
    if metadata:
        part["metadata"] = metadata
    return part


def file_url_part(
    url: str,
    filename: str | None = None,
    media_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    part: dict[str, Any] = {"url": url}
    if filename:
        part["filename"] = filename
    if media_type:
        part["mediaType"] = media_type
    if metadata:
        part["metadata"] = metadata
    return part


def new_message(
    role: str,
    parts: list[dict[str, Any]],
    *,
    context_id: str | None = None,
    task_id: str | None = None,
    message_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "messageId": message_id or new_id("msg-"),
        "role": role,
        "parts": parts,
    }
    if context_id:
        msg["contextId"] = context_id
    if task_id:
        msg["taskId"] = task_id
    if metadata:
        msg["metadata"] = metadata
    return msg


def new_artifact(
    name: str,
    parts: list[dict[str, Any]],
    *,
    description: str | None = None,
    artifact_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    art: dict[str, Any] = {
        "artifactId": artifact_id or new_id("art-"),
        "name": name,
        "parts": parts,
    }
    if description:
        art["description"] = description
    if metadata:
        art["metadata"] = metadata
    return art


def extract_text_from_parts(parts: list[dict[str, Any]] | None) -> str:
    """Flatten A2A Parts (text, data, url, raw) into readable prompt text."""
    chunks: list[str] = []
    for part in parts or []:
        if not isinstance(part, dict):
            chunks.append(str(part))
            continue
        if "text" in part and part["text"] is not None:
            chunks.append(str(part["text"]))
        elif "data" in part:
            chunks.append(json.dumps(part["data"], ensure_ascii=False, indent=2))
        elif "url" in part:
            name = part.get("filename") or "file"
            chunks.append(f"[Attached file {name}: {part['url']}]")
        elif "raw" in part:
            name = part.get("filename") or "inline-bytes"
            chunks.append(f"[Attached inline bytes {name} ({part.get('mediaType', 'application/octet-stream')})]")
    return "\n\n".join(c for c in chunks if c)


@dataclass
class TaskView:
    """Typed A2A v1.0 Task projection."""

    id: str
    context_id: str
    status: dict[str, Any]
    artifacts: list[dict[str, Any]] | None = None
    history: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        view: dict[str, Any] = {
            "id": self.id,
            "contextId": self.context_id,
            "status": dict(self.status),
        }
        if self.artifacts is not None:
            view["artifacts"] = list(self.artifacts)
        if self.history is not None:
            view["history"] = list(self.history)
        if self.metadata:
            view["metadata"] = dict(self.metadata)
        return view

    @classmethod
    def from_task(
        cls,
        task: dict[str, Any],
        *,
        history_length: int | None = None,
        include_artifacts: bool = True,
    ) -> "TaskView":
        hist_out: list[dict[str, Any]] | None = None
        raw_hist = list(task.get("history") or [])
        if history_length is None:
            if raw_hist:
                hist_out = raw_hist
        elif history_length > 0 and raw_hist:
            hist_out = raw_hist[-history_length:]
        return cls(
            id=str(task["id"]),
            context_id=str(task["contextId"]),
            status=dict(task["status"]),
            artifacts=list(task.get("artifacts") or []) if include_artifacts else None,
            history=hist_out,
            metadata=dict(task["metadata"]) if task.get("metadata") else None,
        )


def format_task_view(
    task: dict[str, Any],
    *,
    history_length: int | None = None,
    include_artifacts: bool = True,
) -> dict[str, Any]:
    """Return a clean A2A v1.0 Task projection obeying historyLength and includeArtifacts."""
    return TaskView.from_task(
        task,
        history_length=history_length,
        include_artifacts=include_artifacts,
    ).to_dict()


@dataclass
class WorkerConfig:
    """Dynamic control surface for a Tier-2 Hermes A2A worker invocation."""

    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    profile: str = "default"
    reasoning: str | None = None
    workdir: str | None = None
    worktree: bool = False
    toolsets: list[str] | str | None = None
    skills: list[str] | str | None = None
    max_turns: int | None = None
    run_budget_seconds: int | None = None
    auto_approve: bool = True
    accept_hooks: bool = True
    checkpoints: bool = False
    ignore_rules: bool = False
    ignore_user_config: bool = False
    safe_mode: bool = False
    no_restore_cwd: bool = False
    pass_session_id: bool = True
    role: str = Role.WORKER.value
    role_prompt: str | None = None
    worker_name: str | None = None
    channels: list[str] = field(default_factory=lambda: [DEFAULT_CHANNEL])
    parent_task_id: str | None = None
    parent_worker: str | None = None
    fork_from: str | None = None
    source: str = "tool"
    explicit_fields: set[str] = field(default_factory=set, repr=False, compare=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "WorkerConfig":
        if not raw:
            return cls()
        normalized = dict(raw)
        if "parent_task_id" not in normalized and normalized.get("parentTaskId") is not None:
            normalized["parent_task_id"] = normalized["parentTaskId"]
        if "parent_worker" not in normalized and normalized.get("parentWorker") is not None:
            normalized["parent_worker"] = normalized["parentWorker"]
        if "fork_from" not in normalized and normalized.get("forkFrom") is not None:
            normalized["fork_from"] = normalized["forkFrom"]
        if "auto_approve" not in normalized and normalized.get("autoApprove") is not None:
            normalized["auto_approve"] = normalized["autoApprove"]
        if "role_prompt" not in normalized and normalized.get("rolePrompt") is not None:
            normalized["role_prompt"] = normalized["rolePrompt"]
        if "worker_name" not in normalized and normalized.get("workerName") is not None:
            normalized["worker_name"] = normalized["workerName"]
        if normalized.get("workdir"):
            normalized["workdir"] = normalize_local_path(str(normalized["workdir"]))
        if "role" in normalized and normalized["role"] is not None:
            normalized["role"] = validate_worker_role(str(normalized["role"]))
        if "channels" in normalized and normalized["channels"] is not None:
            raw_ch = normalized["channels"]
            ch_list = [raw_ch] if isinstance(raw_ch, str) else list(raw_ch)
            deduped: list[str] = []
            for ch in ch_list:
                c = normalize_channel_name(str(ch))
                if c not in deduped:
                    deduped.append(c)
            normalized["channels"] = deduped or [DEFAULT_CHANNEL]

        known = {f.name for f in fields(cls) if f.name != "explicit_fields"}
        filtered = {k: v for k, v in normalized.items() if k in known and v is not None}
        return cls(**filtered, explicit_fields=set(filtered.keys()))

    def to_metadata(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "base_url": self.base_url,
            "profile": self.profile,
            "reasoning": self.reasoning,
            "workdir": self.workdir,
            "worktree": self.worktree,
            "toolsets": self.toolsets,
            "skills": self.skills,
            "max_turns": self.max_turns,
            "run_budget_seconds": self.run_budget_seconds,
            "auto_approve": self.auto_approve,
            "accept_hooks": self.accept_hooks,
            "checkpoints": self.checkpoints,
            "ignore_rules": self.ignore_rules,
            "ignore_user_config": self.ignore_user_config,
            "safe_mode": self.safe_mode,
            "role": self.role,
            "worker_name": self.worker_name,
            "channels": list(self.channels),
            "parent_task_id": self.parent_task_id,
            "parent_worker": self.parent_worker,
            "fork_from": self.fork_from,
        }

    def to_resume_overrides(self) -> dict[str, Any]:
        """Return only fields explicitly supplied (or differing from defaults) for turn-N resume."""
        defaults = WorkerConfig()
        meta = self.to_metadata()
        if self.explicit_fields:
            return {
                k: v
                for k, v in meta.items()
                if k in self.explicit_fields and v is not None
            }
        overrides: dict[str, Any] = {}
        for k, v in meta.items():
            if v is None:
                continue
            if hasattr(defaults, k) and getattr(defaults, k) != v:
                overrides[k] = v
        return overrides


def default_swarm_meta(
    swarm_id: str,
    *,
    title: str | None = None,
    goal: str = "",
    mission: str = "",
    workspace: str | None = None,
    primary_conversation_id: str | None = None,
    coordinator_conversation_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Build a canonical v2 meta.json document for a swarm."""
    ts = now_iso()
    norm_ws = normalize_local_path(workspace) if workspace else None
    resolved_title = (title or "").strip()
    if not resolved_title:
        if norm_ws:
            resolved_title = Path(norm_ws).name or swarm_id
        else:
            resolved_title = "Default Swarm" if swarm_id == "default" else swarm_id
    conv_ids: list[str] = []
    for cid in (primary_conversation_id, coordinator_conversation_id):
        clean = str(cid or "").strip()
        if clean and clean not in conv_ids:
            conv_ids.append(clean)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "swarmId": swarm_id,
        "title": resolved_title,
        "goal": str(goal or ""),
        "mission": str(mission or ""),
        "workspace": norm_ws,
        "primaryConversationId": str(primary_conversation_id).strip() if primary_conversation_id else None,
        "coordinatorConversationId": str(coordinator_conversation_id).strip() if coordinator_conversation_id else None,
        "conversationIds": conv_ids,
        "projectId": str(project_id).strip() if project_id else None,
        "createdAt": ts,
        "updatedAt": ts,
        "archived": False,
        "wakeSeq": 0,
    }


def new_channel_record(
    name: str,
    *,
    description: str = "",
    topic: str = "",
    subscribers: list[str] | None = None,
    archived: bool = False,
) -> dict[str, Any]:
    ts = now_iso()
    ch = normalize_channel_name(name)
    return {
        "name": ch,
        "description": description,
        "topic": topic or description,
        "archived": bool(archived),
        "subscribers": list(subscribers or []),
        "createdAt": ts,
        "updatedAt": ts,
    }


def new_board_member(
    name: str,
    *,
    role: str = Role.WORKER.value,
    tier: int = 2,
    runtime: str = "hermes",
    state: str = MemberState.WAITING.value,
    assignment: str = "",
    channels: list[str] | None = None,
    parent: str | None = None,
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
    last_read_seq: int = 0,
    last_post_seq: int = 0,
    killed: bool = False,
    subagents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ts = now_iso()
    deduped_channels: list[str] | None = None
    if channels is not None:
        norm_channels = ["*" if str(c).strip() == "*" else normalize_channel_name(c) for c in channels]
        deduped_channels = []
        for c in norm_channels:
            if c not in deduped_channels:
                deduped_channels.append(c)
        if not deduped_channels:
            deduped_channels = None
    return {
        "name": name,
        "role": normalize_role(role),
        "tier": int(tier),
        "runtime": runtime,
        "state": state,
        "assignment": assignment,
        "channels": deduped_channels,
        "parent": parent,
        "parentTaskId": parent_task_id,
        "taskId": task_id,
        "contextId": context_id,
        "sessionId": session_id,
        "conversationId": conversation_id,
        "model": model,
        "provider": provider,
        "workdir": normalize_local_path(workdir) if workdir else None,
        "worktreePath": normalize_local_path(worktree_path) if worktree_path else None,
        "worktreeBranch": worktree_branch,
        "waitSince": wait_since,
        "lastReadSeq": int(last_read_seq),
        "lastPostSeq": int(last_post_seq),
        "killed": bool(killed),
        "subagents": list(subagents or []),
        "lastHeartbeatAt": ts,
        "updatedAt": ts,
    }


def new_subagent_record(
    subagent_id: str,
    *,
    parent_worker: str,
    parent_task_id: str | None = None,
    label: str = "",
    state: str = MemberState.WORKING.value,
    model: str | None = None,
    summary: str = "",
) -> dict[str, Any]:
    """Build a read-only Tier-3 leaf subagent metadata record."""
    ts = now_iso()
    return {
        "id": subagent_id,
        "role": Role.SUBAGENT.value,
        "tier": 3,
        "parentWorker": parent_worker,
        "parentTaskId": parent_task_id,
        "label": label or subagent_id,
        "state": state,
        "model": model,
        "summary": summary,
        "updatedAt": ts,
    }


def new_scope_claim(
    scope: str,
    *,
    holder: str,
    task_id: str | None = None,
    worktree_path: str | None = None,
    notes: str = "",
    ttl_seconds: float | None = 3600.0,
    expires_at: str | None = None,
) -> dict[str, Any]:
    claimed_dt = datetime.now(timezone.utc)
    claimed_at = claimed_dt.isoformat()
    exp_iso = expires_at
    ttl_val: float | None = None
    if ttl_seconds is not None:
        try:
            ttl_f = float(ttl_seconds)
            if ttl_f > 0:
                ttl_val = ttl_f
                if exp_iso is None:
                    exp_iso = (claimed_dt + timedelta(seconds=ttl_f)).isoformat()
        except (TypeError, ValueError):
            pass
    return {
        "scope": scope,
        "holder": holder,
        "taskId": task_id,
        "worktreePath": normalize_local_path(worktree_path) if worktree_path else None,
        "notes": notes,
        "claimedAt": claimed_at,
        "ttlSeconds": ttl_val,
        "expiresAt": exp_iso,
    }


def new_plan_thread(
    thread_id: str,
    *,
    title: str,
    status: str = "active",
    holders: list[str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    return {
        "id": thread_id,
        "title": title,
        "status": status,
        "holders": list(holders or []),
        "notes": notes,
        "updatedAt": now_iso(),
    }


def new_plan_task(
    task_id: str,
    *,
    title: str,
    status: str = "pending",
    assignee: str | None = None,
    thread_id: str | None = None,
    files: list[str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": title,
        "status": status,
        "assignee": assignee,
        "threadId": thread_id,
        "files": list(files or []),
        "notes": notes,
        "updatedAt": now_iso(),
    }


def new_board_message(
    *,
    seq: int,
    sender: str,
    summary: str,
    channel: str = DEFAULT_CHANNEL,
    channels: list[str] | None = None,
    msg_type: str = MessageType.POST.value,
    kind: str | None = None,
    control: dict[str, Any] | None = None,
    sender_role: str = Role.WORKER.value,
    sender_tier: int = 2,
    mentions: list[str] | None = None,
    channel_mentions: list[str] | None = None,
    reply_to: int | str | None = None,
    thread_id: str | None = None,
    task_id: str | None = None,
    context_id: str | None = None,
    details: str = "",
    files: list[str] | None = None,
    refs: list[dict[str, Any]] | None = None,
    status: str = "open",
    msg_id: str | None = None,
) -> dict[str, Any]:
    ts = now_iso()
    primary_ch = normalize_channel_name(channel)
    all_channels = [normalize_channel_name(c) for c in (channels or [primary_ch])]
    if primary_ch not in all_channels:
        all_channels.insert(0, primary_ch)
    raw_mt = str(msg_type or "").strip().lower()
    norm_type = normalize_message_type(raw_mt)
    clean_kind = str(kind).strip().lower() if kind is not None and str(kind).strip() else ""
    if not clean_kind and raw_mt and raw_mt != norm_type:
        clean_kind = raw_mt
    msg: dict[str, Any] = {
        "seq": int(seq),
        "id": msg_id or new_id("bmsg-"),
        "threadId": thread_id,
        "replyTo": reply_to,
        "channel": primary_ch,
        "channels": all_channels,
        "type": norm_type,
        "sender": sender,
        "senderRole": normalize_role(sender_role),
        "senderTier": int(sender_tier),
        "mentions": list(mentions or []),
        "channelMentions": [normalize_channel_name(c) for c in (channel_mentions or [])],
        "taskId": task_id,
        "contextId": context_id,
        "summary": summary,
        "details": details,
        "files": list(files or []),
        "refs": list(refs or []),
        "status": status,
        "readBy": [sender] if sender else [],
        "createdAt": ts,
        "updatedAt": ts,
    }
    if clean_kind:
        msg["kind"] = clean_kind
    if isinstance(control, dict):
        msg["control"] = dict(control)
    return msg


def default_board_state(
    swarm_id: str = "default",
    *,
    title: str = "",
    goal: str = "",
    mission: str = "",
) -> dict[str, Any]:
    """Build a clean v2 board.json document."""
    ts = now_iso()
    resolved_title = title.strip() or ("Default Swarm" if swarm_id == "default" else swarm_id)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "swarmId": swarm_id,
        "title": resolved_title,
        "goal": goal,
        "mission": mission,
        "nextSeq": 1,
        "channels": {
            DEFAULT_CHANNEL: new_channel_record(
                DEFAULT_CHANNEL,
                description="Primary swarm coordination channel",
                topic="Primary swarm coordination channel",
            ),
        },
        "members": {
            "coordinator": new_board_member(
                "coordinator",
                role=Role.COORDINATOR.value,
                tier=2,
                runtime="antigravity",
                state=MemberState.WORKING.value,
                assignment="Tier-2 Swarm Coordinator",
                channels=[DEFAULT_CHANNEL],
            ),
        },
        "subagents": {},
        "claims": {},
        "plan": {
            "summary": "",
            "threads": [],
            "tasks": [],
            "updatedAt": ts,
        },
        "pendingMentions": {},
        "messages": [],
        "frames": [],
        "updatedAt": ts,
    }


def build_hermes_agent_card(
    catalog: dict[str, Any],
    *,
    profile: str = "default",
    http_url: str | None = None,
) -> dict[str, Any]:
    """Build a canonical A2A v1.0 AgentCard describing the local Hermes worker runtime."""
    default_model = catalog.get("default_model") or ""
    default_provider = catalog.get("default_provider") or ""
    toolsets = catalog.get("cli_toolsets", [])
    skills: list[dict[str, Any]] = [
        {
            "id": "coding-worker",
            "name": "Autonomous Coding & Shell Worker",
            "description": (
                "Executes scoped coding, debugging, file edits, git operations, and test runs inside a target "
                "workspace or isolated git worktree using Hermes tools."
            ),
            "tags": ["coding", "terminal", "file", "worktree", "tests"],
            "examples": [
                "Implement the requested API handler in src/routes/users.py and run pytest.",
                "Run in an isolated git worktree, fix failing unit tests, and summarize the diff.",
            ],
        },
        {
            "id": "research-and-web",
            "name": "Web & Codebase Research Worker",
            "description": "Searches codebases, reads docs, scrapes URLs, and returns cited findings.",
            "tags": ["research", "web", "grep", "analysis"],
            "examples": ["Investigate why module X fails on Windows and return exact file/line citations."],
        },
        {
            "id": "multi-turn-session",
            "name": "Stateful Context-Preserving Worker",
            "description": (
                "Resumes an existing A2A contextId (Hermes session_id) for follow-up fixes, "
                "code-review iterations, and INPUT_REQUIRED continuations."
            ),
            "tags": ["multi-turn", "resume", "context", "review"],
        },
    ]

    for skill_name in catalog.get("enabled_skills", [])[:20]:
        skills.append({
            "id": f"skill:{skill_name}",
            "name": skill_name,
            "description": f"Preload Hermes skill '{skill_name}' via config.skills.",
            "tags": ["hermes-skill", skill_name],
        })

    interfaces: list[dict[str, Any]] = [
        {
            "url": "mcp://hermes-a2a",
            "protocolBinding": "MCP-A2A-BRIDGE",
            "protocolVersion": "1.0",
        }
    ]
    if http_url:
        interfaces.insert(0, {
            "url": http_url.rstrip("/"),
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        })
        interfaces.append({
            "url": f"{http_url.rstrip('/')}/v1",
            "protocolBinding": "HTTP+JSON",
            "protocolVersion": "1.0",
        })

    return {
        "name": f"Hermes Worker ({profile})" if profile != "default" else "Hermes Dynamic A2A Worker",
        "description": (
            f"Dynamic Hermes Agent worker pool (default {default_model} via {default_provider}). "
            "Supports per-task model/provider/reasoning/profile/toolset/skill overrides, isolated git "
            "worktrees, multi-turn contextId session resume, hard cancellation, and parallel fan-out."
        ),
        "version": catalog.get("hermes_version") or "1.0.0",
        "provider": {
            "name": "Local Hermes Harness (Nous Research)",
            "url": "https://hermes-agent.nousresearch.com/docs/",
        },
        "supportedInterfaces": interfaces,
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": ["text/plain", "application/json", "image/png", "image/jpeg"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": skills,
        "metadata": {
            "profile": profile,
            "defaultModel": default_model,
            "defaultProvider": default_provider,
            "aliases": catalog.get("aliases", {}),
            "providers": catalog.get("providers", {}),
            "profiles": catalog.get("profiles", ["default"]),
            "toolsets": toolsets,
            "dynamicControls": [
                "model",
                "provider",
                "base_url",
                "profile",
                "reasoning",
                "workdir",
                "worktree",
                "toolsets",
                "skills",
                "max_turns",
                "run_budget_seconds",
                "ignore_rules",
                "ignore_user_config",
                "safe_mode",
                "auto_approve",
                "checkpoints",
                "role",
                "role_prompt",
                "channels",
                "parent_task_id",
                "parent_worker",
                "fork_from",
            ],
        },
    }
