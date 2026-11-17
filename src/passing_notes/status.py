"""``passing-notes status``: every lane of one project, or of every project the caller
may see, as one read-only table (plan P31-P34, D47-D57).

The read model is collected once and rendered as text or JSON. It reads cards, note
headers (through the validated ``Store.scan_notes``; no second parser), acknowledgement
file names and doorbell bindings. It never prints, returns or keeps a note body or a
subject, and it writes nothing: no ack, card, ring or folder.

Who may see what is the topology's rule (docs/protocol.md#topology, "Seeing"); this
module applies it and keeps no copy of its wording. The caller is the Claude window
named by ``CLAUDE_CODE_SESSION_ID``, matched against Claude bindings
(``hook.session_bindings``) that are still live (``wake.binding_stale_reason`` is
None). The workspace main (lane ``main`` of project ``dev``) sees every project; any
other live binding in project P sees P; several live bindings see the union; an
unbound caller may ask for any project or, explicitly, for all of them. The check is
soft: the CLI cannot prove who types the command.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final

from passing_notes import hook, protocol, queue, wake
from passing_notes.protocol import (
    CARD_STATES,
    DIAGNOSTIC_LINES,
    MAIN_LANE,
    NOTE_SUFFIX,
    RECIPIENT_OPERATOR,
    WORKSPACE_PROJECT,
    Card,
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

# The D54 JSON shape: the keys of each object, in output order. The one source of truth
# for the shape; the renderer builds every object from these tuples and docs/cli.md is
# pinned against them. Changes are additive only.
JSON_KEYS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "status": ("schema", "generated_utc", "scope", "projects", "diagnostics"),
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
        ),
        "diagnostics": ("shown", "total"),
    }
)

COLUMNS: Final = ("PROJECT", "LANE", "STATE", "HOST", "WINDOW", "UPDATED", "OPEN", "WORKING ON")
COLUMN_GAP: Final = "  "
ITEM_WIDTH: Final = 40
ELLIPSIS: Final = "..."
# Card states a stale or doorbell-less lane matters for (D50).
ACTIVE_LIKE_STATES: Final = ("active", "waiting", "blocked")
DONE_STATE: Final = "done"
# The needs that make an open note unblocking (the "r" in OPEN's N/Mr).
UNBLOCKING_NEEDS: Final = ("reply", "operator-decision")
OPERATOR_DECISION_NEEDS: Final = "operator-decision"
# A note to either recipient waits on the operator until the project's main acks it.
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


def caller_scope(store: Store, session_id: str) -> Scope:
    """The scope of the window ``session_id`` names; unbound if it names none.

    An empty or malformed id, or one with no live Claude binding, is unbound. A binding
    that ``wake.binding_stale_reason`` rules out grants nothing.
    """
    if not session_id:
        return Scope(())
    try:
        bindings = hook.session_bindings(store, session_id)
    except ProtocolError:  # not a Claude session id
        return Scope(())
    live = {
        (b.project, b.lane)
        for b in bindings
        if wake.binding_stale_reason(store, b.project, b.lane, b) is None
    }
    return Scope(tuple(sorted(live)))


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


@dataclass(frozen=True)
class ProjectBlock:
    project: str
    lanes: tuple[LaneRow, ...]
    operator_decisions: int

    @property
    def done_count(self) -> int:
        return sum(1 for row in self.lanes if row.state == DONE_STATE)


@dataclass(frozen=True)
class Report:
    generated_utc: str
    caller: tuple[str, ...]
    projects: tuple[ProjectBlock, ...]
    diagnostics: tuple[Diagnostic, ...]

    @property
    def lane_count(self) -> int:
        return sum(len(block.lanes) for block in self.projects)


def list_projects(store: Store) -> tuple[list[str], list[Diagnostic]]:
    """D55: the sorted union of project folder names, plus a diagnostic per other name.

    Only folder names are listed here; nothing is read or created.
    """
    found: set[str] = set()
    diagnostics: list[Diagnostic] = []
    for kind in PROJECT_KINDS:
        folder = store._store_folder(kind)  # refuses a link
        try:
            with os.scandir(folder) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
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
    open_notes: Sequence[str],
    now: datetime,
    stale_after_hours: int,
) -> LaneRow:
    age = now - protocol.parse_utc(card.updated_utc, "updated_utc")
    binding = bindings.get(card.lane)
    flags: list[str] = []
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
        window=WINDOW_NOT_BOUND if binding is None else WINDOW_BOUND,
        open=len(open_notes),
        open_unblocking=sum(1 for needs in open_notes if needs in UNBLOCKING_NEEDS),
        flags=tuple(flags),
    )


def _project_block(
    store: Store, project: str, now: datetime, stale_after_hours: int
) -> tuple[ProjectBlock, list[Diagnostic]]:
    """One project's rows and diagnostics. Note bodies and subjects stay in this call."""
    cards, card_diagnostics = store.scan_cards(project)
    notes, note_diagnostics = store.scan_notes(project)
    acked, ack_diagnostics = store.scan_acks(project)
    supersedes = protocol.resolve_supersedes(notes)
    supersedes_diagnostics = [
        Diagnostic(f"{MESSAGES_DIR}/{project}/{note_id}{NOTE_SUFFIX}", reason)
        for note_id, reason in supersedes.ignored
    ]
    current = [note for note in notes if note.id not in supersedes.hidden]
    bindings = {binding.lane: binding for binding in wake.list_bindings(store, project)}
    rows = [
        _lane_row(
            store,
            card,
            bindings,
            [
                note.needs
                for note in current
                if protocol.is_addressed_to(note, project, card.lane)
                and (note.id, card.lane) not in acked
            ],
            now,
            stale_after_hours,
        )
        for card in cards
    ]
    rows.sort(key=lambda row: (_state_rank(row.state), row.lane))
    decisions = sum(
        1
        for note in current
        if note.needs == OPERATOR_DECISION_NEEDS
        and note.to in OPERATOR_RECIPIENTS
        and (note.id, MAIN_LANE) not in acked
    )
    diagnostics = card_diagnostics + note_diagnostics + ack_diagnostics + supersedes_diagnostics
    return ProjectBlock(project, tuple(rows), decisions), diagnostics


def collect(
    store: Store,
    scope: Scope,
    project: str | None,
    *,
    now: datetime,
    stale_after_hours: int = STALE_AFTER_DEFAULT_HOURS,
) -> Report:
    """The report for ``project``, or for every project (None). Reads only.

    The caller has passed ``refusal`` already.
    """
    diagnostics = store.scan_flat()
    if project is None:
        projects, listing_diagnostics = list_projects(store)
        diagnostics += listing_diagnostics
    else:
        projects = [protocol.check_project_id(project, "--project")]
    blocks: list[ProjectBlock] = []
    for name in projects:
        block, block_diagnostics = _project_block(store, name, now, stale_after_hours)
        blocks.append(block)
        diagnostics += block_diagnostics
    return Report(
        generated_utc=protocol.format_utc(now),
        caller=tuple(scope.labels()),
        projects=tuple(blocks),
        diagnostics=tuple(diagnostics),
    )


# --- Text (D47) ---------------------------------------------------------------------------


def ascii_cell(text: str, width: int | None = None) -> str:
    """``text`` as printable ASCII, each other character shown as an escape such as
    ``\\xe9`` (Python's ``ascii`` form); cut to ``width`` characters ending in ELLIPSIS when longer.

    An escape is never split by the cut.
    """
    pieces = [
        ch if ch.isascii() and not protocol.is_unsafe_char(ch) else ascii(ch)[1:-1] for ch in text
    ]
    whole = "".join(pieces)
    if width is None or len(whole) <= width:
        return whole
    kept: list[str] = []
    used = 0
    for piece in pieces:
        if used + len(piece) > width - len(ELLIPSIS):
            break
        kept.append(piece)
        used += len(piece)
    return "".join(kept) + ELLIPSIS


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
        row.window,
        format_age(row.age_minutes),
        format_open(row),
        ascii_cell(row.item, ITEM_WIDTH),
    ]


def _join(cells: Sequence[str], widths: Sequence[int]) -> str:
    padded = [cell.ljust(width) for cell, width in zip(cells[:-1], widths, strict=False)]
    return COLUMN_GAP.join([*padded, cells[-1]]).rstrip()


def render_text(report: Report) -> list[str]:
    """The header, then each project's block: its rows (done lanes last), then
    ``operator: N decision`` when N > 0 and one ``flags:`` line per flagged lane.

    The diagnostics and the ``summary_line`` follow (the CLI prints them). Every line
    is ASCII.
    """
    table = [[_cells(block.project, row) for row in block.lanes] for block in report.projects]
    widths = [len(name) for name in COLUMNS[:-1]]
    for cells in (cells for block_rows in table for cells in block_rows):
        widths = [max(width, len(cell)) for width, cell in zip(widths, cells, strict=False)]
    lines = [_join(COLUMNS, widths)]
    for block, block_rows in zip(report.projects, table, strict=True):
        if not block_rows:
            lines.append(f"{block.project}: no lane cards")
        lines.extend(_join(cells, widths) for cells in block_rows)
        if block.operator_decisions:
            lines.append(f"operator: {block.operator_decisions} decision")
        lines.extend(
            f"flags: {block.project}/{row.lane} {', '.join(row.flags)}"
            for row in block.lanes
            if row.flags
        )
    return lines


def summary_line(report: Report) -> str:
    """The last text line: ``projects: N  lanes: N  diagnostics: N``."""
    return (
        f"projects: {len(report.projects)}  lanes: {report.lane_count}  "
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
                )
                for row in block.lanes
            ],
            block.done_count,
            block.operator_decisions,
        )
        for block in report.projects
    ]
    shown = [str(diagnostic) for diagnostic in report.diagnostics[:DIAGNOSTIC_LINES]]
    return _record(
        "status",
        SCHEMA,
        report.generated_utc,
        _record("scope", list(report.caller), [block.project for block in report.projects]),
        projects,
        _record("diagnostics", shown, len(report.diagnostics)),
    )


def render_json(report: Report) -> list[str]:
    """``to_json`` as indented, ASCII-only JSON lines."""
    return json.dumps(to_json(report), indent=2, ensure_ascii=True).splitlines()
