"""The store-only checker of the cross-host smoke (plan Step 3).

A smoke run's verdict comes from the store files alone: the notes of one synthetic
project, their ``reply_to`` links, the ack files with their readers, and the lane
cards. Nothing a host or a model says is evidence. The checker only reads, through
the same ``Store`` scans the CLI uses; it never writes, moves or deletes anything.

The exchange (PHASES, in order; each phase is one headless host session):

1. claude-lane sets its card (host ``claude``) and posts a question to codex-lane.
2. codex-lane sets its card (host ``codex``), acknowledges that question and posts an
   answer whose ``reply_to`` names it.
3. claude-lane acknowledges the answer.
4. codex-lane posts a question to claude-lane.
5. claude-lane acknowledges it and posts an answer whose ``reply_to`` names it.
6. codex-lane acknowledges that answer.

Invariants:

- A note's role is fixed by its sender, its recipient and whether it replies at all.
  A ``reply_to`` that names the wrong note is reported against that role; it never
  makes the note match another role.
- An ack counts only as the file ``<note id>--<reader>.md`` of the expected reader,
  whose contents name the same reader (a mismatch is a store diagnostic).
- A check ``through`` the first N phases covers only what those phases produce and
  ignores anything else. A complete check (all phases) also fails on every note, ack
  or card the exchange does not expect.
- Every store diagnostic (an invalid, oversize or stray file) is a failure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from passing_notes import protocol
from passing_notes.protocol import REPLY_TO_NONE, Card, Note, ProtocolError
from passing_notes.store import Store, StoreError

CLAUDE_LANE: Final = "claude-lane"
CODEX_LANE: Final = "codex-lane"
# The host each lane runs on, as its card must name it.
LANE_HOSTS: Final[Mapping[str, str]] = MappingProxyType(
    {CLAUDE_LANE: "claude", CODEX_LANE: "codex"}
)
# A note that opens an exchange asks for a reply; the reply asks for an ack.
QUESTION_KIND: Final = "question"
QUESTION_NEEDS: Final = "reply"
ANSWER_KIND: Final = "answer"
ANSWER_NEEDS: Final = "ack"


@dataclass(frozen=True)
class NoteRole:
    """One note the exchange expects. ``reply_to`` names the role it answers, if any."""

    name: str
    sender: str
    recipient: str
    reply_to: str | None = None

    @property
    def kind(self) -> str:
        return ANSWER_KIND if self.reply_to is not None else QUESTION_KIND

    @property
    def needs(self) -> str:
        return ANSWER_NEEDS if self.reply_to is not None else QUESTION_NEEDS

    def matches(self, note: Note) -> bool:
        """True for a note from ``sender`` to ``recipient`` that replies iff this role does.

        Where the ``reply_to`` points is checked separately, so a broken link is
        reported against this role rather than hiding the note.
        """
        replies = note.reply_to != REPLY_TO_NONE
        return (
            note.from_lane == self.sender
            and note.to == self.recipient
            and replies == (self.reply_to is not None)
        )


@dataclass(frozen=True)
class Phase:
    """One headless host session: what its lane writes to the store.

    ``acks`` is the note the lane acknowledges after finding it in its inbox; a reply in
    ``posts`` answers that same note.
    """

    name: str
    lane: str
    sets_card: bool = False
    acks: NoteRole | None = None
    posts: NoteRole | None = None

    @property
    def host(self) -> str:
        return LANE_HOSTS[self.lane]


CLAUDE_QUESTION: Final = NoteRole("claude-question", CLAUDE_LANE, CODEX_LANE)
CODEX_ANSWER: Final = NoteRole("codex-answer", CODEX_LANE, CLAUDE_LANE, CLAUDE_QUESTION.name)
CODEX_QUESTION: Final = NoteRole("codex-question", CODEX_LANE, CLAUDE_LANE)
CLAUDE_ANSWER: Final = NoteRole("claude-answer", CLAUDE_LANE, CODEX_LANE, CODEX_QUESTION.name)

PHASES: Final[tuple[Phase, ...]] = (
    Phase("claude-opens", CLAUDE_LANE, sets_card=True, posts=CLAUDE_QUESTION),
    Phase("codex-answers", CODEX_LANE, sets_card=True, acks=CLAUDE_QUESTION, posts=CODEX_ANSWER),
    Phase("claude-closes", CLAUDE_LANE, acks=CODEX_ANSWER),
    Phase("codex-opens", CODEX_LANE, posts=CODEX_QUESTION),
    Phase("claude-answers", CLAUDE_LANE, acks=CODEX_QUESTION, posts=CLAUDE_ANSWER),
    Phase("codex-closes", CODEX_LANE, acks=CLAUDE_ANSWER),
)


@dataclass(frozen=True)
class StoreCheck:
    """The checker's verdict. Passed iff ``failures`` is empty.

    ``note_ids`` maps each role found exactly once to its note id, ``ack_files`` lists
    the expected ack file names found, and ``cards`` maps each expected lane whose card
    names the right host to that host.
    """

    failures: tuple[str, ...]
    note_ids: Mapping[str, str]
    ack_files: tuple[str, ...]
    cards: Mapping[str, str]

    @property
    def passed(self) -> bool:
        return not self.failures


def check_store(root: Path, project: str, *, through: int = len(PHASES)) -> StoreCheck:
    """Check what the first ``through`` PHASES must have written to ``project``.

    ``root`` is the store root. With ``through == len(PHASES)`` (the default) the check
    is complete: anything in the project that the exchange does not expect fails it.
    """
    if not 0 <= through <= len(PHASES):
        raise ValueError(f"through must be 0..{len(PHASES)}, not {through}")
    phases = PHASES[:through]
    try:
        protocol.check_project_id(project)
        store = Store(root)
        notes, note_diagnostics = store.scan_notes(project)
        acked, ack_diagnostics = store.scan_acks(project)
        cards, card_diagnostics = store.scan_cards(project)
    except ProtocolError as exc:
        return StoreCheck((f"invalid project: {exc}",), {}, (), {})
    except StoreError as exc:
        return StoreCheck((f"store unusable: {exc.code}: {exc.message}",), {}, (), {})
    failures = [
        f"store diagnostic: {diagnostic}"
        for diagnostic in (*note_diagnostics, *ack_diagnostics, *card_diagnostics)
    ]
    note_ids, claimed = _match_roles(phases, notes, failures)
    ack_files, expected_acks = _check_acks(phases, note_ids, acked, failures)
    found_cards = _check_cards(phases, cards, failures)
    if through == len(PHASES):
        _check_unexpected(phases, notes, claimed, acked, expected_acks, cards, failures)
    return StoreCheck(
        tuple(failures), MappingProxyType(note_ids), tuple(ack_files), MappingProxyType(found_cards)
    )


def _match_roles(
    phases: Sequence[Phase], notes: Sequence[Note], failures: list[str]
) -> tuple[dict[str, str], set[str]]:
    """Each posted role's one note id, and the ids of every note matching any role.

    Roles are taken in phase order, so a reply's target role is resolved first.
    """
    note_ids: dict[str, str] = {}
    claimed: set[str] = set()
    for phase in phases:
        role = phase.posts
        if role is None:
            continue
        found = sorted((note for note in notes if role.matches(note)), key=lambda n: n.id)
        claimed.update(note.id for note in found)
        shape = f"from {role.sender} to {role.recipient}"
        if role.reply_to is not None:
            shape += f" replying to {role.reply_to}"
        if not found:
            failures.append(f"{role.name}: no note {shape}")
            continue
        if len(found) > 1:
            ids = ", ".join(note.id for note in found)
            failures.append(f"{role.name}: {len(found)} notes {shape}, expected one: {ids}")
            continue
        note = found[0]
        note_ids[role.name] = note.id
        if role.reply_to is not None:
            target = note_ids.get(role.reply_to)
            if target is None or note.reply_to != target:
                failures.append(
                    f"{role.name}: reply_to is {note.reply_to}, not the {role.reply_to} note "
                    f"({target or 'missing'})"
                )
    return note_ids, claimed


def _check_acks(
    phases: Sequence[Phase],
    note_ids: Mapping[str, str],
    acked: set[tuple[str, str]],
    failures: list[str],
) -> tuple[list[str], set[tuple[str, str]]]:
    """The expected ack file names found, and every expected (note id, reader) key."""
    found: list[str] = []
    expected: set[tuple[str, str]] = set()
    for phase in phases:
        role = phase.acks
        if role is None:
            continue
        reader = phase.lane
        note_id = note_ids.get(role.name)
        if note_id is None:
            failures.append(f"ack of {role.name} by {reader}: no {role.name} note to acknowledge")
            continue
        expected.add((note_id, reader))
        name = protocol.ack_file_name(note_id, reader)
        if (note_id, reader) in acked:
            found.append(name)
        else:
            failures.append(f"ack of {role.name} by {reader}: no ack file {name}")
    return found, expected


def _check_cards(
    phases: Sequence[Phase], cards: Sequence[Card], failures: list[str]
) -> dict[str, str]:
    """Lane to host for every expected card that names its lane's host."""
    by_lane = {card.lane: card for card in cards}
    found: dict[str, str] = {}
    for phase in phases:
        if not phase.sets_card:
            continue
        card = by_lane.get(phase.lane)
        if card is None:
            failures.append(f"card of {phase.lane}: missing")
        elif card.host != phase.host:
            failures.append(f"card of {phase.lane}: host is {card.host}, expected {phase.host}")
        else:
            found[phase.lane] = card.host
    return found


def _check_unexpected(
    phases: Sequence[Phase],
    notes: Sequence[Note],
    claimed: set[str],
    acked: set[tuple[str, str]],
    expected_acks: set[tuple[str, str]],
    cards: Sequence[Card],
    failures: list[str],
) -> None:
    """Fail on every note, ack and card that no phase produces."""
    for note in sorted(notes, key=lambda n: n.id):
        if note.id not in claimed:
            failures.append(f"unexpected note {note.id} from {note.from_lane} to {note.to}")
    for note_id, reader in sorted(acked - expected_acks):
        failures.append(f"unexpected ack {protocol.ack_file_name(note_id, reader)}")
    card_lanes = {phase.lane for phase in phases if phase.sets_card}
    for card in cards:
        if card.lane not in card_lanes:
            failures.append(f"unexpected card of {card.lane}")
