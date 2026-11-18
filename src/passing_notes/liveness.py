"""Bounded, read-only host evidence for status. Nothing here rings or resumes a lane.

Host silence is never proof of a permission prompt. Only an explicit host waiting
reason supplies pending-approval; a quiet unresolved sub-agent without a child
process supplies suspected-stall. Unsupported or incomplete observations are unknown.
No transcript text, process command line, host path or session id leaves this module.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from passing_notes import protocol, wake

MAX_PROJECTS = 8192
MAX_AGENTS = 32
TAIL_BYTES = 32 * 1024
MAX_PROCESSES = 4096
PROBE_TIMEOUT = 3
PROCESS_TIMEOUT = 8
PROCESS_OUTPUT_BYTES = 512 * 1024
STALL_MINUTES = 30


@dataclass(frozen=True)
class Evidence:
    window_state: str = "unknown"
    activity: str = "unknown"
    transcript_age_minutes: int | None = None
    open_subagents: int | None = None
    child_processes: int | None = None
    reason: str = "unavailable"


def _plain(path: Path) -> bool:
    info = path.lstat()
    return not stat.S_ISLNK(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _entries(folder: Path, limit: int) -> list[Path]:
    if not _plain(folder):
        raise ValueError("linked directory")
    paths: list[Path] = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if len(paths) == limit:
                raise ValueError("entry limit")
            paths.append(Path(entry.path))
    return paths


def _unfinished(path: Path) -> bool | None:
    """Only the last complete conversational event; never retain text or tool inputs.

    An outstanding tool use is positive unfinished evidence. An explicit end_turn is
    completion evidence. Everything else is unknown, including a partial final line.
    """
    if not _plain(path):
        return None
    with path.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - TAIL_BYTES))
        tail = stream.read(TAIL_BYTES)
    if not tail.endswith(b"\n"):
        return None
    lines = tail.splitlines()
    if size > TAIL_BYTES:
        lines = lines[1:]
    for line in reversed(lines):
        event = json.loads(line)
        if not isinstance(event, dict) or event.get("type") not in {"assistant", "user"}:
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            return None
        if event["type"] == "assistant" and message.get("stop_reason") == "end_turn":
            return False
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "tool_use" for part in content
        ):
            return True
        return None
    return None


def _transcript(root: Path, session_id: str, cwd: str | None) -> Path:
    """Prefer the host's cwd; otherwise search a bounded set and refuse ambiguity.

    The conventional encoded directory is only a hint: a missing candidate falls
    back to lookup by session id. No cwd path is opened or emitted.
    """
    wake.check_session_id(session_id)
    if not _plain(root):
        raise ValueError("linked root")
    projects = root / "projects"
    if not _plain(projects):
        raise ValueError("linked projects directory")
    if cwd:
        project = projects / re.sub(r"[^a-zA-Z0-9]", "-", cwd)
        if project.exists():
            if not _plain(project):
                raise ValueError("linked project directory")
            candidate = project / f"{session_id}.jsonl"
            if candidate.exists():
                if not _plain(candidate) or not candidate.is_file():
                    raise ValueError("invalid session transcript")
                return candidate
    found: list[Path] = []
    for project in _entries(projects, MAX_PROJECTS):
        if not _plain(project) or not project.is_dir():
            continue
        candidate = project / f"{session_id}.jsonl"
        if candidate.is_file() and _plain(candidate):
            found.append(candidate)
    if len(found) != 1:
        raise ValueError("session transcript missing or ambiguous")
    return found[0]


def transcript_evidence(
    root: Path, session_id: str, now: datetime, cwd: str | None = None
) -> tuple[int, int | None]:
    """Direct cwd lookup or <=8192 entries; <=32 agents, <=32 KiB per agent tail."""
    transcript = _transcript(root, session_id, cwd)
    latest = transcript.stat().st_mtime
    session_folder = transcript.with_suffix("")
    if session_folder.exists() and not _plain(session_folder):
        raise ValueError("linked session directory")
    folder = session_folder / "subagents"
    unfinished: list[bool | None] = []
    if folder.exists():
        for agent in _entries(folder, MAX_AGENTS):
            if agent.suffix != ".jsonl":
                continue
            if not _plain(agent) or not agent.is_file():
                raise ValueError("invalid agent transcript")
            latest = max(latest, agent.stat().st_mtime)
            try:
                unfinished.append(_unfinished(agent))
            except (OSError, ValueError, RecursionError):
                unfinished.append(None)
    age = max(0, int((now - datetime.fromtimestamp(latest, UTC)).total_seconds() // 60))
    observed = sum(value is True for value in unfinished)
    count = observed if observed or None not in unfinished else None
    return age, count


def process_parents() -> dict[int, int] | None:
    """One bounded process snapshot: ids only, no paths or command lines."""
    if os.name != "nt":
        return None
    system_root = os.environ.get("SystemRoot")
    if not system_root or not Path(system_root).is_absolute():
        return None
    executable = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    try:
        result = subprocess.run(
            [
                str(executable),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$ErrorActionPreference='Stop'; Get-CimInstance Win32_Process | "
                    f"Select-Object -First {MAX_PROCESSES + 1} ProcessId,ParentProcessId | "
                    "ConvertTo-Json -Compress"
                ),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PROCESS_TIMEOUT,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode or len(result.stdout) > PROCESS_OUTPUT_BYTES:
            return None
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or len(rows) > MAX_PROCESSES:
            return None
        parents: dict[int, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                return None
            pid, parent = row.get("ProcessId"), row.get("ParentProcessId")
            if type(pid) is not int or type(parent) is not int:
                return None
            if pid < 0 or parent < 0 or pid in parents:
                return None
            parents[pid] = parent
        return parents
    except (OSError, subprocess.TimeoutExpired, ValueError, RecursionError):
        return None


def descendants(parents: Mapping[int, int], pid: int | None) -> int | None:
    if pid is None or pid not in parents:
        return None
    found = {pid}
    for _ in range(len(parents)):
        children = {child for child, parent in parents.items() if parent in found} - found
        if not children:
            if parents[pid] in found:
                return None  # the observed component loops back to its root
            return len(found) - 1
        found.update(children)
    return None


class Observer:
    """One host listing and process census per report, cached evidence per session."""

    def __init__(self, host: wake.Host | None = None, root: Path | None = None) -> None:
        self.host = host or wake.ClaudeHost(list_timeout_s=PROBE_TIMEOUT)
        self.root = root or Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
        self._sessions: dict[str, wake.Session] | None = None
        self._parents: dict[int, int] | None = None
        self._listed = False
        self._cache: dict[str, Evidence] = {}

    def observe(self, binding: wake.Binding, now: datetime) -> Evidence:
        if binding.host == wake.HOST_CODEX:
            return Evidence(reason="codex-resume-unverified")
        if binding.session_id in self._cache:
            return self._cache[binding.session_id]
        if not self._listed:
            self._listed = True
            try:
                self._sessions = {row.session_id: row for row in self.host.list_sessions()}
            except wake.EVIDENCE_ERRORS:
                pass
            if self._sessions and any(row.pid is not None for row in self._sessions.values()):
                self._parents = process_parents()
        evidence = self._observe(binding.session_id, now)
        self._cache[binding.session_id] = evidence
        return evidence

    def _observe(self, session_id: str, now: datetime) -> Evidence:
        if self._sessions is None:
            return Evidence(reason="host-unavailable")
        session = self._sessions.get(session_id)
        if session is None:
            return Evidence("not-running", reason="session-not-listed")
        window = {"idle": "wakeable", "busy": "busy", "waiting": "waiting"}.get(
            session.status, "unknown"
        )
        approval = session.status == "waiting" and (session.waiting_for or "").casefold() in {
            "permission prompt",
            "permission",
            "approval",
            "permission_request",
            "permission_prompt",
        }
        age: int | None = None
        agents: int | None = None
        try:
            age, agents = transcript_evidence(self.root, session_id, now, session.cwd)
        except (OSError, ValueError, RecursionError, protocol.ProtocolError):
            pass
        children = None if self._parents is None else descendants(self._parents, session.pid)
        activity, reason = "unknown", "activity-unavailable"
        if approval:
            activity, reason = "pending-approval", "host-permission-wait"
        elif children is not None and children > 0:
            activity, reason = "running", "child-process-observed"
        elif age is not None and age < STALL_MINUTES:
            activity, reason = "recent", "transcript-write-observed"
        elif (
            age is not None
            and agents is not None
            and agents > 0
            and children == 0
            and session.status in {"busy", "waiting"}
        ):
            activity, reason = "suspected-stall", "quiet-open-agent-no-child"
        elif session.status == "idle":
            activity, reason = "idle", "host-idle"
        return Evidence(window, activity, age, agents, children, reason)
