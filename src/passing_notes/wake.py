"""Wake a bound Claude lane after a note is posted: a content-free doorbell.

Passing Notes stays the authority. The doorbell carries only the note id; the woken
lane reads the note itself and applies its own instructions and permissions.

How the doorbell travels. Claude Code delivers cross-session messages between local
sessions (its ``SendMessage`` tool); no CLI verb or file write does. So the sender runs
one short headless ``claude -p`` *bridge* whose only tools are ``ListAgents`` and
``SendMessage``, with no MCP servers, and asks it to send the fixed doorbell text to the
bound session. The bridge runs in the permission mode the lane declared when it bound:
Claude Code holds a message from a session in a different mode for its user's approval,
so a matching mode is the documented condition for direct delivery, never an
escalation (the bridge can do nothing but send that one message).

Binding. A lane binds itself explicitly: project, lane, host ``claude``, its exact
session id (``CLAUDE_CODE_SESSION_ID`` in that window) and its permission mode. A window
is never chosen by title. At send time the session id is looked up in
``claude agents --json``. Its display name is used only as the delivery address, and
only when exactly one live session carries that name.

The four states are reported separately, each only from evidence:

* posted: the note file exists in the store;
* delivered: the bridge's ``SendMessage`` returned success with a message id, which
  means "in that session's inbox", not "read";
* acknowledged: the ack file ``<note id>--<lane>.md`` exists;
* replied: a note from that lane with ``reply_to`` set to the note id exists.

Nothing claims that work started; a reply is the strongest evidence offered.

Records live in ``<root>/wake/<project>/``, beside the protocol folders and outside
protocol v1: ``<lane>.binding.json`` and ``rings/<note id>--<lane>--<session>.json``.
A ring record makes a repeated send for the same note, lane and session a no-op once
delivered; an exclusive ``.lock`` file stops two senders ringing at once, and a sender
reads the record again once it holds that file, so a sender that read it before another
finished never rings a second time. ``rung`` answers, without writing, whether a note's
doorbell is recorded as handed over or the note is acknowledged.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from passing_notes import protocol
from passing_notes.queue import CARD_STATE_DONE
from passing_notes.store import (
    CardNotFoundError,
    CodedError,
    NoteNotFoundError,
    Store,
    StoreError,
    os_reason,
    temp_path,
)

WAKE_DIR: Final = "wake"
RINGS_DIR: Final = "rings"
BINDING_SUFFIX: Final = ".binding.json"
HOST_CLAUDE: Final = "claude"
HOST_CODEX: Final = "codex"
CODEX_QUEUE_MODE: Final = "queue"
PERMISSION_MODES: Final = (
    "default",
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "dontAsk",
    "manual",
    "plan",
)
SESSION_ID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
BRIDGE_TOOLS: Final = "ListAgents,SendMessage"
BRIDGE_MODEL: Final = "haiku"
BRIDGE_MAX_TURNS: Final = 4
BRIDGE_TIMEOUT_S: Final = 180
LIST_TIMEOUT_S: Final = 60
LOCK_STALE_S: Final = 600
RECORD_MAX_BYTES: Final = 8192
ACK_READ_MAX_BYTES: Final = 4096
# Host-supplied text (a session name, status or waitingFor) kept in a refused-busy record
# or shown by card show --live, in characters. json.dumps escapes a non-ASCII character
# to at most 12 bytes, so three such fields and the detail stay far inside
# RECORD_MAX_BYTES and the record stays readable.
HOST_TEXT_MAX_CHARS: Final = 64
REFUSAL_DETAIL_MAX_CHARS: Final = 256
STATUS_IDLE: Final = "idle"
STATUS_BUSY: Final = "busy"
STATUS_WAITING: Final = "waiting"
# Configuration, not session identity: kept when the caller's session markers are dropped.
CLAUDE_CONFIG_ENV: Final = "CLAUDE_CONFIG_DIR"
KEEP_CLAUDE_VARS: Final = frozenset({CLAUDE_CONFIG_ENV})

# Ring record states that mean the doorbell was handed over: delivered to a Claude
# session's inbox, or queued for a Codex thread. ``failed`` (and Codex ``attempting`` or
# ``unknown``) are not; ``rung`` and every send read these, one source of truth.
RING_DELIVERED: Final = "delivered"
RING_QUEUED: Final = "queued"
RING_FAILED: Final = "failed"
# D44: a Claude ring refused because the session was not idle. Evidence only: it is not
# in RUNG_STATES, so a refused note still counts as never rung, and a later delivered
# ring for the same session overwrites it.
RING_REFUSED_BUSY: Final = "refused-busy"
RUNG_STATES: Final = (RING_DELIVERED, RING_QUEUED)
RING_RECORD_SUFFIX: Final = ".json"

# Stable error codes. Exit 5 means the note (if any) is in the store but the doorbell
# was not delivered.
WAKE_NO_BINDING: Final = "wake-no-binding"
WAKE_UNSUPPORTED_HOST: Final = "wake-unsupported-host"
WAKE_SESSION_UNAVAILABLE: Final = "wake-session-unavailable"
WAKE_SESSION_BUSY: Final = "wake-session-busy"
WAKE_SESSION_AMBIGUOUS: Final = "wake-session-ambiguous"
WAKE_IN_PROGRESS: Final = "wake-in-progress"
WAKE_BRIDGE_FAILED: Final = "wake-bridge-failed"
WAKE_HOST_UNAVAILABLE: Final = "wake-host-unavailable"
WAKE_NOT_A_LANE: Final = "wake-not-a-lane"
WAKE_QUEUE_UNKNOWN: Final = "wake-queue-unknown"
WAKE_NOT_NEEDED: Final = "wake-not-needed"
# A doorbell costs the recipient a full turn, so it rings only for a note that unblocks.
UNBLOCKING_NEEDS: Final = ("reply", "operator-decision")
# Which notes may ring, in words: the tail of every wake-not-needed message.
RING_RULE: Final = (
    "Ring only for a reply, an operator decision, an operator relay, a handover, or the "
    "answer to a note that needed one"
)


def doorbell_text(note_id: str) -> str:
    """The exact message the woken lane receives: the note id, never its content."""
    return (
        f"New Passing Note {note_id}. Read it and respond through Passing Notes. "
        "Acknowledging this doorbell is not the deliverable: a finished item always "
        "posts its own handoff note. If you already acknowledged this note or your "
        "card is done, stop: post nothing more about it and ring no one."
    )


class WakeError(CodedError):
    """The doorbell was not delivered. Any posted note stays in the store."""


@dataclass(frozen=True)
class Session:
    """One row of ``claude agents --json``."""

    session_id: str
    name: str | None
    status: str
    kind: str
    pid: int | None
    cwd: str | None = None
    # The row's ``waitingFor`` on a ``waiting`` session, e.g. ``permission prompt``.
    waiting_for: str | None = None


@dataclass(frozen=True)
class SendResult:
    """What the bridge's own tool calls show, never what its model says."""

    delivered: bool
    msg_id: str | None
    detail: str


class Host(Protocol):
    """The Claude Code operations the doorbell needs; tests supply a fake."""

    def list_sessions(self) -> list[Session]: ...

    def send(self, address: str, text: str, permission_mode: str) -> SendResult: ...


class CodexQueue(Protocol):
    def queue(self, thread_id: str, message: str) -> str: ...


class CodexHost:
    """Queue a doorbell through the supported Codex CLI, never a terminal window."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("codex")

    def queue(self, thread_id: str, message: str) -> str:
        if not self.executable:
            raise WakeError(WAKE_HOST_UNAVAILABLE, "codex is not on PATH")
        try:
            done = subprocess.run(
                [self.executable, "queue", "--thread", thread_id, "--message", message, "--resume"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=BRIDGE_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise WakeError(WAKE_BRIDGE_FAILED, f"codex queue: {exc}") from None
        except subprocess.TimeoutExpired:
            # The message may already be queued: report unknown, never retry blindly.
            raise WakeError(
                WAKE_QUEUE_UNKNOWN,
                f"codex queue timed out after {BRIDGE_TIMEOUT_S}s; queueing is unknown",
            ) from None
        if done.returncode != 0:
            first = (done.stderr or done.stdout).strip().splitlines()[:1]
            raise WakeError(
                WAKE_BRIDGE_FAILED,
                f"codex queue exited {done.returncode}: {first[0] if first else 'no detail'}",
            )
        receipt = re.search(
            rf"Queued message ([0-9a-f-]{{36}}) for thread {re.escape(thread_id)}\.",
            done.stdout,
        )
        if receipt is None:
            raise WakeError(
                WAKE_QUEUE_UNKNOWN,
                "codex queue exited 0 without a matching queue receipt; delivery is unknown",
            )
        # A resume warning does not undo queue acceptance or prove delivery failed.
        warning = next((line for line in done.stderr.splitlines() if "Warning:" in line), "")
        detail = f"queued message {receipt[1]}"
        return detail + (f"; {warning[:500]}" if warning else "; resume requested")


@dataclass(frozen=True)
class Binding:
    project: str
    lane: str
    host: str
    session_id: str
    permission_mode: str
    bound_utc: str


@dataclass(frozen=True)
class Outcome:
    """Everything ``wake send`` reports. ``delivered`` is None when no send ran."""

    note_id: str
    lane: str
    session: Session | None
    delivered: bool | None
    msg_id: str | None
    wake_line: str
    ack_utc: str | None
    reply_id: str | None
    waited_s: float


def utc_stamp(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_session_id(value: str) -> str:
    if not SESSION_ID_RE.fullmatch(value):
        raise protocol.ProtocolError(
            f"--session: {protocol.show(value)} is not a Claude session id "
            "(lower-case UUID, as in CLAUDE_CODE_SESSION_ID)"
        )
    return value


def check_permission_mode(value: str) -> str:
    if value not in PERMISSION_MODES:
        raise protocol.ProtocolError(
            f"--permission-mode: {protocol.show(value)} is not one of {', '.join(PERMISSION_MODES)}"
        )
    return value


# --- Real host ----------------------------------------------------------------------


def parse_sessions(text: str) -> list[Session]:
    """Rows of ``claude agents --json``. Rows without a session id are skipped."""
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("claude agents --json did not print a list")  # noqa: TRY004
    sessions: list[Session] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        sid = row.get("sessionId")
        if not isinstance(sid, str):
            continue
        name = row.get("name")
        pid = row.get("pid")
        cwd = row.get("cwd")
        waiting_for = row.get("waitingFor")
        sessions.append(
            Session(
                session_id=sid,
                name=name if isinstance(name, str) and name else None,
                status=str(row.get("status", "")),
                kind=str(row.get("kind", "")),
                pid=pid if isinstance(pid, int) else None,
                cwd=cwd if isinstance(cwd, str) and cwd else None,
                waiting_for=waiting_for if isinstance(waiting_for, str) and waiting_for else None,
            )
        )
    return sessions


def bridge_prompt(address: str, text: str) -> str:
    return (
        "You deliver one fixed message and nothing else.\n"
        "1. Call ListAgents once.\n"
        f"2. If exactly one peer row is named {json.dumps(address)}, call SendMessage once "
        f"with to set to {json.dumps(address)} and message set to exactly this text, "
        f"unchanged: {json.dumps(text)}\n"
        "3. Otherwise do not call SendMessage.\n"
        "Then answer with one word: SENT or NOT-SENT."
    )


def _listed_rows(listing: str, address: str) -> int:
    """How many ListAgents rows carry exactly ``address`` as their name."""
    prefix = f"{address} ["
    return sum(1 for line in listing.splitlines() if line.strip().startswith(prefix))


def parse_bridge_stream(lines: Sequence[str], address: str, text: str) -> SendResult:
    """Judge the bridge from its stream-json tool calls and results alone."""
    uses: dict[str, tuple[str, dict[str, object]]] = {}
    listing: str | None = None
    sends: list[tuple[dict[str, object], str]] = []
    for raw in lines:
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") not in ("assistant", "user"):
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                inputs = block.get("input")
                uses[str(block.get("id"))] = (
                    str(block.get("name")),
                    inputs if isinstance(inputs, dict) else {},
                )
            elif block.get("type") == "tool_result":
                used = uses.get(str(block.get("tool_use_id")))
                if used is None:
                    continue
                result = _result_text(block.get("content"))
                if used[0] == "ListAgents":
                    listing = result
                elif used[0] == "SendMessage":
                    sends.append((used[1], result))
    if listing is not None and _listed_rows(listing, address) != 1:
        if sends:
            return SendResult(False, None, "bridge sent although the address was not unique")
        return SendResult(False, None, f"session name {address!r} is not unique among peers")
    if not sends:
        return SendResult(False, None, "bridge made no SendMessage call")
    if len(sends) > 1:
        return SendResult(False, None, "bridge made more than one SendMessage call")
    inputs, result = sends[0]
    if inputs.get("to") != address or inputs.get("message") != text:
        return SendResult(False, None, "bridge sent a different address or text")
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return SendResult(False, None, f"SendMessage result was not JSON: {result[:200]}")
    if isinstance(payload, dict) and payload.get("success") is True:
        msg_id = payload.get("msg_id")
        return SendResult(True, msg_id if isinstance(msg_id, str) else None, "delivered")
    return SendResult(False, None, f"SendMessage did not succeed: {result[:200]}")


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def host_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for ``claude`` child processes, minus the caller's own session.

    A process started from inside a Claude session inherits its ``CLAUDE_*`` markers
    and becomes that session's child: it sees the caller's background sessions and is
    not a messaging peer. The doorbell must act as an independent sender, the same
    from a Claude lane, a Codex lane or a terminal.
    """
    env = dict(os.environ if environ is None else environ)
    return {
        k: v
        for k, v in env.items()
        if not k.upper().startswith("CLAUDE") or k.upper() in KEEP_CLAUDE_VARS
    }


class ClaudeHost:
    """Claude Code on this machine, driven through its CLI with argument lists."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("claude")

    def _exe(self) -> str:
        if not self.executable:
            raise WakeError(WAKE_HOST_UNAVAILABLE, "claude is not on PATH")
        return self.executable

    def list_sessions(self) -> list[Session]:
        try:
            done = subprocess.run(
                [self._exe(), "agents", "--json"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=LIST_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
                env=host_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WakeError(WAKE_HOST_UNAVAILABLE, f"claude agents --json: {exc}") from None
        if done.returncode != 0:
            first = (done.stderr or done.stdout).strip().splitlines()[:1]
            raise WakeError(
                WAKE_HOST_UNAVAILABLE,
                f"claude agents --json exited {done.returncode}: {first[0] if first else ''}",
            )
        try:
            return parse_sessions(done.stdout)
        except ValueError as exc:
            raise WakeError(WAKE_HOST_UNAVAILABLE, f"claude agents --json: {exc}") from None

    def send(self, address: str, text: str, permission_mode: str) -> SendResult:
        # An empty folder outside every repository: no project settings, hooks or
        # CLAUDE.md reach the bridge, and its session is named for what it does.
        with tempfile.TemporaryDirectory(prefix="passing-notes-wake-") as work:
            argv = [
                self._exe(),
                "-p",
                bridge_prompt(address, text),
                "--tools",
                BRIDGE_TOOLS,
                "--allowedTools",
                BRIDGE_TOOLS,
                "--strict-mcp-config",
                "--setting-sources",
                "project",
                "--permission-mode",
                permission_mode,
                "--model",
                BRIDGE_MODEL,
                "--max-turns",
                str(BRIDGE_MAX_TURNS),
                "--name",
                "passing-notes doorbell",
                "--output-format",
                "stream-json",
                "--verbose",
            ]
            try:
                done = subprocess.run(
                    argv,
                    check=False,
                    cwd=work,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=BRIDGE_TIMEOUT_S,
                    stdin=subprocess.DEVNULL,
                    env=host_env(),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise WakeError(WAKE_BRIDGE_FAILED, f"claude -p bridge: {exc}") from None
        result = parse_bridge_stream(done.stdout.splitlines(), address, text)
        if not result.delivered and done.returncode != 0:
            first = done.stderr.strip().splitlines()[:1]
            detail = f"{result.detail}; claude exited {done.returncode}"
            if first:
                detail += f": {first[0]}"
            return SendResult(False, None, detail)
        return result


# --- Records ------------------------------------------------------------------------


def _wake_folder(store: Store, project: str, *more: str, create: bool = False) -> Path:
    protocol.check_project_id(project, "--project")
    folder = store._store_folder(WAKE_DIR, project, *more)  # refuses links at every level
    if create:
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            raise StoreError(
                "store-unusable", f"cannot create {folder}: {os_reason(exc)}"
            ) from None
        folder = store._store_folder(WAKE_DIR, project, *more)
    return folder


def _write_json(final: Path, data: Mapping[str, object]) -> None:
    """Replace ``final`` in one step through an exclusive temporary file."""
    tmp = temp_path(final)
    body = (json.dumps(dict(data), indent=1, sort_keys=True) + "\n").encode("utf-8")
    try:
        with open(tmp, "xb") as handle:
            handle.write(body)
        os.replace(tmp, final)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise StoreError("store-unusable", f"cannot write {final}: {os_reason(exc)}") from None


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(RECORD_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StoreError("store-unusable", f"cannot read {path}: {os_reason(exc)}") from None
    if len(raw) > RECORD_MAX_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# Evidence and status views fail open (D44, D45): an error while writing a refusal record,
# or while reading one binding, card or ring record for card show --live, degrades that
# one item. It never changes the primary outcome (the busy refusal) or aborts a listing.
EVIDENCE_ERRORS: Final = (CodedError, protocol.ProtocolError, OSError)


def _evidence_code(exc: BaseException) -> str:
    """The stable code an evidence error shows as, ``unknown (<code>)``."""
    return exc.code if isinstance(exc, CodedError) else "store-unusable"


def _cap(text: str, limit: int = HOST_TEXT_MAX_CHARS) -> str:
    """Host text bounded to ``limit`` characters for a record or the live view."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _ring_entries(folder: Path, prefix: str = "") -> list[str]:
    """The ring record file names in ``folder`` that start with ``prefix``, sorted. A
    missing folder holds none; any other listing failure is a StoreError."""
    try:
        entries = sorted(os.listdir(folder))
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise StoreError("store-unusable", f"cannot list {folder}: {os_reason(exc)}") from None
    return [e for e in entries if e.startswith(prefix) and e.endswith(RING_RECORD_SUFFIX)]


def binding_path(store: Store, project: str, lane: str) -> Path:
    protocol.check_lane_id(lane, "--lane")
    return _wake_folder(store, project) / f"{lane}{BINDING_SUFFIX}"


def save_binding(store: Store, binding: Binding) -> None:
    _wake_folder(store, binding.project, create=True)
    _write_json(
        binding_path(store, binding.project, binding.lane),
        {
            "project": binding.project,
            "lane": binding.lane,
            "host": binding.host,
            "session_id": binding.session_id,
            "permission_mode": binding.permission_mode,
            "bound_utc": binding.bound_utc,
        },
    )


def load_binding(store: Store, project: str, lane: str) -> Binding:
    path = binding_path(store, project, lane)
    data = _read_json(path)
    if data is None:
        raise WakeError(
            WAKE_NO_BINDING,
            f"lane {lane} in {project} has no valid wake binding; the lane runs "
            "'passing-notes wake bind' from its own window",
        )
    try:
        binding = Binding(
            project=protocol.check_project_id(str(data["project"])),
            lane=protocol.check_lane_id(str(data["lane"])),
            host=str(data["host"]),
            session_id=check_session_id(str(data["session_id"])),
            permission_mode=str(data["permission_mode"]),
            bound_utc=str(data["bound_utc"]),
        )
    except (KeyError, protocol.ProtocolError) as exc:
        raise WakeError(WAKE_NO_BINDING, f"wake binding {path.name} is invalid: {exc}") from None
    if (binding.project, binding.lane) != (project, lane):
        raise WakeError(WAKE_NO_BINDING, f"wake binding {path.name} names another lane")
    if binding.host == HOST_CLAUDE:
        check_permission_mode(binding.permission_mode)
    elif binding.host == HOST_CODEX:
        if binding.permission_mode != CODEX_QUEUE_MODE:
            raise WakeError(WAKE_NO_BINDING, f"wake binding {path.name} has invalid Codex mode")
    else:
        raise WakeError(WAKE_UNSUPPORTED_HOST, f"wake binding {path.name} has host {binding.host}")
    return binding


def list_bindings(store: Store, project: str) -> list[Binding]:
    folder = _wake_folder(store, project)
    if not folder.is_dir():
        return []
    found: list[Binding] = []
    for entry in sorted(os.listdir(folder)):
        if not entry.endswith(BINDING_SUFFIX):
            continue
        lane = entry[: -len(BINDING_SUFFIX)]
        try:
            found.append(load_binding(store, project, lane))
        except (WakeError, protocol.ProtocolError):
            continue
    return found


def ring_prefix(note_id: str, lane: str) -> str:
    """The file-name prefix every session's ring record of ``note_id`` for ``lane``
    shares: the one owner of the ring file-name shape ``<note id>--<lane>--<session id>``
    (ring_paths, rung, latest_rings). Lane ids hold no "--", so the prefix is exact."""
    return f"{note_id}--{lane}--"


def _ring_name_lane(entry: str) -> str | None:
    """The lane a ring record file name (ring_prefix, a session id, RING_RECORD_SUFFIX)
    carries; None for a name of another shape, which belongs to no lane."""
    parts = entry.removesuffix(RING_RECORD_SUFFIX).split("--")
    return parts[1] if len(parts) == 3 else None


def ring_paths(
    store: Store, project: str, note_id: str, lane: str, session_id: str
) -> tuple[Path, Path]:
    folder = _wake_folder(store, project, RINGS_DIR, create=True)
    stem = ring_prefix(note_id, lane) + session_id
    return folder / f"{stem}{RING_RECORD_SUFFIX}", folder / f"{stem}.lock"


def rung(store: Store, project: str, note_id: str, lane: str) -> bool:
    """Whether ``lane`` already has ``note_id``'s doorbell, by the evidence on disk.

    True when the lane acknowledged the note (``<note id>--<lane>.md``), or a ring record
    of that note and lane, for any session it was bound to, says the doorbell was
    delivered (Claude: the bridge's ``SendMessage`` succeeded, so it is in the
    session's inbox, not necessarily read) or queued (Codex: accepted, delivery
    unverified). A stored note with no such record was never rung: no binding, a busy
    or missing session, a failed bridge, a Codex attempt of unknown outcome, or a crash
    before the send. Read-only: creates no folder and lists no session.
    """
    protocol.check_project_id(project, "--project")
    protocol.check_lane_id(lane, "--lane")
    protocol.check_note_id(note_id, "ID")
    if observed_ack(store, project, note_id, lane) is not None:
        return True
    folder = _wake_folder(store, project, RINGS_DIR)
    for entry in _ring_entries(folder, ring_prefix(note_id, lane)):
        record = _read_json(folder / entry)
        if (
            record is not None
            and record.get("note_id") == note_id
            and record.get("lane") == lane
            and record.get("state") in RUNG_STATES
        ):
            return True
    return False


# --- Evidence -----------------------------------------------------------------------


def observed_ack(store: Store, project: str, note_id: str, lane: str) -> str | None:
    """The ack's ``read_utc`` when ``<note id>--<lane>.md`` exists and parses."""
    path = store.ack_path(project, note_id, lane)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(ACK_READ_MAX_BYTES)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        ack = protocol.parse_ack(raw.decode("utf-8"), file_reader=lane)
    except (UnicodeDecodeError, protocol.ProtocolError):
        return "present (unparsed)"
    return ack.read_utc


def observed_reply(store: Store, project: str, note_id: str, lane: str) -> str | None:
    """The id of the first note from ``lane`` whose ``reply_to`` is ``note_id``."""
    notes, _ = store.scan_notes(project)
    replies = sorted(
        n.id
        for n in notes
        if n.reply_to == note_id and n.from_lane == lane and n.from_project in (None, project)
    )
    return replies[0] if replies else None


# --- Operations ---------------------------------------------------------------------


def _settings_mode(path: Path) -> str | None:
    """``permissions.defaultMode`` from one Claude settings file, when it names a mode."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(1024 * 1024)
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    permissions = data.get("permissions") if isinstance(data, dict) else None
    mode = permissions.get("defaultMode") if isinstance(permissions, dict) else None
    return mode if isinstance(mode, str) and mode in PERMISSION_MODES else None


def user_settings_path(environ: Mapping[str, str] | None = None) -> Path:
    """The user's Claude ``settings.json``: in ``CLAUDE_CONFIG_DIR`` when set, else in
    ``~/.claude``. The one resolver: ``configured_permission_mode`` reads it and
    ``hook install`` writes it."""
    env = os.environ if environ is None else environ
    config_dir = env.get(CLAUDE_CONFIG_ENV)
    user_dir = Path(config_dir) if config_dir else Path.home() / ".claude"
    return user_dir / "settings.json"


def configured_permission_mode(
    project_dir: Path | None, environ: Mapping[str, str] | None = None
) -> tuple[str, str]:
    """The permission mode a Claude window starts in when launched with no mode flag.

    Claude Code's settings precedence: managed settings, then the project's
    ``.claude/settings.local.json`` and ``.claude/settings.json``, then the user's
    ``settings.json`` (``CLAUDE_CONFIG_DIR`` or ``~/.claude``). The first file naming a
    ``permissions.defaultMode`` wins; with none, Claude starts in ``default``. A model
    cannot reliably report its own mode (a live test bound ``default`` for a
    ``bypassPermissions`` window), so the CLI reads the configuration instead.
    """
    env = os.environ if environ is None else environ
    candidates: list[Path] = []
    program_files = env.get("ProgramFiles") or env.get("PROGRAMFILES")
    if program_files:
        candidates.append(Path(program_files) / "ClaudeCode" / "managed-settings.json")
    if project_dir is not None:
        candidates.append(project_dir / ".claude" / "settings.local.json")
        candidates.append(project_dir / ".claude" / "settings.json")
    candidates.append(user_settings_path(env))
    for path in candidates:
        mode = _settings_mode(path)
        if mode is not None:
            return mode, str(path)
    return "default", "no settings file sets permissions.defaultMode"


def bind(
    store: Store,
    host: Host,
    project: str,
    lane: str,
    session_id: str,
    mode: str | None,
    environ: Mapping[str, str] | None = None,
) -> tuple[Binding, Session, str]:
    """Record the lane's binding after checking the session is live right now.

    ``mode`` None means the window runs in its configured default mode; the third
    value says where the mode came from.
    """
    protocol.check_project_id(project, "--project")
    protocol.check_lane_id(lane, "--lane")
    check_session_id(session_id)
    if mode is not None:
        check_permission_mode(mode)
    session = _find_session(host.list_sessions(), session_id)
    if session is None:
        raise WakeError(
            WAKE_SESSION_UNAVAILABLE,
            f"no live Claude session {session_id}; run this from the lane's own window "
            "with its CLAUDE_CODE_SESSION_ID",
        )
    if mode is None:
        project_dir = Path(session.cwd) if session.cwd else None
        mode, source = configured_permission_mode(project_dir, environ)
        origin = f"configured default, from {source}"
    else:
        origin = "given with --permission-mode"
    binding = Binding(project, lane, HOST_CLAUDE, session_id, mode, utc_stamp())
    save_binding(store, binding)
    return binding, session, origin


def bind_codex(
    store: Store,
    project: str,
    lane: str,
    session_id: str,
    environ: Mapping[str, str] | None = None,
) -> Binding:
    """Bind only the current Codex thread, identified by its host-provided UUID."""
    protocol.check_project_id(project, "--project")
    protocol.check_lane_id(lane, "--lane")
    check_session_id(session_id)
    env = os.environ if environ is None else environ
    current = env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID")
    if (
        env.get("CODEX_THREAD_ID")
        and env.get("CODEX_SESSION_ID")
        and env["CODEX_THREAD_ID"] != env["CODEX_SESSION_ID"]
    ):
        raise WakeError(WAKE_SESSION_AMBIGUOUS, "Codex session identity variables disagree")
    if current != session_id:
        raise WakeError(
            WAKE_SESSION_UNAVAILABLE,
            "--session must equal this window's CODEX_THREAD_ID (or CODEX_SESSION_ID)",
        )
    binding = Binding(project, lane, HOST_CODEX, session_id, CODEX_QUEUE_MODE, utc_stamp())
    save_binding(store, binding)
    return binding


class RingFields(Protocol):
    """What the ring rule reads: a posted ``protocol.Note`` or a ``protocol.Draft``."""

    @property
    def needs(self) -> str: ...

    @property
    def kind(self) -> str | None: ...

    @property
    def reply_to(self) -> str: ...


def ring_reason(store: Store, project: str, note: RingFields) -> str | None:
    """Why ``note`` may ring its lane, or None when the next inbox check is enough.

    Rings: an operator relay whatever its ``needs`` (D40: an ``operator-said:`` relay is
    the answer a lane waits for), a note that needs a reply or an operator decision, a
    handover, and the answer to a note that needed one. Not: completions,
    acknowledgement-only notes, FYIs and replies to them, which only cost the recipient
    a turn (2026-11-14 echo).

    ``note`` may be a draft not yet posted, so ``post --wake`` can refuse an
    ineligible note before storing it; the answer is the same as for the note
    published from that draft.
    """
    if note.kind == "operator-relay":
        return "it is an operator relay"
    if note.needs in UNBLOCKING_NEEDS:
        return f"it needs {note.needs}"
    if note.kind == "handoff":
        return "it is a handover"
    if note.reply_to and note.reply_to != "none":
        try:
            asked = store.load_note(project, note.reply_to)
        except NoteNotFoundError:
            return None
        if asked.needs in UNBLOCKING_NEEDS:
            return f"it answers a note that needed {asked.needs}"
    return None


def _find_session(sessions: Sequence[Session], session_id: str) -> Session | None:
    for session in sessions:
        if session.session_id == session_id:
            return session
    return None


def binding_stale_reason(store: Store, project: str, lane: str, binding: Binding) -> str | None:
    """Why the lane's card rules ``binding`` out as its live routing target, or None.

    The one owner of this routing rule: ``wake send`` refuses to ring with this reason
    (``session-unavailable``), and the Stop hook surfaces no notes through a binding it
    rules out. A card of another host, a card naming another Claude session in
    ``window``, a Codex binding with no card, or a ``done`` card makes the binding
    stale. No card (or a damaged one; both raise CardNotFoundError) leaves a Claude
    binding in charge. Other store errors propagate.
    """
    try:
        card = store.load_card(project, lane)
    except CardNotFoundError:
        card = None
    if card is not None and card.host != binding.host:
        return (
            f"lane {lane} card names host {card.host}, but its binding names {binding.host}; "
            "the lane must rebind from its current host before delivery"
        )
    if (
        card is not None
        and card.window is not None
        and SESSION_ID_RE.fullmatch(card.window)
        and card.window != binding.session_id
    ):
        return "lane card session differs from binding; rebind"
    if card is None and binding.host == HOST_CODEX:
        # Without a card nothing shows the thread is live, and `--resume` could revive it.
        return f"Codex lane {lane} has no card; a lane registers its card before it is rung"
    if card is not None and card.state == CARD_STATE_DONE:
        # A late doorbell must not revive a closed lane. For Codex, `queue --resume`
        # could otherwise restart a finished thread on a local daemon, unseen.
        return (
            f"lane {lane} card is done; a closed lane is not rung. The note stays posted, "
            "and a reopened lane sets its card active and rebinds first"
        )
    return None


# --- Live view (card show --live, D45) ----------------------------------------------

LIVE_UNBOUND: Final = "unbound"
LIVE_NOT_RUNNING: Final = "not running"
LIVE_CODEX: Final = "codex"
# The live view's label for a row whose state cannot be established; not the Codex ring
# record state of the same spelling (an attempt of unknown outcome).
LIVE_UNKNOWN: Final = "unknown"


def window_state(store: Store, host: Host, project: str, lane: str) -> str:
    """The lane's bound window as ``card show --live`` prints it (D45).

    ``unbound``: no valid binding. ``not running``: the bound session is not listed, or
    the lane's card rules the binding out (``binding_stale_reason``, so ``wake send``
    refuses to ring through it). ``codex``: a Codex binding, which ``claude agents``
    does not list. Otherwise the session's status: ``idle``, ``busy``, or
    ``waiting: <waitingFor>`` (``waiting`` when the host names nothing), host text
    capped at HOST_TEXT_MAX_CHARS. A host listing that fails, or a binding or card that
    cannot be read, gives ``unknown (<code>)`` for this row only (EVIDENCE_ERRORS). Lists
    sessions only for a Claude binding that is still the lane's routing target; the
    caller shares one listing across lanes.
    """
    try:
        return _window_state(store, host, project, lane)
    except EVIDENCE_ERRORS as exc:
        return f"{LIVE_UNKNOWN} ({_evidence_code(exc)})"


def _window_state(store: Store, host: Host, project: str, lane: str) -> str:
    try:
        binding = load_binding(store, project, lane)
    except WakeError:
        return LIVE_UNBOUND
    if binding_stale_reason(store, project, lane, binding) is not None:
        return LIVE_NOT_RUNNING
    if binding.host == HOST_CODEX:
        return LIVE_CODEX
    session = _find_session(host.list_sessions(), binding.session_id)
    if session is None:
        return LIVE_NOT_RUNNING
    if session.status == STATUS_WAITING:
        if session.waiting_for:
            return f"{STATUS_WAITING}: {_cap(session.waiting_for)}"
        return STATUS_WAITING
    return _cap(session.status) if session.status else LIVE_UNKNOWN


def refused_counts(store: Store, project: str) -> dict[str, int | None] | None:
    """Per lane, how many unacknowledged notes have ``refused-busy`` as their latest ring
    record (latest_rings, D45). A note rung through more than one session (the lane
    rebound) counts by its latest attempt, so a later delivery clears an earlier
    refusal and a later refusal counts again. Read-only: lists the rings folder once and
    creates nothing.

    Unreadable evidence never aborts the listing and never passes for a count
    (EVIDENCE_ERRORS). A ring record that cannot be read or parsed makes the lane its
    file name carries map to None, shown as unknown, while other lanes keep exact
    counts; a rings folder that cannot be listed gives None for the whole result.
    """
    try:
        latest, unknown = latest_rings(_wake_folder(store, project, RINGS_DIR))
    except EVIDENCE_ERRORS:
        return None
    counts: dict[str, int | None] = {lane: None for lane in unknown}
    for (note_id, lane), (_, record) in latest.items():
        if record.get("state") != RING_REFUSED_BUSY or lane in unknown:
            continue
        try:  # observed_ack swallows OS errors; a forged note id raises ProtocolError
            if observed_ack(store, project, note_id, lane) is not None:
                continue
        except EVIDENCE_ERRORS:
            continue
        counts[lane] = (counts.get(lane) or 0) + 1
    return counts


def latest_rings(
    folder: Path, prefix: str = ""
) -> tuple[dict[tuple[str, str], tuple[str, dict[str, object]]], set[str]]:
    """The one owner of "the latest ring record of a note for a lane, across sessions".

    Among the ring record files in ``folder`` whose names start with ``prefix``
    (ring_prefix narrows it to one note and lane), maps each ``(note id, lane)`` to the
    file name and record of its latest attempt: the greatest ``attempt_utc``, and at the
    same second a refusal never hides another state (a handover or a failure). Also
    returns the lanes, by file name, with a record that cannot be read or parsed: their
    latest record is not known. A record that names another note or lane than its file
    name is ignored as forged, and a file name not of the ring shape belongs to no lane.
    A folder that cannot be listed raises StoreError; a missing one holds none.
    """
    latest: dict[tuple[str, str], tuple[tuple[str, bool, str], str, dict[str, object]]] = {}
    unknown: set[str] = set()
    for entry in _ring_entries(folder, prefix):
        name_lane = _ring_name_lane(entry)
        if name_lane is None:
            continue
        try:
            record = _read_json(folder / entry)
        except EVIDENCE_ERRORS:
            record = None
        note_id, lane, state = _ring_fields(record)
        if record is None or note_id is None or lane is None or state is None:
            unknown.add(name_lane)
            continue
        if not entry.startswith(ring_prefix(note_id, lane)):
            continue
        attempt = record.get("attempt_utc")
        key = (attempt if isinstance(attempt, str) else "", state != RING_REFUSED_BUSY, state)
        if (note_id, lane) not in latest or key > latest[(note_id, lane)][0]:
            latest[(note_id, lane)] = (key, entry, record)
    return {pair: (entry, record) for pair, (_, entry, record) in latest.items()}, unknown


def _ring_fields(
    record: Mapping[str, object] | None,
) -> tuple[str | None, str | None, str | None]:
    """A ring record's ``note_id``, ``lane`` and ``state``, each None unless a string
    (all None for no record)."""
    record = record or {}
    values = (record.get("note_id"), record.get("lane"), record.get("state"))
    note_id, lane, state = (value if isinstance(value, str) else None for value in values)
    return note_id, lane, state


def send(
    store: Store,
    host: Host,
    project: str,
    lane: str,
    note_id: str,
    *,
    wait_s: float = 0.0,
    poll_s: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    codex_host: CodexQueue | None = None,
) -> Outcome:
    """Ring the bound session about ``note_id``, then watch for ack and reply.

    Raises WakeError when the doorbell was not delivered; the note is untouched.
    """
    protocol.check_project_id(project, "--project")
    protocol.check_lane_id(lane, "--lane")
    protocol.check_note_id(note_id, "ID")
    note = store.load_note(project, note_id)  # posted: the file exists and parses
    if note.to != lane:
        raise WakeError(
            WAKE_NOT_A_LANE,
            f"note {note_id} is addressed to {note.to}, not to lane {lane}; "
            "a doorbell goes only to the one lane a note names",
        )
    ack_utc = observed_ack(store, project, note_id, lane)
    if ack_utc is not None:
        return Outcome(
            note_id,
            lane,
            None,
            None,
            None,
            "not sent: the lane already acknowledged this note",
            ack_utc,
            observed_reply(store, project, note_id, lane),
            0.0,
        )
    if ring_reason(store, project, note) is None:
        raise WakeError(
            WAKE_NOT_NEEDED,
            f"not rung: note {note_id} needs {note.needs} and unblocks no one, so the lane "
            f"sees it at its next inbox check. {RING_RULE}",
        )
    # The binding and the card belong to the recipient lane and can change between a
    # post and its ring (the lane rebinds or closes; an idempotent retry may ring much
    # later), so they are checked here, at ring time, never before posting. Only the
    # note's own eligibility (ring_reason) is checked up front by ``post --wake``.
    binding = load_binding(store, project, lane)
    stale = binding_stale_reason(store, project, lane, binding)
    if stale is not None:
        raise WakeError(WAKE_SESSION_UNAVAILABLE, stale)
    if binding.host == HOST_CODEX:
        return _send_codex(
            store,
            project,
            lane,
            note_id,
            binding,
            codex_host or CodexHost(),
            wait_s,
            poll_s,
            sleep,
            clock,
        )
    sessions = host.list_sessions()
    session = _find_session(sessions, binding.session_id)
    if session is None:
        raise WakeError(
            WAKE_SESSION_UNAVAILABLE,
            f"bound session {binding.session_id} for lane {lane} is not running (closed, "
            "or the binding is stale); the note stays posted; the lane rebinds from its "
            "current window",
        )
    record_path, lock_path = ring_paths(store, project, note_id, lane, binding.session_id)
    previous = _read_json(record_path)
    if previous is not None and previous.get("state") == RING_DELIVERED:
        return _already_delivered(
            store, project, lane, note_id, session, binding, previous, wait_s, poll_s, sleep, clock
        )
    if session.status != STATUS_IDLE:
        _record_refusal(record_path, lock_path, note_id, lane, binding, session)
        raise WakeError(
            WAKE_SESSION_BUSY, _busy_message(binding.session_id, session.name, session.status)
        )
    if session.name is None:
        raise WakeError(
            WAKE_SESSION_AMBIGUOUS, f"session {binding.session_id} has no name to address"
        )
    same_name = [s for s in sessions if s.name == session.name]
    if len(same_name) != 1:
        raise WakeError(
            WAKE_SESSION_AMBIGUOUS,
            f"{len(same_name)} live sessions are named {session.name!r}; rename the lane's "
            "window (/rename) so its name is unique, then send again",
        )

    _claim(lock_path)
    try:
        # Read again under the claim: another sender may have rung, and released its
        # claim, after the read above, and a delivered doorbell is never sent twice.
        again = _read_json(record_path)
        result: SendResult | None = None
        if again is None or again.get("state") != RING_DELIVERED:
            result = host.send(session.name, doorbell_text(note_id), binding.permission_mode)
            _write_json(record_path, _claude_record(note_id, lane, binding, session.name, result))
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass
    if result is None:
        previous = again or {}
        return _already_delivered(
            store, project, lane, note_id, session, binding, previous, wait_s, poll_s, sleep, clock
        )
    if not result.delivered:
        raise WakeError(
            WAKE_BRIDGE_FAILED,
            f"doorbell not delivered: {result.detail}. The note stays posted; sending "
            "again is safe",
        )
    line = (
        f"delivered to session {binding.session_id} ({session.name}) inbox, message "
        f"{result.msg_id}; delivery is not reading"
    )
    return _watch(
        store,
        project,
        lane,
        note_id,
        session,
        True,
        result.msg_id,
        line,
        wait_s,
        poll_s,
        sleep,
        clock,
    )


def _busy_message(session_id: str, name: str | None, status: str) -> str:
    """The ``wake-session-busy`` message, also kept (host text capped) as the detail of
    the refusal's ring record."""
    return (
        f"session {session_id} ({name}) is {status or 'not idle'}; not interrupting it. "
        "The note stays posted; send again when it is idle"
    )


def _claude_record(
    note_id: str, lane: str, binding: Binding, address: str, outcome: SendResult | Session
) -> dict[str, object]:
    """The one builder of a Claude ring record. From a send result: ``delivered`` with its
    message id, or ``failed``. From the session that was not idle: ``refused-busy``
    (D44) with its status and any waitingFor, host text capped (HOST_TEXT_MAX_CHARS) so
    the record stays under RECORD_MAX_BYTES and readable."""
    record: dict[str, object] = {
        "note_id": note_id,
        "lane": lane,
        "session_id": binding.session_id,
        # A send's address is the name it was sent to; a refusal's is host text.
        "address": address if isinstance(outcome, SendResult) else _cap(address),
        "permission_mode": binding.permission_mode,
        "attempt_utc": utc_stamp(),
    }
    if isinstance(outcome, SendResult):
        record["state"] = RING_DELIVERED if outcome.delivered else RING_FAILED
        record["detail"] = outcome.detail
        if outcome.delivered:
            record["delivered_utc"] = record["attempt_utc"]
            record["msg_id"] = outcome.msg_id
        return record
    name = None if outcome.name is None else _cap(outcome.name)
    status = _cap(outcome.status)
    record["state"] = RING_REFUSED_BUSY
    record["status"] = status
    record["detail"] = _cap(
        _busy_message(binding.session_id, name, status), REFUSAL_DETAIL_MAX_CHARS
    )
    if outcome.waiting_for is not None:
        record["waiting_for"] = _cap(outcome.waiting_for)
    return record


def _record_refusal(
    record_path: Path,
    lock_path: Path,
    note_id: str,
    lane: str,
    binding: Binding,
    session: Session,
) -> None:
    """D44: leave a ``refused-busy`` ring record of a busy refusal, then return.

    Written under the ring claim, and never over a rung record (RUNG_STATES:
    ``delivered`` or ``queued``): a sender that found the session busy because another
    sender's doorbell had just woken it must not hide that delivery. While another
    sender holds the claim, nothing is written; that sender's record tells what
    happened. A refusal that repeats the record already there (same session, address,
    status and waitingFor) writes nothing only while that record is also the note's
    latest ring record for the lane across sessions (latest_rings), so a lane that stays
    busy is not rewritten on every stall check; its ``attempt_utc`` is when that
    unchanged refusal began. Otherwise the refusal is the newest attempt and is written
    with a fresh ``attempt_utc``: after another session's later ``delivered`` or
    ``failed`` record (the lane rebound away and back), or when the latest record cannot
    be established. Nothing is retried (no automatic retry).
    Evidence only, so it fails open (EVIDENCE_ERRORS): any error claiming, reading or
    writing is swallowed, writes nothing over a record it could not read, and never
    replaces the ``wake-session-busy`` refusal the caller raises next.
    """
    try:
        _claim(lock_path)
    except EVIDENCE_ERRORS:
        return
    try:
        again = _read_json(record_path)
        if again is None or again.get("state") not in RUNG_STATES:
            record = _claude_record(note_id, lane, binding, session.name or "", session)
            if not (_same_refusal(again, record) and _is_latest(record_path, note_id, lane)):
                _write_json(record_path, record)
    except EVIDENCE_ERRORS:
        pass
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _is_latest(record_path: Path, note_id: str, lane: str) -> bool:
    """Whether the record at ``record_path`` is the latest ring record of ``note_id`` for
    ``lane`` across sessions (latest_rings), every one of them readable. Evidence that
    cannot be read or listed answers False, so the caller writes."""
    try:
        latest, unknown = latest_rings(record_path.parent, ring_prefix(note_id, lane))
    except EVIDENCE_ERRORS:
        return False
    found = latest.get((note_id, lane))
    return lane not in unknown and found is not None and found[0] == record_path.name


def _same_refusal(old: Mapping[str, object] | None, new: Mapping[str, object]) -> bool:
    """Whether ``old`` is already the refusal ``new`` would write, but for its time."""
    if old is None or old.get("state") != RING_REFUSED_BUSY:
        return False
    return {k: v for k, v in old.items() if k != "attempt_utc"} == {
        k: v for k, v in new.items() if k != "attempt_utc"
    }


def _already_delivered(
    store: Store,
    project: str,
    lane: str,
    note_id: str,
    session: Session,
    binding: Binding,
    previous: Mapping[str, object],
    wait_s: float,
    poll_s: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> Outcome:
    """The outcome of a send whose ring record already says delivered: nothing is sent."""
    msg = previous.get("msg_id")
    outcome_line = (
        f"not sent again: already delivered to session {binding.session_id} at "
        f"{previous.get('delivered_utc')}"
    )
    return _watch(
        store,
        project,
        lane,
        note_id,
        session,
        None,
        msg if isinstance(msg, str) else None,
        outcome_line,
        wait_s,
        poll_s,
        sleep,
        clock,
    )


def _send_codex(
    store: Store,
    project: str,
    lane: str,
    note_id: str,
    binding: Binding,
    host: CodexQueue,
    wait_s: float,
    poll_s: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> Outcome:
    record_path, lock_path = ring_paths(store, project, note_id, lane, binding.session_id)
    previous = _read_json(record_path)
    if previous is not None and previous.get("state") == RING_QUEUED:
        line = f"not queued again: already queued for Codex thread {binding.session_id}"
    else:
        _refuse_unknown_codex_attempt(previous, record_path)
        _claim(lock_path)
        try:
            previous = _read_json(record_path)
            if previous is not None and previous.get("state") == RING_QUEUED:
                detail = "already queued"
            else:
                _refuse_unknown_codex_attempt(previous, record_path)
                record = {
                    "note_id": note_id,
                    "lane": lane,
                    "session_id": binding.session_id,
                    "attempt_utc": utc_stamp(),
                }
                # Recorded before the call, so a crash or an ambiguous result can never
                # lead to a second queue (and a second resume) of the same doorbell.
                _write_json(record_path, {**record, "state": "attempting", "detail": ""})
                try:
                    detail = host.queue(binding.session_id, doorbell_text(note_id))
                except WakeError as exc:
                    state = "unknown" if exc.code == WAKE_QUEUE_UNKNOWN else RING_FAILED
                    _write_json(record_path, {**record, "state": state, "detail": exc.message})
                    raise
                _write_json(record_path, {**record, "state": RING_QUEUED, "detail": detail})
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass
        line = (
            f"queued for Codex thread {binding.session_id}; {detail}; "
            "delivery and reading unverified"
        )
    return _watch(
        store,
        project,
        lane,
        note_id,
        None,
        None,
        None,
        line,
        wait_s,
        poll_s,
        sleep,
        clock,
    )


def _refuse_unknown_codex_attempt(previous: dict[str, object] | None, record_path: Path) -> None:
    """A Codex attempt whose outcome is unknown may have queued; never queue it twice."""
    if previous is not None and previous.get("state") in ("attempting", "unknown"):
        raise WakeError(
            WAKE_QUEUE_UNKNOWN,
            "an earlier codex queue attempt for this note and thread has an unknown outcome "
            f"({previous.get('detail') or 'interrupted'}); not queued again. Wait for the "
            f"lane's ack, or, after checking it was not queued, delete {record_path}",
        )


def _claim(lock_path: Path) -> None:
    try:
        with open(lock_path, "xb") as handle:
            handle.write(utc_stamp().encode("ascii"))
        return
    except FileExistsError:
        pass
    except OSError as exc:
        raise StoreError("store-unusable", f"cannot create {lock_path}: {os_reason(exc)}") from None
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        age = 0.0
    if age > LOCK_STALE_S:
        try:
            lock_path.unlink()
        except OSError:
            pass
        return _claim(lock_path)
    raise WakeError(WAKE_IN_PROGRESS, "another sender is ringing this lane about this note now")


def _watch(
    store: Store,
    project: str,
    lane: str,
    note_id: str,
    session: Session | None,
    delivered: bool | None,
    msg_id: str | None,
    wake_line: str,
    wait_s: float,
    poll_s: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> Outcome:
    start = clock()
    ack_utc = observed_ack(store, project, note_id, lane)
    reply_id = observed_reply(store, project, note_id, lane)
    while (ack_utc is None or reply_id is None) and clock() - start < wait_s:
        sleep(min(poll_s, max(0.0, wait_s - (clock() - start))))
        ack_utc = ack_utc or observed_ack(store, project, note_id, lane)
        reply_id = reply_id or observed_reply(store, project, note_id, lane)
    return Outcome(
        note_id, lane, session, delivered, msg_id, wake_line, ack_utc, reply_id, clock() - start
    )
