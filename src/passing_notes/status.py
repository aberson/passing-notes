"""``passing-notes status``: every lane of one project, or of every project the caller
may see, as one read-only table (plan P31-P34, D47-D57).

The read model is collected once and rendered as text or JSON. It reads cards, note
headers (through the validated ``Store.scan_notes``; no second parser), acknowledgement
file names, doorbell bindings and queue holders, plus cheap free-memory evidence.
It never prints, returns or keeps a note body. Only the subject of a scoped actionable
coordinator item crosses into output; unrelated subjects stay private. It writes
nothing: no ack, card, ring or folder.

Who may see what is the topology's rule (docs/protocol.md#topology, "Seeing"); this
module applies it and keeps no copy of its wording. The caller is the host window
named by its session environment variable, matched against that host's bindings
(``hook.session_bindings``) that are still live (``wake.binding_stale_reason`` is
None). The workspace main (lane ``main`` of project ``dev``) sees every project; any
other live binding in project P sees P; several live bindings see the union; an
unbound caller may ask for any project or, explicitly, for all of them. The check is
soft: the CLI cannot prove who types the command.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from itertools import islice
from types import MappingProxyType
from typing import Final

from passing_notes import hook, liveness, protocol, queue, status_resources, wake
from passing_notes.liveness import Observer
from passing_notes.protocol import (
    CARD_STATES,
    DIAGNOSTIC_LINES,
    MAIN_LANE,
    NOTE_SUFFIX,
    RECIPIENT_ALL,
    RECIPIENT_OPERATOR,
    REPLY_TO_NONE,
    WORKSPACE_PROJECT,
    Card,
    Note,
    ProtocolError,
)
from passing_notes.store import (
    ACKS_DIR,
    CARDS_DIR,
    MESSAGES_DIR,
    QUEUES_DIR,
    STORE_UNUSABLE,
    Diagnostic,
    Store,
    StoreError,
    display_name,
    os_reason,
)

SCHEMA: Final = "passing-notes.status/1"
STATUS_OUT_OF_SCOPE: Final = "status-out-of-scope"
TOPOLOGY_REF: Final = "docs/protocol.md#topology"
STALE_AFTER_DEFAULT_HOURS: Final = 24
MAX_REPLY_PROJECTS: Final = 1024
MAX_REPLY_ENTRIES: Final = 4096

# The D54 JSON shape: the keys of each object, in output order. The one source of truth
# for the shape; the renderer builds every object from these tuples and docs/cli.md is
# pinned against them. Changes are additive only.
JSON_KEYS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "status": (
            "schema",
            "generated_utc",
            "scope",
            "projects",
            "diagnostics",
            "collapsed_idle",
            "resources",
            "attention",
            "coordinator_attention",
            "sessions",
        ),
        "session": ("label", "host", "binding", "current", "lanes"),
        "scope": ("caller", "projects"),
        "project": ("project", "lanes", "done_count", "operator_decisions"),
        "lane": (
            "lane",
            "host",
            "state",
            "item",
            "updated_utc",
            "age_minutes",
            "window",
            "open",
            "open_unblocking",
            "flags",
            "waiting_on",
            "window_state",
            "activity",
            "transcript_age_minutes",
            "open_subagents",
            "child_processes",
            "activity_reason",
        ),
        "diagnostics": ("shown", "total"),
        "resources": ("free_memory_bytes", "queue_holders", "queues_complete"),
        "queue_holder": ("project", "resource", "lane", "deadline_utc"),
        "attention": ("project", "lane", "reason"),
        "coordinator_attention": (
            "priority",
            "kind",
            "project",
            "lane",
            "evidence",
            "summary",
            "delivery",
            "action",
        ),
    }
)

COLUMNS: Final = (
    "PROJECT",
    "LANE",
    "STATE",
    "HOST",
    "WINDOW",
    "ACTIVITY",
    "WAITING ON",
    "UPDATED",
    "OPEN",
    "WORKING ON",
)
COLUMN_GAP: Final = "  "
ITEM_WIDTH: Final = 64
ELLIPSIS: Final = "..."
# Card states a stale or doorbell-less lane matters for (D50).
ACTIVE_LIKE_STATES: Final = ("active", "waiting", "blocked")
DONE_STATE: Final = "done"
# The needs that make an open note unblocking (the "r" in OPEN's N/Mr).
UNBLOCKING_NEEDS: Final = ("reply", "operator-decision")
OPERATOR_DECISION_NEEDS: Final = "operator-decision"
# A note to either recipient waits until main acknowledges it or an eligible reply exists.
OPERATOR_RECIPIENTS: Final = (RECIPIENT_OPERATOR, MAIN_LANE)
FLAG_STALE: Final = "stale"
FLAG_NO_DOORBELL: Final = "no-doorbell"
WINDOW_BOUND: Final = "bound"
WINDOW_NOT_BOUND: Final = "not-bound"
# The store folders whose sub-folders name projects (D55).
PROJECT_KINDS: Final = (MESSAGES_DIR, ACKS_DIR, CARDS_DIR, QUEUES_DIR, wake.WAKE_DIR)
# Files directly in these folders are the flat pilot layout, which scan_flat reports.
_FLAT_KINDS: Final = (MESSAGES_DIR, ACKS_DIR)
_NOT_A_PROJECT_ID: Final = "ignored: not a valid project id"
_NOT_A_PROJECT_FOLDER: Final = "ignored: not a project folder"


# --- Caller scope (P32, D57) ------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """The caller's live bindings as ``(project, lane)``; empty when unbound."""

    caller: tuple[tuple[str, str], ...]

    @property
    def bound(self) -> bool:
        return bool(self.caller)

    @property
    def sees_all(self) -> bool:
        return not self.bound or (WORKSPACE_PROJECT, MAIN_LANE) in self.caller

    def sees(self, project: str) -> bool:
        return self.sees_all or any(bound == project for bound, _ in self.caller)

    def labels(self) -> list[str]:
        return sorted(f"{project}/{lane}" for project, lane in self.caller)


def caller_scope(store: Store, session_id: str, *, host: str = wake.HOST_CLAUDE) -> Scope:
    """The scope of the window ``session_id`` names; unbound if it names none.

    An empty or malformed id, or one with no live host binding, is unbound. A binding
    that ``wake.binding_stale_reason`` rules out grants nothing.
    """
    if not session_id:
        return Scope(())
    try:
        bindings = hook.session_bindings(store, session_id, host=host)
    except ProtocolError:  # not a host session id
        return Scope(())
    live = {
        (b.project, b.lane)
        for b in bindings
        if wake.binding_stale_reason(store, b.project, b.lane, b) is None
    }
    return Scope(tuple(sorted(live)))


def mine_projects(scope: Scope) -> list[str]:
    """``--mine``: the projects of the caller's live bindings, sorted; empty when unbound."""
    return sorted({project for project, _ in scope.caller})


def refusal(scope: Scope, project: str | None) -> str | None:
    """Why the caller may not see ``project`` (None: every project); None if it may."""
    if project is None and scope.sees_all:
        return None
    if project is not None and scope.sees(project):
        return None
    bound = ", ".join(scope.labels())
    visible = ", ".join(sorted({p for p, _ in scope.caller}))
    wanted = "every project" if project is None else f"project {project}"
    return (
        f"this window is bound as {bound} and sees only {visible}, not {wanted}; only the "
        f"workspace main ({WORKSPACE_PROJECT}/{MAIN_LANE}) or an unbound terminal sees "
        f"every project (see {TOPOLOGY_REF})"
    )


# --- The read model ---------------------------------------------------------------------


@dataclass(frozen=True)
class LaneRow:
    lane: str
    host: str
    state: str
    item: str
    updated_utc: str
    age_minutes: int
    window: str
    open: int
    open_unblocking: int
    flags: tuple[str, ...]
    waiting_on: str | None = None
    evidence: liveness.Evidence = field(default_factory=liveness.Evidence)
    session_id: str | None = None


@dataclass(frozen=True)
class ProjectBlock:
    """One project's rows. ``done_count`` counts every done card, shown or not;
    ``hidden_done`` is how many done rows ``--live`` left out of ``lanes``."""

    project: str
    lanes: tuple[LaneRow, ...]
    operator_decisions: int
    done_count: int
    hidden_done: int = 0


def hide_done(block: ProjectBlock) -> ProjectBlock:
    """``--live``: ``block`` without its done rows; ``done_count`` keeps the true count."""
    shown = tuple(row for row in block.lanes if row.state != DONE_STATE)
    return replace(block, lanes=shown, hidden_done=len(block.lanes) - len(shown))


@dataclass(frozen=True)
class Attention:
    project: str
    lane: str
    reason: str


@dataclass(frozen=True)
class CoordinatorAttention:
    """One scoped action; its subject may cross, but no body or unrelated subject."""

    priority: int
    kind: str
    project: str
    lane: str
    evidence: str
    summary: str
    delivery: str
    action: str


def first_attention(blocks: Sequence[ProjectBlock]) -> Attention | None:
    """Host approval/stall evidence wins over self-reported state and note counts."""
    candidates: list[tuple[int, int, int, str, str, str]] = []
    for block in blocks:
        for row in block.lanes:
            if row.state == DONE_STATE:
                continue
            reasons = (
                (row.evidence.activity == "pending-approval", "activity pending-approval"),
                (row.evidence.activity == "suspected-stall", "activity suspected-stall"),
                (row.state == "blocked", "state blocked"),
                (row.waiting_on == "operator", "waiting on operator"),
                (row.open_unblocking > 0, "unblocking notes"),
                (FLAG_STALE in row.flags, "stale card"),
                (FLAG_NO_DOORBELL in row.flags, "no doorbell"),
            )
            for rank, (matches, reason) in enumerate(reasons):
                if matches:
                    if row.waiting_on and reason != "waiting on operator":
                        reason += f"; waiting on {row.waiting_on}"
                    candidates.append(
                        (
                            rank,
                            -row.open_unblocking,
                            -row.age_minutes,
                            block.project,
                            row.lane,
                            reason,
                        )
                    )
                    break
    if not candidates:
        return None
    _, _, _, project, lane, reason = min(candidates)
    return Attention(project, lane, reason)


@dataclass(frozen=True)
class Report:
    generated_utc: str
    caller: tuple[str, ...]
    projects: tuple[ProjectBlock, ...]
    diagnostics: tuple[Diagnostic, ...]
    collapsed_idle: tuple[str, ...] = ()
    resources: status_resources.Resources = field(default_factory=status_resources.Resources)
    coordinator_attention: tuple[CoordinatorAttention, ...] = ()

    @property
    def attention(self) -> Attention | None:
        return first_attention(self.projects)

    @property
    def lane_count(self) -> int:
        return sum(len(block.lanes) for block in self.projects)


def needs_attention(report: Report) -> Report:
    """Focus recorded waits and requests, without interpreting free-form card prose.

    Preserve project decision totals and diagnostics even when no lane matches.
    Matching idle caller rows must not disappear into the idle summary.
    """
    return replace(
        report,
        projects=tuple(
            replace(
                block,
                lanes=tuple(
                    row
                    for row in block.lanes
                    if row.state != DONE_STATE
                    and (
                        row.state in {"blocked", "waiting"}
                        or row.waiting_on is not None
                        or row.open_unblocking > 0
                        or row.evidence.activity in {"pending-approval", "suspected-stall"}
                        or (block.operator_decisions > 0 and row.lane == MAIN_LANE)
                    )
                ),
            )
            for block in report.projects
        ),
        collapsed_idle=(),
    )


def list_projects(
    store: Store, *, kinds: Sequence[str] = PROJECT_KINDS, max_entries: int | None = None
) -> tuple[list[str], list[Diagnostic]]:
    """D55: the sorted union of project folder names, plus a diagnostic per other name.

    Only folder names are listed here; nothing is read or created.
    """
    found: set[str] = set()
    diagnostics: list[Diagnostic] = []
    for kind in kinds:
        folder = store._store_folder(kind)  # refuses a link
        try:
            with os.scandir(folder) as listing:
                entries = list(listing if max_entries is None else islice(listing, max_entries + 1))
                if max_entries is not None and len(entries) > max_entries:
                    diagnostics.append(Diagnostic(kind, "entry limit reached; scan incomplete"))
                    entries = entries[:max_entries]
                entries.sort(key=lambda entry: entry.name)
        except FileNotFoundError:
            continue
        except NotADirectoryError:
            diagnostics.append(Diagnostic(kind, "expected a folder, found a file"))
            continue
        except OSError as exc:
            raise StoreError(STORE_UNUSABLE, f"cannot list {folder}: {os_reason(exc)}") from None
        for entry in entries:
            rel = f"{kind}/{display_name(entry.name)}"
            try:
                is_folder = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_folder = False
            if not is_folder:
                if kind not in _FLAT_KINDS:
                    diagnostics.append(Diagnostic(rel, _NOT_A_PROJECT_FOLDER))
                continue
            try:
                protocol.check_project_id(entry.name)
            except ProtocolError:
                diagnostics.append(Diagnostic(rel, _NOT_A_PROJECT_ID))
                continue
            found.add(entry.name)
    return sorted(found), diagnostics


def _state_rank(state: str) -> int:
    return CARD_STATES.index(state) if state in CARD_STATES else len(CARD_STATES)


def _lane_row(
    store: Store,
    card: Card,
    bindings: Mapping[str, wake.Binding],
    open_notes: Sequence[tuple[str, str]],
    now: datetime,
    stale_after_hours: int,
    observer: Observer,
) -> LaneRow:
    age = now - protocol.parse_utc(card.updated_utc, "updated_utc")
    binding = bindings.get(card.lane)
    flags: list[str] = []
    session_id = None
    evidence = liveness.Evidence("unbound", reason="no-binding")
    if binding is not None:
        try:
            if wake.binding_stale_reason(store, card.project, card.lane, binding) is not None:
                evidence = liveness.Evidence("not-running", reason="stale-binding")
            else:
                session_id = binding.session_id
                evidence = observer.observe(binding, now)
        except wake.EVIDENCE_ERRORS:
            evidence = liveness.Evidence(reason="binding-unavailable")
    if card.state in ACTIVE_LIKE_STATES:
        # Compared in seconds, so any accepted --stale-after value works.
        if age.total_seconds() > stale_after_hours * 3600:
            flags.append(FLAG_STALE)
        live = (
            binding is not None
            and wake.binding_stale_reason(store, card.project, card.lane, binding) is None
        )
        if not live:
            flags.append(FLAG_NO_DOORBELL)
    return LaneRow(
        lane=card.lane,
        host=card.host,
        state=card.state,
        item=card.item,
        updated_utc=card.updated_utc,
        age_minutes=queue.whole_minutes(age),
        # D52: whether a binding record exists, live or not; no-doorbell is the live test.
        window=WINDOW_NOT_BOUND if binding is None else WINDOW_BOUND,
        open=len(open_notes),
        open_unblocking=sum(
            1
            for needs, recipient in open_notes
            if needs in UNBLOCKING_NEEDS and recipient != RECIPIENT_ALL
        ),
        flags=tuple(flags),
        waiting_on=card.waiting_on,
        evidence=evidence,
        session_id=session_id,
    )


@dataclass(frozen=True)
class _Request:
    sender: tuple[str, str]
    to: str


@dataclass(frozen=True)
class _Reply:
    target: str
    sender: tuple[str, str]


RequestKey = tuple[str, str]


@dataclass(frozen=True)
class _ProjectRead:
    """Counts plus scoped actionable headers cross this boundary; note bodies never do."""

    block: ProjectBlock
    diagnostics: tuple[Diagnostic, ...]
    pending: Mapping[str, frozenset[str]]
    pending_unblocking: Mapping[str, frozenset[str]]
    decisions: frozenset[str]
    requests: Mapping[str, _Request]
    replies: tuple[_Reply, ...]
    attention_notes: tuple[Note, ...]
    cards: tuple[Card, ...]
    acked: frozenset[tuple[str, str]]
    ring_states: Mapping[tuple[str, str], str]


def _reply_facts(notes: Iterable[Note]) -> tuple[_Reply, ...]:
    return tuple(
        _Reply(note.reply_to, note.sender)
        for note in notes
        if note.reply_to != REPLY_TO_NONE and note.reply_to != note.id
    )


def _resolved(
    requests: Mapping[RequestKey, _Request], replies: Iterable[_Reply]
) -> set[RequestKey]:
    by_id: dict[str, list[tuple[RequestKey, _Request]]] = {}
    for key, request in requests.items():
        by_id.setdefault(key[1], []).append((key, request))
    return {
        key
        for reply in replies
        for key, request in by_id.get(reply.target, ())
        if reply.sender != request.sender
        and (
            reply.sender in {(key[0], MAIN_LANE), (request.sender[0], MAIN_LANE)}
            or (
                request.to not in {RECIPIENT_ALL, RECIPIENT_OPERATOR}
                and reply.sender == (key[0], request.to)
            )
        )
    }


def _foreign_replies(
    store: Store, selected: set[str], wanted: Mapping[RequestKey, _Request]
) -> tuple[set[RequestKey], bool]:
    """Reply links only, from other projects; suppress foreign names and diagnostics.

    Missing/invalid evidence cannot resolve a question. One generic diagnostic tells
    the caller that unanswered counts may be high without revealing another project.
    """
    replies: set[RequestKey] = set()
    try:
        projects, diagnostics = list_projects(
            store, kinds=(MESSAGES_DIR,), max_entries=MAX_REPLY_PROJECTS
        )
    except StoreError:
        return replies, True
    incomplete = bool(diagnostics)
    remaining = MAX_REPLY_ENTRIES
    for project in projects:
        if project in selected:
            continue
        if remaining <= 0:
            incomplete = True
            break
        try:
            notes, diagnostics = store.scan_notes(project, max_entries=remaining)
        except StoreError:
            incomplete = True
            continue
        remaining -= len(notes) + len(diagnostics)
        replies.update(_resolved(wanted, _reply_facts(notes)))
        incomplete |= bool(diagnostics)
        if wanted.keys() <= replies:
            break
    return replies, incomplete


def _answer_aware(read: _ProjectRead, resolved: set[RequestKey]) -> ProjectBlock:
    replies = {note_id for project, note_id in resolved if project == read.block.project}
    rows: list[LaneRow] = []
    for row in read.block.lanes:
        answered = len(read.pending[row.lane] & replies)
        answered_unblocking = len(read.pending_unblocking[row.lane] & replies)
        rows.append(
            replace(
                row,
                open=row.open - answered,
                open_unblocking=row.open_unblocking - answered_unblocking,
            )
        )
    return replace(read.block, lanes=tuple(rows), operator_decisions=len(read.decisions - replies))


def _project_block(
    store: Store,
    project: str,
    now: datetime,
    stale_after_hours: int,
    observer: Observer,
) -> _ProjectRead:
    """One project's rows and diagnostics; bodies and unrelated subjects stay here."""
    cards, card_diagnostics = store.scan_cards(project)
    notes, note_diagnostics = store.scan_notes(project)
    acked, ack_diagnostics = store.scan_acks(project)
    supersedes = protocol.resolve_supersedes(notes)
    supersedes_diagnostics = [
        Diagnostic(f"{MESSAGES_DIR}/{project}/{note_id}{NOTE_SUFFIX}", reason)
        for note_id, reason in supersedes.ignored
    ]
    current = [note for note in notes if note.id not in supersedes.hidden]
    try:
        rings, unknown_rings = wake.latest_rings(
            store.root / wake.WAKE_DIR / project / wake.RINGS_DIR
        )
    except wake.EVIDENCE_ERRORS:
        rings, unknown_rings = {}, set()
    ring_states = {key: str(record.get("state", "unknown")) for key, (_, record) in rings.items()}
    for lane in unknown_rings:
        ring_states[("*", lane)] = "unknown"
    bindings = {binding.lane: binding for binding in wake.list_bindings(store, project)}
    rows = [
        _lane_row(
            store,
            card,
            bindings,
            [
                (note.needs, note.to)
                for note in current
                if protocol.is_addressed_to(note, project, card.lane)
                and (note.id, card.lane) not in acked
            ],
            now,
            stale_after_hours,
            observer,
        )
        for card in cards
    ]
    rows.sort(key=lambda row: (_state_rank(row.state), row.lane))
    decisions = frozenset(
        note.id
        for note in current
        if note.needs == OPERATOR_DECISION_NEEDS
        and note.to in OPERATOR_RECIPIENTS
        and (note.id, MAIN_LANE) not in acked
    )
    diagnostics = card_diagnostics + note_diagnostics + ack_diagnostics + supersedes_diagnostics
    done = sum(1 for row in rows if row.state == DONE_STATE)
    pending = {
        card.lane: frozenset(
            note.id
            for note in current
            if note.needs in UNBLOCKING_NEEDS
            and protocol.is_addressed_to(note, project, card.lane)
            and (note.id, card.lane) not in acked
        )
        for card in cards
    }
    pending_unblocking = {
        card.lane: frozenset(
            note.id
            for note in current
            if note.needs in UNBLOCKING_NEEDS
            and note.to != RECIPIENT_ALL
            and protocol.is_addressed_to(note, project, card.lane)
            and (note.id, card.lane) not in acked
        )
        for card in cards
    }
    return _ProjectRead(
        ProjectBlock(project, tuple(rows), len(decisions), done),
        tuple(diagnostics),
        pending,
        pending_unblocking,
        decisions,
        {
            note.id: _Request(note.sender, note.to)
            for note in current
            if note.needs in UNBLOCKING_NEEDS
        },
        _reply_facts(notes),
        tuple(
            note
            for note in current
            if note.to in OPERATOR_RECIPIENTS
            and ((note.id, MAIN_LANE) not in acked or note.needs in UNBLOCKING_NEEDS)
            and (
                (note.kind in {"handoff", "blocker"} and note.needs in UNBLOCKING_NEEDS)
                or note.needs == OPERATOR_DECISION_NEEDS
            )
        ),
        tuple(cards),
        frozenset(acked),
        ring_states,
    )


def _delivery(read: _ProjectRead, note: Note) -> str:
    """Receipt and wake evidence are separate; queued Codex work stays unverified."""
    if (note.id, MAIN_LANE) in read.acked:
        return "acknowledged-unreplied"
    state = read.ring_states.get((note.id, MAIN_LANE))
    if state == wake.RING_QUEUED:
        return "queued-unverified"
    if state == wake.RING_DELIVERED:
        return "delivered-unread"
    if state is not None:
        return state
    if ("*", MAIN_LANE) in read.ring_states:
        return "unknown"
    return "posted-unrung"


def _coordinator_attention(
    reads: Sequence[_ProjectRead], resolved: set[RequestKey], now: datetime, scope: Scope
) -> tuple[CoordinatorAttention, ...]:
    """Prioritized coordinator work from structured headers and card deadlines only."""
    items: list[CoordinatorAttention] = []
    gate_ids: set[RequestKey] = set()
    for read in reads:
        project = read.block.project
        if (
            scope.bound
            and (WORKSPACE_PROJECT, MAIN_LANE) not in scope.caller
            and (
                project,
                MAIN_LANE,
            )
            not in scope.caller
        ):
            continue
        for note in read.attention_notes:
            key = (project, note.id)
            if key in resolved:
                continue
            if note.kind in {"handoff", "blocker"} and note.needs != OPERATOR_DECISION_NEEDS:
                gate_ids.add(key)
                items.append(
                    CoordinatorAttention(
                        1,
                        "blocking-gate" if note.kind == "blocker" else "gate-handoff",
                        project,
                        note.from_lane,
                        note.id,
                        note.subject,
                        _delivery(read, note),
                        f"read and disposition for {note.from_lane}",
                    )
                )
        for note in read.attention_notes:
            key = (project, note.id)
            if note.needs != OPERATOR_DECISION_NEEDS or key in resolved or key in gate_ids:
                continue
            items.append(
                CoordinatorAttention(
                    2,
                    "operator-decision",
                    project,
                    note.from_lane,
                    note.id,
                    note.subject,
                    _delivery(read, note),
                    "show the operator, then reply through main",
                )
            )
        for card in read.cards:
            waiting_on = card.waiting_on
            if card.state == DONE_STATE or not waiting_on or not waiting_on.startswith("time:"):
                continue
            deadline = protocol.parse_utc(waiting_on.removeprefix("time:"), "waiting_on time")
            if deadline > now:
                continue
            items.append(
                CoordinatorAttention(
                    3,
                    "overdue-checkpoint",
                    project,
                    card.lane,
                    waiting_on,
                    "promised review/status checkpoint elapsed",
                    "overdue",
                    f"request status from {card.lane}",
                )
            )
    return tuple(
        sorted(
            items,
            key=lambda item: (item.priority, item.project, item.lane, item.evidence),
        )
    )


def collect(
    store: Store,
    scope: Scope,
    projects: Sequence[str] | None,
    *,
    now: datetime,
    stale_after_hours: int = STALE_AFTER_DEFAULT_HOURS,
    live: bool = False,
    observer: Observer | None = None,
    expand_idle: bool = False,
) -> Report:
    """The report for ``projects`` in the order given (duplicates dropped), or for every
    project (None). ``live`` leaves done lanes out of the rows and keeps each project's
    true ``done_count``. Idle collapse is metadata only: every idle row remains in
    the report. Reads only.

    The caller has passed ``refusal`` for each project already.
    """
    diagnostics = store.scan_flat()
    if projects is None:
        names, listing_diagnostics = list_projects(store)
        diagnostics += listing_diagnostics
    else:
        names = list(dict.fromkeys(protocol.check_project_id(p, "--project") for p in projects))
    observer = observer or Observer()
    reads: list[_ProjectRead] = []
    for name in names:
        read = _project_block(store, name, now, stale_after_hours, observer)
        reads.append(read)
        diagnostics += read.diagnostics
    requests = {
        (read.block.project, note_id): read.requests[note_id]
        for read in reads
        for ids in (
            *read.pending.values(),
            read.decisions,
            frozenset(note.id for note in read.attention_notes if note.needs in UNBLOCKING_NEEDS),
        )
        for note_id in ids
    }
    replies = _resolved(requests, (reply for read in reads for reply in read.replies))
    wanted = {key: request for key, request in requests.items() if key not in replies}
    if projects is not None and wanted:
        foreign, incomplete = _foreign_replies(store, set(names), wanted)
        replies.update(foreign)
        if incomplete and wanted.keys() - foreign:
            diagnostics.append(
                Diagnostic("status", "reply evidence incomplete; unanswered counts may be high")
            )
    blocks: list[ProjectBlock] = []
    for read in reads:
        block = _answer_aware(read, replies)
        blocks.append(hide_done(block) if live else block)
    resources, resource_diagnostics = status_resources.collect(store, names)
    diagnostics.extend(resource_diagnostics)
    caller = set(scope.labels())
    collapsed = tuple(
        f"{block.project}/{row.lane}"
        for block in blocks
        for row in block.lanes
        if not expand_idle
        and f"{block.project}/{row.lane}" in caller
        and row.state == "idle"
        and row.evidence.window_state != "waiting"
        and row.evidence.activity not in {"pending-approval", "suspected-stall"}
    )
    return Report(
        generated_utc=protocol.format_utc(now),
        caller=tuple(scope.labels()),
        projects=tuple(blocks),
        diagnostics=tuple(diagnostics),
        collapsed_idle=collapsed,
        resources=resources,
        coordinator_attention=_coordinator_attention(reads, replies, now, scope),
    )


# --- Text (D47) ---------------------------------------------------------------------------


def ascii_cell(text: str, width: int | None = None, *, keep_tail: bool = False) -> str:
    """``text`` as printable ASCII, each other character shown as an escape such as
    ``\\xe9`` (Python's ``ascii`` form); cut to ``width`` characters ending in ELLIPSIS when longer.

    With ``keep_tail``, put the ellipsis in the middle, retaining the ending.
    An escape is never split by either cut.
    """
    # escape_unsafe's rule, widened to every non-ASCII character, in its escape form.
    pieces = [
        ch if ch.isascii() and not protocol.is_unsafe_char(ch) else protocol.escape_char(ch)
        for ch in text
    ]
    whole = "".join(pieces)
    if width is None or len(whole) <= width:
        return whole
    prefix_budget = (width - len(ELLIPSIS)) // 3 if keep_tail else width - len(ELLIPSIS)
    kept: list[str] = []
    used = 0
    for piece in pieces:
        if used + len(piece) > prefix_budget:
            break
        kept.append(piece)
        used += len(piece)
    tail: list[str] = []
    if keep_tail:
        for piece in reversed(pieces):
            if used + len(piece) > width - len(ELLIPSIS):
                break
            tail.append(piece)
            used += len(piece)
    return "".join(kept) + ELLIPSIS + "".join(reversed(tail))


def format_age(minutes: int) -> str:
    """A card age as ``4m``, ``3h`` or ``2d`` (rounded down)."""
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 24 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


def format_open(row: LaneRow) -> str:
    """``N``, or ``N/Mr`` when M of the N open notes need a reply or a decision."""
    return f"{row.open}/{row.open_unblocking}r" if row.open_unblocking else str(row.open)


def _cells(project: str, row: LaneRow) -> list[str]:
    return [
        project,
        row.lane,
        row.state,
        row.host,
        row.evidence.window_state,
        row.evidence.activity,
        row.waiting_on or "-",
        format_age(row.age_minutes),
        format_open(row),
        ascii_cell(row.item, ITEM_WIDTH, keep_tail=True),
    ]


def _join(cells: Sequence[str], widths: Sequence[int]) -> str:
    padded = [cell.ljust(width) for cell, width in zip(cells[:-1], widths, strict=False)]
    return COLUMN_GAP.join([*padded, cells[-1]]).rstrip()


def resource_line(resources: status_resources.Resources) -> str:
    memory = (
        "unknown"
        if resources.free_memory_bytes is None
        else f"{resources.free_memory_bytes / 2**30:.1f} GiB"
    )
    holders = (
        "; ".join(
            f"{holder.project}/{holder.resource}={holder.lane} until {holder.deadline_utc}"
            for holder in resources.queue_holders
        )
        or "none"
    )
    if not resources.queues_complete:
        holders = (holders + " (incomplete)") if resources.queue_holders else "unknown (incomplete)"
    return f"resources: memory free {memory} | queue holders: {holders}"


def session_groups(report: Report) -> list[dict[str, object]]:
    """Group only validated host/session bindings, within the selected scope.

    Labels are report-local; raw host identifiers never enter the output. Unknown
    and stale bindings remain separate lane records, never one invented window.
    """
    grouped: dict[tuple[str, str], list[str]] = {}
    for block in report.projects:
        for row in block.lanes:
            label = f"{block.project}/{row.lane}"
            key = (row.host, row.session_id or label)
            grouped.setdefault(key, []).append(label)
    result: list[dict[str, object]] = []
    rows = {f"{b.project}/{r.lane}": r for b in report.projects for r in b.lanes}
    ordered = sorted(
        grouped.items(),
        key=lambda entry: (not any(lane in report.caller for lane in entry[1]),),
    )
    for (host, _), lanes in ordered:
        bound = rows[lanes[0]].session_id is not None
        current = any(lane in report.caller for lane in lanes)
        label = "This session" if current else f"Session {len(result) + 1}"
        if not bound:
            label = f"Unbound record {len(result) + 1}"
        result.append(
            _record("session", label, host, "bound" if bound else "unbound", current, lanes)
        )
    return result


def render_text(report: Report) -> list[str]:
    """Resources and collapsed-idle summary, then each project's visible rows, then
    ``done: N hidden`` when ``--live`` hid N > 0 done lanes, ``operator: N decision``
    when N > 0 and one ``flags:`` line per flagged lane. ``<project>: no lane cards``
    stands in for the rows only when the project has no cards at all.

    The attention recommendation comes last; diagnostics and ``summary_line`` follow
    (the CLI prints them). Every line is ASCII.
    """
    collapsed = set(report.collapsed_idle)
    table = [
        [
            _cells(block.project, row)
            for row in block.lanes
            if f"{block.project}/{row.lane}" not in collapsed
        ]
        for block in report.projects
    ]
    widths = [len(name) for name in COLUMNS[:-1]]
    for cells in (cells for block_rows in table for cells in block_rows):
        widths = [max(width, len(cell)) for width, cell in zip(widths, cells, strict=False)]
    lines = [resource_line(report.resources)]
    if collapsed:
        lanes = ", ".join(sorted({label.split("/")[1] for label in collapsed}))
        projects = len({label.split("/")[0] for label in collapsed})
        open_notes = sum(
            row.open
            for block in report.projects
            for row in block.lanes
            if f"{block.project}/{row.lane}" in collapsed
        )
        unblocking = sum(
            row.open_unblocking
            for block in report.projects
            for row in block.lanes
            if f"{block.project}/{row.lane}" in collapsed
        )
        open_summary = f"{open_notes}/{unblocking}r" if unblocking else str(open_notes)
        lines.append(
            f"this window: {lanes} in {projects} projects (idle; {len(collapsed)} rows; "
            f"open: {open_summary}; expand: --expand-idle)"
        )
    groups = session_groups(report)
    bound_count = sum(group["binding"] == "bound" for group in groups)
    lines.append(
        f"sessions: {bound_count} distinct bindings | lane records: {report.lane_count} | "
        "bindings are not process counts; unknown windows are unverified"
    )
    lookup = {f"{b.project}/{r.lane}": (b.project, r) for b in report.projects for r in b.lanes}
    for group in groups:
        labels = group["lanes"]
        assert isinstance(labels, list)
        visible = [label for label in labels if label not in collapsed]
        if not visible:
            continue
        lines.append(f"{group['label']} ({group['host']}; {group['binding']}):")
        lines.append("  " + _join(COLUMNS, widths))
        for label in visible:
            project, row = lookup[label]
            lines.append("  " + _join(_cells(project, row), widths))
    for block in report.projects:
        if not block.lanes and not block.hidden_done:
            lines.append(f"{block.project}: no lane cards")
        if block.hidden_done:
            lines.append(f"done: {block.hidden_done} hidden")
        if block.operator_decisions:
            lines.append(f"operator: {block.operator_decisions} decision")
        lines.extend(
            f"flags: {block.project}/{row.lane} {', '.join(row.flags)}"
            for row in block.lanes
            if row.flags
        )
    attention = report.attention
    lines.extend(
        "coordinator attention: "
        f"P{item.priority} {item.kind} {item.project}/{item.lane} "
        f"evidence={item.evidence} summary={ascii_cell(item.summary)} "
        f"delivery={item.delivery} action={item.action}"
        for item in report.coordinator_attention
    )
    lines.append(
        f"look here first: {attention.project}/{attention.lane} - {attention.reason}"
        if attention is not None
        else "look here first: nothing urgent"
    )
    return lines


def render_coordinator_text(report: Report) -> list[str]:
    """Compact coordinator snapshot. Receipt without a linked reply stays visible.

    The full, unchanged evidence remains in JSON and ``render_text`` (``--details``).
    A blocker is called active only when its sender's current card says blocked.
    """
    rows = {(block.project, row.lane): row for block in report.projects for row in block.lanes}
    decisions: list[CoordinatorAttention] = []
    blockers: list[CoordinatorAttention] = []
    actions: list[CoordinatorAttention] = []
    received: list[CoordinatorAttention] = []
    for item in report.coordinator_attention:
        row = rows.get((item.project, item.lane))
        if item.kind == "operator-decision":
            decisions.append(item)
        elif item.kind == "blocking-gate" and row is not None and row.state == "blocked":
            blockers.append(item)
        elif item.delivery == "acknowledged-unreplied":
            received.append(item)
        else:
            actions.append(item)

    def entry(item: CoordinatorAttention) -> list[str]:
        return [
            f"    {item.project}/{item.lane}: {ascii_cell(item.summary, 72)}",
            f"      {item.evidence} | {item.delivery} | {item.action}",
        ]

    lines = [
        resource_line(report.resources),
        "Coordinator attention (one read; receipt is not resolution):",
    ]
    for title, items in (
        ("Operator decisions", decisions),
        ("Active blockers (sender card blocked)", blockers),
        ("Coordinator actions", actions),
    ):
        lines.append(f"  {title}: {len(items)}")
        for item in items:
            lines.extend(entry(item))
    kinds = {
        kind: sum(item.kind == kind for item in received)
        for kind in ("gate-handoff", "blocking-gate")
    }
    lines.append(
        "  Received without linked reply: "
        f"{len(received)} ({kinds['gate-handoff']} handoffs, "
        f"{kinds['blocking-gate']} blocker notes); "
        "unresolved evidence, expand with --details"
    )
    lines.append(
        "  Safe-close candidates: use the lane-status verifier; status has no launcher proof"
    )

    groups = session_groups(report)
    bound_count = sum(group["binding"] == "bound" for group in groups)
    lines.append(
        f"Lane snapshot: {report.lane_count} records, {bound_count} bindings "
        "(bindings are not process counts; unknown windows are unverified)"
    )
    collapsed = set(report.collapsed_idle)
    for group in groups:
        labels = group["lanes"]
        assert isinstance(labels, list)
        visible = [label for label in labels if label not in collapsed]
        if not visible:
            continue
        lines.append(f"  {group['label']} ({group['host']}; {group['binding']}):")
        for label in visible:
            project, lane = label.split("/", 1)
            row = rows[(project, lane)]
            age = format_age(row.age_minutes)
            open_count = format_open(row)
            wait = f"; waits {ascii_cell(row.waiting_on, 35)}" if row.waiting_on else ""
            lines.append(
                f"    {label} [{row.state}] {age}, {open_count} open{wait}; "
                f"card: {ascii_cell(row.item, 64, keep_tail=True)}"
            )
    if collapsed:
        lines.append(f"  Idle caller rows collapsed: {len(collapsed)}; expand with --expand-idle")
    hidden_done = sum(block.hidden_done for block in report.projects)
    if hidden_done:
        lines.append(f"  Done card rows hidden: {hidden_done}")
    attention = report.attention
    lines.append(
        f"Lane urgency: {attention.project}/{attention.lane} - {attention.reason}"
        if attention is not None
        else "Lane urgency: none observed"
    )
    lines.append("Full note and lane evidence: --details or --json")
    return lines


def summary_line(report: Report) -> str:
    """The last text line: projects, visible lanes, any collapsed idle rows, diagnostics."""
    hidden = len(report.collapsed_idle)
    idle = f"idle: {hidden} collapsed  " if hidden else ""
    return (
        f"projects: {len(report.projects)}  lanes: {report.lane_count - hidden}  {idle}"
        f"diagnostics: {len(report.diagnostics)}"
    )


# --- JSON (D54) ---------------------------------------------------------------------------


def _record(kind: str, *values: object) -> dict[str, object]:
    return dict(zip(JSON_KEYS[kind], values, strict=True))


def to_json(report: Report) -> dict[str, object]:
    """The D54 object; every object is built from JSON_KEYS."""
    projects = [
        _record(
            "project",
            block.project,
            [
                _record(
                    "lane",
                    row.lane,
                    row.host,
                    row.state,
                    row.item,
                    row.updated_utc,
                    row.age_minutes,
                    row.window,
                    row.open,
                    row.open_unblocking,
                    list(row.flags),
                    row.waiting_on,
                    row.evidence.window_state,
                    row.evidence.activity,
                    row.evidence.transcript_age_minutes,
                    row.evidence.open_subagents,
                    row.evidence.child_processes,
                    row.evidence.reason,
                )
                for row in block.lanes
            ],
            block.done_count,
            block.operator_decisions,
        )
        for block in report.projects
    ]
    shown = [str(diagnostic) for diagnostic in report.diagnostics[:DIAGNOSTIC_LINES]]
    attention = report.attention
    return _record(
        "status",
        SCHEMA,
        report.generated_utc,
        _record("scope", list(report.caller), [block.project for block in report.projects]),
        projects,
        _record("diagnostics", shown, len(report.diagnostics)),
        list(report.collapsed_idle),
        _record(
            "resources",
            report.resources.free_memory_bytes,
            [
                _record("queue_holder", h.project, h.resource, h.lane, h.deadline_utc)
                for h in report.resources.queue_holders
            ],
            report.resources.queues_complete,
        ),
        None
        if attention is None
        else _record("attention", attention.project, attention.lane, attention.reason),
        [
            _record(
                "coordinator_attention",
                item.priority,
                item.kind,
                item.project,
                item.lane,
                item.evidence,
                item.summary,
                item.delivery,
                item.action,
            )
            for item in report.coordinator_attention
        ],
        session_groups(report),
    )


def render_json(report: Report) -> list[str]:
    """``to_json`` as indented, ASCII-only JSON lines."""
    return json.dumps(to_json(report), indent=2, ensure_ascii=True).splitlines()
