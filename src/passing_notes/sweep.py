"""``passing-notes status --sweep``: what a coordinator should tidy, one row per finding
(plan D62-D64).

The rows are built from the read model ``status.collect`` already made: its lane rows,
its scoped project reads (validated note headers, acknowledgement names, raw doorbell
bindings) and its resource snapshot. Nothing here parses a note or decides scope; the
caller passes only the reads its coordinator rule admits. A row's evidence is note
ids, counts or a card age, never a note body or subject.

The CLI never reads process trees or launcher files (D64): a ``done-window-open`` row
says only that the host still lists the session a done card was bound to. The
lane-status skill turns it into ``safe-close`` or ``operator-close`` with the close
helper's public inventory. Reads only; nothing is written.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from passing_notes import protocol, queue, wake
from passing_notes.liveness import Observer
from passing_notes.protocol import MAIN_LANE, RECIPIENT_ALL, WORKSPACE_PROJECT, Note
from passing_notes.store import Store

if TYPE_CHECKING:  # status imports this module; the types are for annotations only
    from passing_notes.status import _ProjectRead

# The finding kinds, in output order; the one source of truth, pinned by docs/cli.md.
SWEEP_KINDS: Final = (
    "memory-below-floor",
    "windowless-lane",
    "idle-lane",
    "stale-card",
    "done-window-open",
    "operator-decision-old",
    "backlog-bulk",
    "backlog-direct",
    "stand-ins-over-limit",
)
# Each kind's action word (D63). The lane-status skill adds ``operator-close`` (D64).
SWEEP_ACTIONS: Final[Mapping[str, str]] = MappingProxyType(
    dict(
        zip(
            SWEEP_KINDS,
            (
                "stop-process",
                "stand-in-close",
                "close-request",
                "status-request",
                "safe-close",
                "surface-to-operator",
                "bulk-ack",
                "disposition",
                "open-project-main",
            ),
            strict=True,
        )
    )
)
MEMORY_FLOOR_DEFAULT_GIB: Final = 4
OPERATOR_DECISION_OLD_HOURS: Final = 24
BACKLOG_BULK_HOURS: Final = 48
# D4: at most this many stand-in mains (projects other than dev) per Claude session.
STAND_IN_LIMIT: Final = 3
# The project and lane of a row that belongs to no single project or lane.
NO_SCOPE: Final = "-"
DIRECT_NEEDS: Final = ("reply", "ack")
BULK_NEEDS: Final = "fyi"
IDLE_STATE: Final = "idle"
# liveness.Observer reasons that mean the host does not list the bound session.
_NOT_LISTED: Final = frozenset({"session-not-listed", "host-unavailable"})
_SESSION_NOT_LISTED: Final = "session-not-listed"


@dataclass(frozen=True)
class SweepRow:
    """One finding. ``action`` always comes from SWEEP_ACTIONS; ``age_minutes`` is
    None when no age applies (memory, stand-ins)."""

    kind: str
    project: str
    lane: str
    evidence: str
    age_minutes: int | None

    @property
    def action(self) -> str:
        return SWEEP_ACTIONS[self.kind]


def _age(now: datetime, utc: str, field: str) -> float:
    """Seconds since ``utc``; compared in seconds, as status compares --stale-after."""
    return (now - protocol.parse_utc(utc, field)).total_seconds()


def _minutes(seconds: float) -> int:
    return queue.whole_minutes(timedelta(seconds=seconds))


def _host_lists(observer: Observer, binding: wake.Binding, now: datetime) -> bool:
    """The same Observer status uses: True when the host lists the bound session."""
    try:
        evidence = observer.observe(binding, now)
    except wake.EVIDENCE_ERRORS:
        return False
    return evidence.reason not in _NOT_LISTED


def _lane_rows(
    read: _ProjectRead, now: datetime, stale_after_hours: int, observer: Observer
) -> list[SweepRow]:
    """At most one row per lane: done-window-open, else windowless-lane, else idle-lane,
    else stale-card."""
    from passing_notes import status  # deferred: status imports this module

    project = read.block.project
    found: list[SweepRow] = []
    for row in read.block.lanes:
        seconds = _age(now, row.updated_utc, "updated_utc")
        minutes = _minutes(seconds)
        if row.state == status.DONE_STATE:
            # The raw binding: status's stale-binding filter rules out every done card.
            binding = read.bindings.get(row.lane)
            if (
                binding is not None
                and binding.host == wake.HOST_CLAUDE
                and _host_lists(observer, binding, now)
            ):
                found.append(
                    SweepRow(
                        "done-window-open",
                        project,
                        row.lane,
                        "card done; bound session still listed",
                        minutes,
                    )
                )
            continue
        # Live as status means it (a binding that is not stale), and not observably gone.
        live = row.session_id is not None and row.evidence.reason != _SESSION_NOT_LISTED
        old = seconds > stale_after_hours * 3600
        if not live and old:
            reason = row.evidence.reason
            found.append(
                SweepRow(
                    "windowless-lane",
                    project,
                    row.lane,
                    f"card {row.state}; no live binding ({reason})",
                    minutes,
                )
            )
        elif live and old and row.state == IDLE_STATE:
            found.append(
                SweepRow(
                    "idle-lane",
                    project,
                    row.lane,
                    f"card idle; window {row.evidence.window_state}",
                    minutes,
                )
            )
        elif live and status.FLAG_STALE in row.flags:
            found.append(
                SweepRow(
                    "stale-card",
                    project,
                    row.lane,
                    f"card {row.state}; older than {stale_after_hours}h",
                    minutes,
                )
            )
    return found


def _note_rows(
    read: _ProjectRead, resolved: AbstractSet[tuple[str, str]], now: datetime
) -> list[SweepRow]:
    from passing_notes import status  # deferred: status imports this module

    project = read.block.project
    found: list[SweepRow] = []
    by_id = {note.id: note for note in read.current}
    answered = {note_id for p, note_id in resolved if p == project}
    for note_id in sorted(read.decisions - answered):
        note = by_id.get(note_id)
        if note is None:
            continue
        seconds = _age(now, note.created_utc, "created_utc")
        if seconds > OPERATOR_DECISION_OLD_HOURS * 3600:
            found.append(
                SweepRow("operator-decision-old", project, MAIN_LANE, note.id, _minutes(seconds))
            )
    unacked = [
        note
        for note in read.current
        if protocol.is_addressed_to(note, project, MAIN_LANE)
        and (note.id, MAIN_LANE) not in read.acked
    ]
    bulk: list[tuple[float, Note]] = []
    for note in unacked:
        seconds = _age(now, note.created_utc, "created_utc")
        if (
            note.needs != status.OPERATOR_DECISION_NEEDS
            and (note.needs == BULK_NEEDS or note.to == RECIPIENT_ALL)
            and seconds > BACKLOG_BULK_HOURS * 3600
        ):
            bulk.append((seconds, note))
    if bulk:
        seconds, oldest = max(bulk, key=lambda entry: (entry[0], entry[1].id))
        found.append(
            SweepRow(
                "backlog-bulk",
                project,
                MAIN_LANE,
                f"{len(bulk)} notes; oldest {oldest.id}",
                _minutes(seconds),
            )
        )
    for note in unacked:
        if note.to == MAIN_LANE and note.needs in DIRECT_NEEDS:
            seconds = _age(now, note.created_utc, "created_utc")
            found.append(SweepRow("backlog-direct", project, MAIN_LANE, note.id, _minutes(seconds)))
    return found


def _stand_in_rows(store: Store, reads: Sequence[_ProjectRead]) -> list[SweepRow]:
    """One row per Claude session bound as a live ``main`` in more than STAND_IN_LIMIT
    projects other than dev. The session id never leaves this function."""
    projects: dict[str, list[str]] = {}
    for read in reads:
        project = read.block.project
        binding = read.bindings.get(MAIN_LANE)
        if project == WORKSPACE_PROJECT or binding is None or binding.host != wake.HOST_CLAUDE:
            continue
        try:
            if wake.binding_stale_reason(store, project, MAIN_LANE, binding) is not None:
                continue
        except wake.EVIDENCE_ERRORS:
            continue
        projects.setdefault(binding.session_id, []).append(project)
    return [
        SweepRow(
            "stand-ins-over-limit",
            NO_SCOPE,
            MAIN_LANE,
            f"one session is main in {len(names)} projects: {', '.join(sorted(names))}",
            None,
        )
        for names in projects.values()
        if len(names) > STAND_IN_LIMIT
    ]


def _memory_rows(free_memory_bytes: int | None, floor_gib: float) -> list[SweepRow]:
    """Unknown memory is no finding: unknown is never below the floor."""
    if free_memory_bytes is None or free_memory_bytes >= floor_gib * 2**30:
        return []
    return [
        SweepRow(
            "memory-below-floor",
            NO_SCOPE,
            NO_SCOPE,
            f"free {free_memory_bytes / 2**30:.1f} GiB; floor {floor_gib:g} GiB",
            None,
        )
    ]


def build(
    store: Store,
    reads: Sequence[_ProjectRead],
    *,
    resolved: AbstractSet[tuple[str, str]],
    free_memory_bytes: int | None,
    include_memory: bool,
    now: datetime,
    stale_after_hours: int,
    memory_floor_gib: float,
    observer: Observer,
) -> tuple[SweepRow, ...]:
    """Every finding in ``reads`` (already scoped by the caller), sorted by kind, then
    project, lane and evidence. ``resolved`` is status's set of answered requests."""
    rows = _memory_rows(free_memory_bytes, memory_floor_gib) if include_memory else []
    for read in reads:
        rows.extend(_lane_rows(read, now, stale_after_hours, observer))
        rows.extend(_note_rows(read, resolved, now))
    rows.extend(_stand_in_rows(store, reads))
    order = {kind: rank for rank, kind in enumerate(SWEEP_KINDS)}
    return tuple(
        sorted(rows, key=lambda row: (order[row.kind], row.project, row.lane, row.evidence))
    )
