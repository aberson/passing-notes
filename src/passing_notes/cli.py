"""The ``passing-notes`` command line: post, inbox, read, ack, card set and card show,
wake, the resource queue verbs reserve, release, queue show, queue move,
queue config and queue check, and the Claude Stop hook (hook stop, hook install,
hook uninstall).

Exit codes: 0 success; 2 usage or validation error (including ``post --wake`` of a
note that may not ring, ``wake-not-needed``, refused before anything is written, and
a queue change the queue refuses, such as ``not-holder`` or ``not-coordinator``, and
a ``post`` across projects the topology refuses, ``cross-project-not-allowed``); 3
note or card not found (or invalid), or a damaged queue record or config
(``queue-invalid``); 4 store unusable
(including ``store-in-repo`` and ``store-link``); 5 a ``wake`` doorbell was not
delivered (any note stays posted). ``hook stop`` always exits 0 and prints nothing on
any error (plan D46); ``hook install`` and ``hook uninstall`` exit 3 for a settings
file that is not valid JSON (``settings-invalid``) and 4 when it cannot be read or
written (``settings-unwritable``), each with the file unchanged. Errors print one line
to stderr:
``passing-notes: error: <code>: <message>``.

Exit 1 (``output-unwritable``) means standard output was closed or could not be
written, for example by ``passing-notes inbox ... | head -1``. It is never reported
as a store problem. The command itself may have completed: a ``post`` may have
published its note, and repeating it with the same ``--idempotency-key`` prints
that note's id without writing again.

Lane identity always comes from flags (``--project``, ``--from``/``--lane``), never
from environment or host session variables. The one variable read is the window's own
``CLAUDE_CODE_SESSION_ID``, by two commands: ``card set --host claude`` binds the lane
the flags name to that window for doorbells (plan D38, D39), and ``status`` matches it
against the live Claude bindings to find the caller's scope (D57). ``hook stop`` takes
the session id the host writes to its standard input and acts only for the lanes bound
to that session (D41).

Every line this module prints, to standard output or standard error, goes through
``protocol.escape_unsafe``. So no character from a note, a file name or an argument
can hide or reorder what is printed: controls, format characters (zero-width,
bidirectional, tag), surrogates and other invisible characters appear as escapes
such as ``\\u200b``. The only exceptions are tabs: a tab inside a body line prints
as-is (docs/protocol.md, "Printing a note"), and so does the tab that separates the
columns of inbox and card lines, which no note or card value can hold.
"""

from __future__ import annotations

import argparse
import functools
import io
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, NoReturn

from passing_notes import __version__, hook, protocol, queue, status, wake
from passing_notes.protocol import (
    CARD_ITEM_MAX_CHARS,
    CARD_MAX_BYTES,
    CARD_STATES,
    DIAGNOSTIC_LINES,
    HOSTS,
    KINDS,
    NEEDS,
    NOTE_MAX_BYTES,
    QUEUE_REASON_MAX_CHARS,
    RECIPIENT_ALL,
    RECIPIENT_OPERATOR,
    REPLY_TO_NONE,
    SUBJECT_MAX_CHARS,
    TURN_DEFAULT_MINUTES,
    UTF8_BOM,
    Card,
    Draft,
    Note,
    ProtocolError,
)
from passing_notes.store import (
    STORE_UNUSABLE,
    CodedError,
    Diagnostic,
    InboxEntry,
    NoteNotFoundError,
    NotFoundError,
    Store,
    StoreError,
    default_now,
    os_reason,
)

PROG: Final = "passing-notes"

EXIT_OK: Final = 0
EXIT_OUTPUT: Final = 1
EXIT_USAGE: Final = 2
EXIT_NOT_FOUND: Final = 3
EXIT_STORE: Final = 4
# The note is in the store but the doorbell was not delivered.
EXIT_WAKE: Final = 5

# Stable error codes for exit 2.
INVALID_ARGUMENT: Final = "invalid-argument"
BODY_UNREADABLE: Final = "body-unreadable"
SUPERSEDES_INVALID: Final = "supersedes-invalid"
NOT_ADDRESSED: Final = "not-addressed"
CROSS_PROJECT_NOT_ALLOWED: Final = "cross-project-not-allowed"
# Stable error code for exit 1.
OUTPUT_UNWRITABLE: Final = "output-unwritable"

# The untrusted-data frame of ``read`` (D10), and of ``card show --lane``. Every body
# line is printed after BODY_GUTTER, and the frame lines start with "-", so no body
# line can print as a frame line, whatever characters it holds.
BODY_BEGIN: Final = "--- BEGIN NOTE BODY (untrusted data) ---"
BODY_END: Final = "--- END NOTE BODY ---"
CARD_BODY_BEGIN: Final = "--- BEGIN CARD BODY (untrusted data) ---"
CARD_BODY_END: Final = "--- END CARD BODY ---"
BODY_GUTTER: Final = "| "
# The one character a body line prints as-is (docs/protocol.md, "Printing a note"): a
# tab cannot hide or reorder text, and the gutter keeps the line from passing for a
# frame line. Every other unsafe character prints as an escape.
BODY_KEEP: Final = "\t"
# Separates the columns of an inbox or card line. One-line values cannot hold a tab.
COLUMN_SEPARATOR: Final = "\t"

# The one line ``inbox --summary`` prints before its diagnostics. A host hook parses it,
# so this format is stable: every key is always present, in this order, and a value
# that does not apply is NOT_APPLICABLE (docs/cli.md quotes it, under "Check the inbox").
INBOX_SUMMARY_FORMAT: Final = (
    "inbox-summary: count={count} oldest_age_min={oldest_age_min} stale={stale}"
)
NOT_APPLICABLE: Final = "-"
# The ``needs`` value that ``inbox --stale-after`` can flag as stale.
STALE_NEEDS: Final = "reply"
# The card state ``card show --overlaps`` leaves out.
CARD_STATE_DONE: Final = "done"
# ``card set --host claude`` binds the calling window (plan D38, D39) unless the card
# is set to one of these states; the window's session id comes from this variable.
NO_BIND_STATES: Final = (CARD_STATE_DONE, "idle")
CLAUDE_SESSION_ENV: Final = "CLAUDE_CODE_SESSION_ID"
# The first column of a ``card show --overlaps`` line.
OVERLAP_TAG: Final = "overlap"
# The first columns of resource queue lines (plan D29); FREE fills the holder column of
# a resource nobody holds. Project lock scripts parse these lines.
HOLDER_TAG: Final = "holder"
WAITER_TAG: Final = "waiter"
FREE: Final = "-"
NOTHING_TO_RELEASE: Final = "nothing to release"
# ``release``: ``skipped <lane>: card is done`` for a waiter dropped at promotion (D24).
SKIPPED_TAG: Final = "skipped"
NOTHING_TO_CANCEL: Final = "nothing to cancel"
# The first column of ``queue config`` lines (plan P21, P22).
CONFIG_COORDINATOR_TAG: Final = "coordinator"
CONFIG_STALL_TAG: Final = "stall-minutes"
CONFIG_DEFAULT_TURN_TAG: Final = "default-turn-minutes"
CONFIG_TURN_TAG: Final = "turn-minutes"
# The one line ``queue check`` prints after its finding rows (plan D28). A hook parses it,
# so this format is stable: every key is always present, in this order.
QUEUE_CHECK_FORMAT: Final = (
    "queue-check: past_deadline={past_deadline} stalled={stalled} mismatch={mismatch}"
)
# ``queue check --ring``: a stall note whose doorbell is recorded as delivered (or
# queued, for Codex), or that its recipient acknowledged (``wake.rung``; decision D26).
STALL_ALREADY_RUNG: Final = "stall: already rung {note_id}"


class CliError(CodedError):
    """A usage or validation error, or a refusal, with its exit code."""

    def __init__(self, exit_code: int, code: str, message: str) -> None:
        super().__init__(code, message)
        self.exit_code = exit_code


class OutputError(CodedError):
    """Standard output was closed or could not be written (exit 1)."""

    def __init__(self, exc: OSError) -> None:
        super().__init__(OUTPUT_UNWRITABLE, f"cannot write to standard output: {os_reason(exc)}")


Handler = Callable[[argparse.Namespace], int]


# --- Output helpers ------------------------------------------------------------------


def _out(line: str, keep: str = "") -> None:
    """One line of results, with every unsafe character except those in ``keep`` escaped.

    The only writer of standard output. A failed write (a closed pipe) is an
    OutputError.
    """
    try:
        print(protocol.escape_unsafe(line, keep=keep), file=sys.stdout)
    except OSError as exc:  # BrokenPipeError, or EINVAL for a closed pipe on Windows
        raise OutputError(exc) from None


def _flush_stdout() -> None:
    if sys.stdout is None:
        return
    try:
        sys.stdout.flush()
    except OSError as exc:
        raise OutputError(exc) from None


def _silence_stdout() -> None:
    """After an output error, point the real standard output at the null device.

    Otherwise the interpreter's own flush at exit fails again, prints an
    "Exception ignored" warning and replaces the exit code.
    """
    stream = sys.__stdout__
    if stream is None or sys.stdout is not stream:
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, stream.fileno())
        finally:
            os.close(devnull)
    except (OSError, ValueError):
        pass


def _err_line(text: str) -> None:
    """One line to stderr, escaped (argparse's own messages use ``_Parser.error``).

    If stderr is closed too, the exit code is all that is left.
    """
    try:
        print(protocol.escape_unsafe(text), file=sys.stderr)
    except OSError:
        pass


def _error(code: str, message: str) -> None:
    _err_line(f"{PROG}: error: {code}: {message}")


def _harden_streams() -> None:
    """Never crash on a character the console encoding cannot show."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")


def format_inbox_line(entry: InboxEntry, *, stale: bool = False) -> str:
    """Tab-separated: id, kind, needs, from, subject, then a state column if any.

    The state column joins ``acked``, ``superseded`` and ``stale`` (only ever set by
    ``inbox --stale-after``) with commas, in that order. One-line fields cannot hold
    tabs or other control characters, so the columns are unambiguous.
    """
    note = entry.note
    columns = [note.id, note.kind or "-", note.needs, protocol.sender_label(note), note.subject]
    flags = (("acked", entry.acked), ("superseded", entry.superseded), ("stale", stale))
    states = [name for name, flag in flags if flag]
    if states:
        columns.append(",".join(states))
    return COLUMN_SEPARATOR.join(columns)


def format_inbox_summary(count: int, oldest_age_min: int | None, stale: int | None) -> str:
    """The ``inbox --summary`` line (INBOX_SUMMARY_FORMAT); None prints as NOT_APPLICABLE."""

    def value(number: int | None) -> str:
        return NOT_APPLICABLE if number is None else str(number)

    return INBOX_SUMMARY_FORMAT.format(
        count=count, oldest_age_min=value(oldest_age_min), stale=value(stale)
    )


def note_age(note: Note, now: datetime) -> timedelta:
    """How long before ``now`` the note was created (negative if after ``now``)."""
    return now - protocol.parse_utc(note.created_utc, "created_utc")


def is_stale(note: Note, now: datetime, stale_after_min: int) -> bool:
    """True if the note needs a reply and is more than ``stale_after_min`` minutes old.

    Compared in seconds, never by building a timedelta from ``stale_after_min``, so any
    accepted value works: one beyond the largest timedelta simply flags nothing.
    """
    return note.needs == STALE_NEEDS and note_age(note, now).total_seconds() > stale_after_min * 60


def whole_minutes(age: timedelta) -> int:
    """``age`` in whole minutes, rounded down and never negative.

    A note stamped after ``now`` counts as 0.
    """
    return queue.whole_minutes(age)


def path_key(entry: str) -> str:
    """An owns or holds entry as ``card show --overlaps`` and ``card set`` compare it.

    Case-folded; backslashes become ``/``; repeated ``/`` collapse to one; ``.``
    segments (a leading ``./``, an interior ``/./``) are dropped; and any trailing
    ``/`` is dropped. A leading ``/`` is kept. ``..`` is not resolved.
    """
    key = entry.replace("\\", "/").casefold()
    lead = "/" if key.startswith("/") else ""
    return lead + "/".join(part for part in key.split("/") if part not in ("", "."))


def entries_overlap(a: str, b: str) -> bool:
    """Equal after path_key, or one is a path prefix of the other at a ``/`` boundary.

    ``src/`` overlaps ``src/passing_notes/cli.py``, ``Src/`` and ``./src//a``; ``src``
    does not overlap ``srcx``. An entry that is empty after path_key (``.``) is a
    prefix of nothing.
    """
    key_a, key_b = path_key(a), path_key(b)
    if key_a == key_b:
        return True
    return bool(key_a and key_b.startswith(key_a.rstrip("/") + "/")) or bool(
        key_b and key_a.startswith(key_b.rstrip("/") + "/")
    )


def card_overlaps(cards: Iterable[Card]) -> list[tuple[str, str, str, str]]:
    """Every overlapping owns or holds pair between two cards that are not done.

    Each is (lane-a, lane-b, entry-a, entry-b) with lane-a < lane-b, listed once, sorted
    by lane-a, lane-b, then entries.
    """
    live = sorted((c for c in cards if c.state != CARD_STATE_DONE), key=lambda c: c.lane)
    found: set[tuple[str, str, str, str]] = set()
    for index, first in enumerate(live):
        for second in live[index + 1 :]:  # one card per lane, so first.lane < second.lane
            for a in (*first.owns, *first.holds):
                for b in (*second.owns, *second.holds):
                    if entries_overlap(a, b):
                        found.add((first.lane, second.lane, a, b))
    return sorted(found)


def format_card_line(card: Card) -> str:
    """Tab-separated: lane, host, state, item, updated.

    One-line fields cannot hold tabs or other control characters, so the columns
    are unambiguous.
    """
    return COLUMN_SEPARATOR.join([card.lane, card.host, card.state, card.item, card.updated_utc])


def frame_body(body: str) -> list[str]:
    """The body's lines as ``read`` (and ``card show``) print them inside the frame.

    Each starts with BODY_GUTTER (an empty line is the gutter alone), so none can
    print as a frame line: a look-alike, a homoglyph or an invisible prefix still
    sits behind the gutter. ``_out`` then escapes anything that could hide or
    reorder text (all but BODY_KEEP), so nothing can hide the gutter either.
    """
    if not body:
        return []
    bare = BODY_GUTTER.rstrip()
    return [f"{BODY_GUTTER}{line}" if line else bare for line in body.split("\n")]


def _out_diagnostics(diagnostics: Sequence[Diagnostic]) -> None:
    """At most DIAGNOSTIC_LINES diagnostic lines, then the total count."""
    shown = diagnostics[:DIAGNOSTIC_LINES]
    for diagnostic in shown:
        _out(f"diagnostic: {diagnostic}")
    total = len(diagnostics)
    suffix = f" ({len(shown)} shown)" if total > len(shown) else ""
    _out(f"diagnostics: {total}{suffix}")


# --- Input helpers -------------------------------------------------------------------


def _read_body(args: argparse.Namespace, max_bytes: int) -> str:
    """The body from --body-file or --body-stdin (never from an argument), as text.

    At most ``max_bytes`` bytes are read. Every failure is a body error (exit 2),
    never a traceback: a missing or closed standard input, an unreadable file, a
    path no file system can name (a NUL, or a lone surrogate that POSIX cannot
    encode), more than ``max_bytes`` bytes, and bytes that are not UTF-8.
    """
    data: bytes
    if args.body_stdin:
        source = "--body-stdin"
        buffer = getattr(sys.stdin, "buffer", None)  # None: closed, or no byte stream
        if buffer is None:
            raise CliError(EXIT_USAGE, BODY_UNREADABLE, f"{source}: no standard input")
        try:
            data = buffer.read(max_bytes + 1)
        except OSError as exc:
            raise CliError(
                EXIT_USAGE, BODY_UNREADABLE, f"{source}: cannot read: {os_reason(exc)}"
            ) from None
        except ValueError:  # a closed stream
            raise CliError(
                EXIT_USAGE, BODY_UNREADABLE, f"{source}: standard input is closed"
            ) from None
    else:
        source = "--body-file"
        path: str = args.body_file
        try:
            with open(path, "rb") as handle:
                data = handle.read(max_bytes + 1)
        except OSError as exc:
            raise CliError(
                EXIT_USAGE, BODY_UNREADABLE, f"{source}: cannot read {path}: {os_reason(exc)}"
            ) from None
        except ValueError:
            raise CliError(
                EXIT_USAGE, BODY_UNREADABLE, f"{source}: {protocol.show(path)} is not a file name"
            ) from None
    if len(data) > max_bytes:
        raise CliError(EXIT_USAGE, INVALID_ARGUMENT, f"{source}: larger than {max_bytes} bytes")
    return protocol.decode_text(data.removeprefix(UTF8_BOM), source)


def _has_body(args: argparse.Namespace) -> bool:
    return bool(args.body_stdin) or args.body_file is not None


# --- Commands ------------------------------------------------------------------------


def _check_supersedes(store: Store, draft: Draft, target_id: str) -> None:
    try:
        target = store.load_note(draft.project, target_id)
    except NoteNotFoundError as exc:
        raise CliError(
            EXIT_USAGE,
            SUPERSEDES_INVALID,
            f"supersedes: {exc.message}; a note can only supersede an existing note "
            "from the same sender",
        ) from None
    if target.sender != draft.sender:
        raise CliError(
            EXIT_USAGE,
            SUPERSEDES_INVALID,
            f"supersedes: {target.id} was sent by {protocol.sender_label(target)}; "
            "a lane can only supersede its own notes",
        )


def _cmd_post(args: argparse.Namespace) -> int:
    refs: list[str] = args.refs
    draft = protocol.make_draft(
        project=args.project,
        from_project=args.from_project,
        from_lane=args.from_lane,
        to=args.to,
        subject=args.subject,
        kind=args.kind,
        needs=args.needs,
        work=args.work,
        revision=args.revision,
        supersedes=args.supersedes,
        reply_to=args.reply_to,
        idempotency_key=args.idempotency_key,
        refs=refs,
    )
    if not protocol.crossing_allowed(draft):
        sender = f"{draft.from_project}/{draft.from_lane}"
        raise CliError(
            EXIT_USAGE,
            CROSS_PROJECT_NOT_ALLOWED,
            f"not posted: {sender} to {draft.project}/{draft.to} crosses projects, and a "
            f"note crosses projects only between {protocol.WORKSPACE_PROJECT}/"
            f"{protocol.MAIN_LANE} and a project's {protocol.MAIN_LANE} (docs/protocol.md, "
            f"Topology); nothing was written. Post to your own project's "
            f"{protocol.MAIN_LANE} instead",
        )
    # --wake rings or fails loudly; --wake-if-needed rings only a note that unblocks
    # someone and posts any other note without a ring (argparse keeps them exclusive).
    if_needed: bool = args.wake_if_needed
    ringing: bool = args.wake or if_needed
    if ringing:
        flag = "--wake-if-needed" if if_needed else "--wake"
        # A retry must never post a second note, so a ring needs an idempotency key;
        # and a doorbell goes to exactly one lane.
        if draft.idempotency_key is None:
            raise CliError(
                EXIT_USAGE,
                INVALID_ARGUMENT,
                f"{flag} needs --idempotency-key so a retry posts nothing new",
            )
        if draft.to == RECIPIENT_ALL:
            # Plan P23: refuse, and point the sender at the routes that do work.
            raise CliError(
                EXIT_USAGE,
                INVALID_ARGUMENT,
                f"{flag} needs --to naming one lane, not all; ring one lane with --to <lane>,"
                " or take turns on a shared resource with passing-notes reserve",
            )
        if draft.to == RECIPIENT_OPERATOR:
            raise CliError(
                EXIT_USAGE, INVALID_ARGUMENT, f"{flag} needs --to naming one lane, not {draft.to}"
            )
    elif args.wait:
        raise CliError(EXIT_USAGE, INVALID_ARGUMENT, "--wait applies only with --wake")
    body = _read_body(args, NOTE_MAX_BYTES)
    store = Store.open()
    # Check, then publish, with no lock between them. This is the accepted design
    # (plan D14; docs/protocol.md, "Idempotency"): sequential retries are harmless, and
    # two simultaneous posts with the same key can both land, so readers show both.
    if draft.idempotency_key is not None:
        existing = store.find_idempotent(draft.project, draft.sender, draft.idempotency_key)
        if existing is not None:
            _out(existing.id)
            _err_line(
                f"{PROG}: already posted with idempotency key "
                f"{draft.idempotency_key}; nothing written"
            )
            if if_needed and wake.ring_reason(store, draft.project, existing) is None:
                return _not_rung(store, existing)
            if ringing:
                return _ring(store, draft.project, draft.to, existing.id, args.wait)
            return EXIT_OK
    eligible = ringing and wake.ring_reason(store, draft.project, draft) is not None
    if args.wake and not eligible:
        # Refuse before storing: a doorbell that would be refused must not leave a note
        # behind. Only the note's own eligibility can be known now. The recipient's
        # binding and card can change before the ring (the lane rebinds or closes), so
        # wake.send checks them at ring time; the retry above rings a stored note there.
        missing = _missing_reply_to(store, draft.project, draft)
        if missing is not None:
            why = f"the note it answers, {missing}, was not found"
        else:
            why = f"this note needs {draft.needs} and unblocks no one"
        raise CliError(
            EXIT_USAGE,
            wake.WAKE_NOT_NEEDED,
            f"not posted: {why}, so --wake would not ring it; nothing was written. "
            f"{wake.RING_RULE}, or post without --wake and the lane sees it at its next "
            "inbox check, or use --wake-if-needed to post it and ring only when it "
            "unblocks someone",
        )
    if draft.supersedes is not None:
        _check_supersedes(store, draft, draft.supersedes)
    note = store.publish_note(draft, body)
    _out(note.id)
    if eligible:
        return _ring(store, draft.project, draft.to, note.id, args.wait)
    if if_needed:
        return _not_rung(store, note)
    return EXIT_OK


def _missing_reply_to(store: Store, project: str, note: Draft | Note) -> str | None:
    """The id ``note`` answers when no such note is in the store, else None."""
    if note.reply_to == REPLY_TO_NONE:
        return None
    try:
        store.load_note(project, note.reply_to)
    except NoteNotFoundError:
        return note.reply_to
    return None


def _not_rung(store: Store, note: Note) -> int:
    """``post --wake-if-needed`` of a note that may not ring: say so, and succeed.

    The note is in the store and no doorbell is sent, so any ``--wait`` is ignored.
    """
    missing = _missing_reply_to(store, note.project, note)
    if missing is not None:
        why = f"it needs {note.needs} and the note it answers, {missing}, was not found"
    else:
        why = f"it needs {note.needs} and unblocks no one"
    _out(f"note: {note.id} is in the store")
    _out(f"wake: not rung ({why}); the lane sees it at its next inbox check")
    return EXIT_OK


def _now_utc() -> datetime:
    """The time read-only views measure note ages against, and the time queue verbs
    stamp turns with; tests replace this function."""
    return default_now()


def _cmd_inbox(args: argparse.Namespace) -> int:
    project = protocol.check_project_id(args.project, "--project")
    lane = protocol.check_lane_id(args.lane, "--lane")
    summary: bool = args.summary
    stale_after: int | None = args.stale_after
    store = Store.open()
    view = store.inbox(
        project,
        lane,
        include_acked=args.include_acked,
        include_superseded=args.include_superseded,
    )
    # Read-only: --summary and --stale-after change what is printed, never the store.
    now = _now_utc()
    stale = [
        stale_after is not None and is_stale(entry.note, now, stale_after) for entry in view.entries
    ]
    if summary:
        # The oldest listed entry is the one with the greatest age.
        oldest = max((whole_minutes(note_age(e.note, now)) for e in view.entries), default=None)
        stale_count = sum(stale) if stale_after is not None else None
        _out(format_inbox_summary(len(view.entries), oldest, stale_count))
    else:
        for entry, flagged in zip(view.entries, stale, strict=True):
            _out(format_inbox_line(entry, stale=flagged), keep=COLUMN_SEPARATOR)
    _out_diagnostics(view.diagnostics)
    return EXIT_OK


def _cmd_read(args: argparse.Namespace) -> int:
    project = protocol.check_project_id(args.project, "--project")
    note_id = protocol.check_note_id(args.note_id, "ID")
    store = Store.open()
    note = store.load_note(project, note_id)
    for line in protocol.render_header_lines(note):
        _out(line)
    _out(BODY_BEGIN)
    for line in frame_body(note.body):
        _out(line, keep=BODY_KEEP)
    _out(BODY_END)
    return EXIT_OK


def _cmd_ack(args: argparse.Namespace) -> int:
    project = protocol.check_project_id(args.project, "--project")
    lane = protocol.check_lane_id(args.lane, "--lane")
    note_id = protocol.check_note_id(args.note_id, "ID")
    session: str | None = args.session
    if session is not None:
        protocol.check_token(session, "--session")
    store = Store.open()
    note = store.load_note(project, note_id)
    if note.to == RECIPIENT_OPERATOR:
        raise CliError(
            EXIT_USAGE,
            NOT_ADDRESSED,
            f"note {note_id} is to operator; only the operator's own word acknowledges it",
        )
    if not protocol.is_addressed_to(note, project, lane):
        if note.sender == (project, lane):
            reason = f"lane {lane} sent note {note_id}"
        else:
            reason = f"note {note_id} is addressed to {note.to}, not to lane {lane}"
        raise CliError(EXIT_USAGE, NOT_ADDRESSED, reason)
    ack = protocol.make_ack(lane, default_now(), session)
    if store.publish_ack(project, note_id, ack):
        _out(f"acknowledged {note_id} as {lane}")
    else:
        _out(f"already acknowledged: {note_id} by {lane}")
    return EXIT_OK


def _card_from_args(args: argparse.Namespace, body: str) -> Card:
    """The card the ``card set`` arguments describe, holding ``body``, stamped now."""
    owns: list[str] = args.owns
    holds: list[str] = args.holds
    return protocol.make_card(
        project=args.project,
        lane=args.lane,
        host=args.host,
        item=args.item,
        state=args.state,
        updated=default_now(),
        window=args.window,
        checkout=args.checkout,
        branch=args.branch,
        owns=owns,
        holds=holds,
        next_checkpoint=args.next_checkpoint,
        body=body,
    )


def _cmd_card_set(args: argparse.Namespace) -> int:
    # Every field but the body is checked before the body is read, as post does: a bad
    # argument is reported without consuming standard input or opening a body file.
    card = _card_from_args(args, "")
    if _has_body(args):
        card = _card_from_args(args, _read_body(args, CARD_MAX_BYTES))
    store = Store.open()
    store.publish_card(card)
    _out(f"card set: {card.project}/{card.lane} at {card.updated_utc}")
    for overlap in _overlaps_with(store, card):
        _out(_format_overlap(overlap), keep=COLUMN_SEPARATOR)
    wake_line = _bind_calling_window(store, card)
    if wake_line is not None:
        _out(wake_line)
    return EXIT_OK


class _ListedOnce:
    """A host whose session list is read at most once, so the liveness check and
    ``wake.bind`` see the same listing and ``card set`` (or ``card show --live``, across
    all its rows) pays for one host call. A failed listing is not retried: the same
    WakeError is raised again."""

    def __init__(self, host: wake.Host) -> None:
        self._host = host
        self._sessions: list[wake.Session] | None = None
        self._error: wake.WakeError | None = None

    def list_sessions(self) -> list[wake.Session]:
        if self._error is not None:
            raise self._error
        if self._sessions is None:
            try:
                self._sessions = self._host.list_sessions()
            except wake.WakeError as exc:
                self._error = exc
                raise
        return list(self._sessions)

    def send(self, address: str, text: str, permission_mode: str) -> wake.SendResult:
        return self._host.send(address, text, permission_mode)


def _bind_calling_window(store: Store, card: Card) -> str | None:
    """D39: bind the calling Claude window to the card's lane; the one ``wake:`` line
    to print, or None.

    Only for ``--host claude``, a state other than done or idle, and a set
    ``CLAUDE_CODE_SESSION_ID``. A binding to this window is left as it is, with no host
    call. A binding to another window that is still live is never replaced. With no
    valid binding, or one whose window is gone, ``wake.bind`` binds this window in its
    configured mode, and refuses an id that is not a live window (a sub-agent's own
    id, for example). Advisory: a WakeError (``ClaudeHost`` reports every host failure
    as one) or a store error is one ``wake: not bound (<code>)`` line, and the card just
    published and the exit code never depend on it.
    """
    session_id = os.environ.get(CLAUDE_SESSION_ENV, "")
    if card.host != wake.HOST_CLAUDE or card.state in NO_BIND_STATES or not session_id:
        return None
    try:
        try:
            current: wake.Binding | None = wake.load_binding(store, card.project, card.lane)
        except wake.WakeError:  # missing, damaged or of an unknown host: no valid binding
            current = None
        if (
            current is not None
            and current.host == wake.HOST_CLAUDE
            and current.session_id == session_id
        ):
            return None
        host = _ListedOnce(make_wake_host())
        if current is not None and current.host == wake.HOST_CLAUDE:
            for live in host.list_sessions():
                if live.session_id == current.session_id:
                    return (
                        f"wake: {card.lane} stays bound to live session {live.session_id} "
                        f"({live.name}); run wake bind from the lane's own window if this "
                        "window is the lane"
                    )
        binding, session, _ = wake.bind(store, host, card.project, card.lane, session_id, None)
    except CodedError as exc:
        return f"wake: not bound ({exc.code})"
    except ProtocolError:  # CLAUDE_CODE_SESSION_ID is not a session id
        return f"wake: not bound ({INVALID_ARGUMENT})"
    except OSError:
        return f"wake: not bound ({STORE_UNUSABLE})"
    return f"wake: bound {binding.lane} to {binding.session_id} ({session.name})"


def _overlaps_with(store: Store, card: Card) -> list[tuple[str, str, str, str]]:
    """The ``card show --overlaps`` rows that involve ``card``'s lane, in that order.

    Advisory and fail-open: a done card overlaps nothing, and a store that cannot be
    scanned gives no rows, so the card just published and the exit code never depend
    on this. Unreadable sibling cards are skipped, as the scan skips them. The card
    just set is compared as published, whatever a concurrent writer left on disk.
    """
    if card.state == CARD_STATE_DONE:
        return []
    try:
        cards, _ = store.scan_cards(card.project)
    except (CodedError, OSError):
        return []
    others = [c for c in cards if c.lane != card.lane]
    return [row for row in card_overlaps([*others, card]) if card.lane in row[:2]]


def _format_overlap(overlap: tuple[str, str, str, str]) -> str:
    """One ``card show --overlaps`` line: overlap, lane-a, lane-b, entry-a, entry-b."""
    return COLUMN_SEPARATOR.join((OVERLAP_TAG, *overlap))


def _cmd_card_show(args: argparse.Namespace) -> int:
    project = protocol.check_project_id(args.project, "--project")
    lane: str | None = args.lane
    overlaps: bool = args.overlaps
    live: bool = args.live
    if lane is not None and overlaps:
        raise CliError(EXIT_USAGE, INVALID_ARGUMENT, "--overlaps applies only without --lane")
    if lane is not None and live:
        raise CliError(EXIT_USAGE, INVALID_ARGUMENT, "--live applies only without --lane")
    if lane is not None:
        lane = protocol.check_lane_id(lane, "--lane")
    store = Store.open()
    if lane is not None:
        card = store.load_card(project, lane)
        for line in protocol.render_card_header_lines(card):
            _out(line)
        _out(CARD_BODY_BEGIN)
        for line in frame_body(card.body):
            _out(line, keep=BODY_KEEP)
        _out(CARD_BODY_END)
        return EXIT_OK
    cards, diagnostics = store.scan_cards(project)
    if live:
        _out_live_card_lines(store, project, cards)
    else:
        for card in cards:
            _out(format_card_line(card), keep=COLUMN_SEPARATOR)
    if overlaps:  # read-only, like the rest of card show
        for overlap in card_overlaps(cards):
            _out(_format_overlap(overlap), keep=COLUMN_SEPARATOR)
    _out_diagnostics(diagnostics)
    return EXIT_OK


def _out_live_card_lines(store: Store, project: str, cards: Sequence[Card]) -> None:
    """D45: each card line with two more columns, the bound window's state and
    ``refused=<n>``. One host listing for the whole run, made only when a Claude binding
    needs it. Both columns fail open (wake.EVIDENCE_ERRORS): a failed listing or an
    unreadable binding or card shows as ``unknown (<code>)`` in that row, an unreadable
    ring record shows ``refused=unknown`` for the lane its file name carries (other
    lanes keep exact counts), and a rings folder that cannot be listed shows
    ``refused=unknown`` in every row. Neither changes the exit code.
    """
    host = _ListedOnce(make_wake_host())
    refused = wake.refused_counts(store, project)
    for card in cards:
        # The state may hold host text (waitingFor): escape it whole, tabs included, so
        # it stays one column.
        state = protocol.escape_unsafe(wake.window_state(store, host, project, card.lane))
        count = wake.LIVE_UNKNOWN if refused is None else _refused_column(refused, card.lane)
        columns = (format_card_line(card), state, f"refused={count}")
        _out(COLUMN_SEPARATOR.join(columns), keep=COLUMN_SEPARATOR)


def _cmd_status(args: argparse.Namespace) -> int:
    """Every lane of the named projects, of the caller's own projects (``--mine``), or of
    every project the caller may see; read-only. ``--live`` leaves done lanes out.

    The caller's scope (docs/protocol.md#topology) comes from CLAUDE_CODE_SESSION_ID and
    its live Claude bindings. A refusal prints one error line and nothing on stdout.
    """
    named: list[str] | None = args.project
    all_projects: bool = args.all_projects
    mine: bool = args.mine
    if (named is not None) + all_projects + mine != 1:
        raise CliError(
            EXIT_USAGE,
            INVALID_ARGUMENT,
            "give exactly one of --project (one or more), --all-projects and --mine",
        )
    projects: list[str] | None = None
    if named is not None:
        projects = list(dict.fromkeys(protocol.check_project_id(p, "--project") for p in named))
    store = Store.open()
    scope = status.caller_scope(store, os.environ.get(CLAUDE_SESSION_ENV, ""))
    if mine:
        if not scope.bound:
            raise CliError(
                EXIT_USAGE, INVALID_ARGUMENT, "--mine needs a window bound to at least one lane"
            )
        projects = status.mine_projects(scope)
    for wanted in [None] if projects is None else projects:
        reason = status.refusal(scope, wanted)
        if reason is not None:
            raise CliError(EXIT_USAGE, status.STATUS_OUT_OF_SCOPE, reason)
    report = status.collect(
        store,
        scope,
        projects,
        now=_now_utc(),
        stale_after_hours=args.stale_after,
        live=args.live,
    )
    if args.json:
        for line in status.render_json(report):
            _out(line)
        return EXIT_OK
    for line in status.render_text(report):
        _out(line)
    _out_diagnostics(report.diagnostics)
    _out(status.summary_line(report))
    return EXIT_OK


def _refused_column(refused: Mapping[str, int | None], lane: str) -> str:
    """The lane's ``refused=`` value: its count, 0 when it has none, or ``unknown``."""
    if lane not in refused:
        return "0"
    count = refused[lane]
    return wake.LIVE_UNKNOWN if count is None else str(count)


# --- Resource queues -----------------------------------------------------------------


def format_holder_line(record: queue.Record) -> str:
    """``holder``, resource, lane, granted, deadline; or ``holder``, resource, ``-``."""
    holder = record.holder
    if holder is None:
        return COLUMN_SEPARATOR.join((HOLDER_TAG, record.resource, FREE))
    columns = (HOLDER_TAG, record.resource, holder.lane, holder.granted_utc, holder.deadline_utc)
    return COLUMN_SEPARATOR.join(columns)


def format_waiter_line(record: queue.Record, position: int) -> str:
    """``waiter``, resource, position (from 1), lane, since."""
    waiter = record.waiters[position - 1]
    columns = (WAITER_TAG, record.resource, str(position), waiter.lane, waiter.since_utc)
    return COLUMN_SEPARATOR.join(columns)


def _out_queue(record: queue.Record) -> None:
    """The holder line, then one waiter line per waiter, in order."""
    _out(format_holder_line(record), keep=COLUMN_SEPARATOR)
    for position in range(1, len(record.waiters) + 1):
        _out(format_waiter_line(record, position), keep=COLUMN_SEPARATOR)


def _queue_ids(args: argparse.Namespace) -> tuple[str, str, str]:
    project = protocol.check_project_id(args.project, "--project")
    resource = protocol.check_resource_id(args.resource, "RESOURCE")
    lane = protocol.check_lane_id(args.lane, "--lane")
    return project, resource, lane


def _cmd_reserve(args: argparse.Namespace) -> int:
    project, resource, lane = _queue_ids(args)
    minutes: int | None = args.minutes
    reason: str | None = args.reason
    if args.cancel:
        if minutes is not None or reason is not None:
            raise CliError(
                EXIT_USAGE, INVALID_ARGUMENT, "--minutes and --reason do not apply with --cancel"
            )
        return _cancel(Store.open(), project, resource, lane)
    if reason is not None:
        protocol.check_queue_reason(reason, "--reason")
    store = Store.open()
    change = queue.reserve(
        store, project, resource, lane, _now_utc(), minutes=minutes, reason=reason
    )
    record = change.record
    _out(format_holder_line(record), keep=COLUMN_SEPARATOR)
    position = record.position(lane)
    if position is not None:
        _out(format_waiter_line(record, position), keep=COLUMN_SEPARATOR)
    if not change.changed:
        place = "holds it" if position is None else f"is waiting at position {position}"
        _err_line(f"{PROG}: lane {lane} already {place} on {resource}; nothing written")
    return EXIT_OK


def _cancel(store: Store, project: str, resource: str, lane: str) -> int:
    change = queue.cancel(store, project, resource, lane)
    if not change.changed:
        _out(NOTHING_TO_CANCEL)
        return EXIT_OK
    _out_queue(change.record)
    return EXIT_OK


def _cmd_release(args: argparse.Namespace) -> int:
    project, resource, lane = _queue_ids(args)
    holder: str | None = args.holder
    if holder is not None:
        holder = protocol.check_lane_id(holder, "--holder")
    store = Store.open()
    change = queue.release(store, project, resource, lane, _now_utc(), holder=holder)
    if not change.changed:
        return _retry_turn_note(store, change.record, lane)
    record = change.record
    _out(format_holder_line(record), keep=COLUMN_SEPARATOR)
    for skipped in change.skipped:
        _out(f"{SKIPPED_TAG} {skipped}: card is done")
    for invalid in change.invalid_cards:
        _err_line(
            f"{PROG}: the card of lane {invalid} is invalid, so it was promoted as not done; "
            f"the wake path decides whether it is rung"
        )
    return _tell_holder(store, record, lane)


def _tell_holder(store: Store, record: queue.Record, sender: str) -> int:
    """Post the turn's reservation note from ``sender`` to the new holder, then ring it.

    Called after the queue lock is released (plan P24): never post or ring under it.
    A free resource, or a turn ``sender`` did not grant to another lane, gets no note.
    The note's key names the grant, so a note already posted for it is rung, not
    posted again (one note per grant).
    """
    holder = record.holder
    if holder is None or holder.granted_by != sender or holder.lane == sender:
        return EXIT_OK
    draft = queue.turn_draft(record, sender)
    key = queue.turn_key(record.resource, record.grant_seq)  # the draft's key
    note = store.find_idempotent(record.project, draft.sender, key)
    if note is None:
        note = store.publish_note(draft, queue.turn_body(record, sender))
    return _ring(store, record.project, holder.lane, note.id, 0.0)


def _retry_turn_note(store: Store, record: queue.Record, lane: str) -> int:
    """``release`` by a lane not in the queue (decision D22).

    Normally ``nothing to release``. But if ``lane`` granted the current turn to
    another lane and that turn's reservation note from ``lane`` is missing (a crash
    between the promotion and the post), post and ring it now. A retry after a
    complete release finds the note and posts and rings nothing.
    """
    holder = record.holder
    if holder is not None and holder.granted_by == lane and holder.lane != lane:
        key = queue.turn_key(record.resource, record.grant_seq)
        if store.find_idempotent(record.project, (record.project, lane), key) is None:
            _out(format_holder_line(record), keep=COLUMN_SEPARATOR)
            _err_line(
                f"{PROG}: lane {lane} granted turn {record.grant_seq} on {record.resource} "
                f"but its reservation note {key} is missing; posting it now"
            )
            return _tell_holder(store, record, lane)
    _out(NOTHING_TO_RELEASE)
    return EXIT_OK


def _cmd_queue_show(args: argparse.Namespace) -> int:
    project = protocol.check_project_id(args.project, "--project")
    resource: str | None = args.resource
    if resource is not None:
        resource = protocol.check_resource_id(resource, "RESOURCE")
    view = queue.show(Store.open(), project, resource)
    for record in view.records:
        _out_queue(record)
    _out_diagnostics(view.diagnostics)
    return EXIT_OK


def _cmd_queue_move(args: argparse.Namespace) -> int:
    project, resource, lane = _queue_ids(args)
    waiter = protocol.check_lane_id(args.waiter, "--waiter")
    to: int | None = args.to
    after: str | None = args.after
    if after is not None:
        after = protocol.check_lane_id(after, "--after")
        if after == waiter:
            raise CliError(EXIT_USAGE, INVALID_ARGUMENT, "--after must name another waiter")
    change = queue.move(Store.open(), project, resource, lane, waiter, to=to, after=after)
    _out_queue(change.record)
    if not change.changed:
        position = change.record.position(waiter)
        _err_line(
            f"{PROG}: lane {waiter} is already at position {position} on {resource}; "
            "nothing written"
        )
    return EXIT_OK


def format_config_lines(config: queue.Config) -> list[str]:
    """The effective config as ``queue config`` prints it, one tab-separated line each.

    Coordinator, stall minutes and the default turn minutes, then one line per resource
    the config lists, sorted.
    """
    sep = COLUMN_SEPARATOR
    lines = [
        sep.join((CONFIG_COORDINATOR_TAG, config.effective_coordinator)),
        sep.join((CONFIG_STALL_TAG, str(config.effective_stall_minutes))),
        sep.join((CONFIG_DEFAULT_TURN_TAG, str(TURN_DEFAULT_MINUTES))),
    ]
    lines.extend(
        sep.join((CONFIG_TURN_TAG, resource, str(minutes)))
        for resource, minutes in config.resource_minutes
    )
    return lines


def _cmd_queue_config(args: argparse.Namespace) -> int:
    project = protocol.check_project_id(args.project, "--project")
    lane = protocol.check_lane_id(args.lane, "--lane")
    coordinator: str | None = args.coordinator
    if coordinator is not None:
        coordinator = protocol.check_lane_id(coordinator, "--coordinator")
    resource: str | None = args.resource
    minutes: int | None = args.minutes
    if (resource is None) != (minutes is None):
        raise CliError(EXIT_USAGE, INVALID_ARGUMENT, "--resource and --minutes go together")
    if resource is not None:
        resource = protocol.check_resource_id(resource, "--resource")
    stall: int | None = args.stall_minutes
    store = Store.open()
    if coordinator is None and resource is None and stall is None:
        config = queue.load_config(store, project)  # a read: any lane may
    else:
        change = queue.configure(
            store,
            project,
            lane,
            coordinator=coordinator,
            resource=resource,
            minutes=minutes,
            stall_minutes=stall,
        )
        config = change.config
        if not change.changed:
            _err_line(f"{PROG}: the queue config already says that; nothing written")
    for line in format_config_lines(config):
        _out(line, keep=COLUMN_SEPARATOR)
    return EXIT_OK


def format_check_line(finding: queue.Finding) -> str:
    """One ``queue check`` finding row (plan D28), tab-separated."""
    record = finding.record
    holder = FREE if record.holder is None else record.holder.lane
    if finding.kind == queue.MISMATCH:
        columns = (finding.kind, record.resource, holder, finding.lock_holder)
    else:
        columns = (finding.kind, record.resource, holder, str(finding.minutes))
    return COLUMN_SEPARATOR.join(columns)


def format_check_summary(report: queue.CheckReport) -> str:
    """The pinned ``queue-check:`` line (QUEUE_CHECK_FORMAT)."""
    return QUEUE_CHECK_FORMAT.format(
        past_deadline=report.count(queue.PAST_DEADLINE),
        stalled=report.count(queue.STALLED),
        mismatch=report.count(queue.MISMATCH),
    )


def _cmd_queue_check(args: argparse.Namespace) -> int:
    project = protocol.check_project_id(args.project, "--project")
    resource: str | None = args.resource
    if resource is not None:
        resource = protocol.check_resource_id(resource, "RESOURCE")
    lane: str | None = args.lane
    ring: bool = args.ring
    if ring != (lane is not None):
        raise CliError(EXIT_USAGE, INVALID_ARGUMENT, "--lane and --ring go together")
    if lane is not None:
        lane = protocol.check_lane_id(lane, "--lane")
    lock_holder: str | None = args.lock_holder
    if lock_holder is not None:
        if resource is None:
            raise CliError(
                EXIT_USAGE,
                INVALID_ARGUMENT,
                "--lock-holder needs RESOURCE: it says who holds that one resource's lock",
            )
        if lock_holder != queue.LOCK_HOLDER_NONE:
            lock_holder = protocol.check_lane_id(lock_holder, "--lock-holder")
    store = Store.open()
    now = _now_utc()
    report = queue.check(store, project, resource, now, lock_holder=lock_holder)
    for finding in report.findings:
        _out(format_check_line(finding), keep=COLUMN_SEPARATOR)
    _out(format_check_summary(report))
    _out_diagnostics(report.diagnostics)
    if lane is None:
        return EXIT_OK  # without --ring nothing is written (plan Step 8)
    return _ring_stalls(store, report, lane, now)


def _ring_stalls(store: Store, report: queue.CheckReport, lane: str, now: datetime) -> int:
    """``queue check --ring --lane L``: tell each stall's holder and coordinator once
    (D26, D36).

    Two questions, each answered from its own record. Whether to *post*:
    ``queue.post_stall`` checks each stalled turn again under its stall lock, posts from
    L the note of each recipient with no stall note yet (never to L, nor to a lane that
    sent one), and returns one alarm note per recipient, a note to L included. Whether
    to *ring*: after the lock is released, an alarm whose doorbell the wake ring
    records show was handed over, or that its recipient acknowledged (``wake.rung``),
    prints ``stall: already rung <id>``; every other alarm except one to L is rung
    through ``_ring``. So a second run after a delivered first, from any lane, stores
    and rings nothing, and a note stored but never rung (a failed ring, or a crash
    before it) is rung by the next run. ``wake.send`` reads the ring record again under
    its per-note claim, so two runs never ring one note twice. Exit 5 when any doorbell
    was not delivered (the notes stay); a damaged config rings nothing.
    """
    code = EXIT_OK
    stalls = [f for f in report.findings if f.kind == queue.STALLED]
    if stalls and report.config is None:
        _err_line(
            f"{PROG}: the queue config is damaged (see the diagnostic), so the coordinator "
            "is unknown and no stall is rung"
        )
        return code
    for finding in stalls:
        record = finding.record
        if record.holder is None or report.config is None:
            continue
        coordinator = report.config.effective_coordinator
        # Checked again and posted under the resource's stall lock, then rung after it.
        stall = queue.post_stall(store, record, lane, coordinator, now, report.stall_minutes)
        if not stall.stalled:
            continue  # released or acknowledged since the check: nothing to tell
        if record.holder.lane == coordinator == lane:
            _err_line(
                f"{PROG}: stall on {record.resource}: lane {lane} is both its holder "
                "and the coordinator, so no note is sent"
            )
        for note in stall.alarms:
            # Read at ring time, outside every lock: the ring record, not the note store.
            if wake.rung(store, record.project, note.id, note.to):
                _out(STALL_ALREADY_RUNG.format(note_id=note.id))
                continue
            if note.to == lane:
                continue  # L ran this check itself: never ring it (D36)
            rang = _ring(store, record.project, note.to, note.id, 0.0)
            if code == EXIT_OK:
                code = rang
    return code


# --- Wake (doorbells for bound Claude lanes) ----------------------------------------


def make_wake_host() -> wake.Host:
    """The real Claude host; tests replace this function."""
    return wake.ClaudeHost()


def make_codex_host() -> wake.CodexQueue:
    return wake.CodexHost()


def _ring(store: Store, project: str, lane: str, note_id: str, wait_s: float) -> int:
    """Ring ``lane`` about ``note_id`` and print the evidence, one state per line."""
    _out(f"note: {note_id} is in the store")
    try:
        # send() picks the host from the binding, after its already-acknowledged check.
        outcome = wake.send(
            store,
            make_wake_host(),
            project,
            lane,
            note_id,
            wait_s=wait_s,
            codex_host=make_codex_host(),
        )
    except wake.WakeError as exc:
        _out("wake: not delivered")
        _error(exc.code, exc.message)
        return EXIT_WAKE
    _out(f"wake: {outcome.wake_line}")
    waited = f" (watched {outcome.waited_s:.0f}s)" if wait_s else ""
    if outcome.ack_utc:
        _out(f"ack: observed, read_utc {outcome.ack_utc}")
    else:
        _out(f"ack: not observed{waited}")
    if outcome.reply_id:
        _out(f"reply: observed {outcome.reply_id}")
    else:
        _out(f"reply: not observed{waited}")
    return EXIT_OK


def _cmd_wake_bind(args: argparse.Namespace) -> int:
    store = Store.open()
    if args.host == wake.HOST_CODEX:
        if args.permission_mode is not None:
            raise CliError(EXIT_USAGE, INVALID_ARGUMENT, "--permission-mode applies only to Claude")
        binding = wake.bind_codex(store, args.project, args.lane, args.session)
        _out(f"bound {binding.project}/{binding.lane} to Codex thread {binding.session_id}")
        return EXIT_OK
    binding, session, origin = wake.bind(
        store, make_wake_host(), args.project, args.lane, args.session, args.permission_mode
    )
    _out(
        f"bound {binding.project}/{binding.lane} to claude session {binding.session_id} "
        f"({session.name}, {session.status}) in {binding.permission_mode} mode ({origin})"
    )
    return EXIT_OK


def _cmd_wake_send(args: argparse.Namespace) -> int:
    project = protocol.check_project_id(args.project, "--project")
    lane = protocol.check_lane_id(args.lane, "--lane")
    note_id = protocol.check_note_id(args.note_id, "ID")
    return _ring(Store.open(), project, lane, note_id, args.wait)


def _cmd_wake_show(args: argparse.Namespace) -> int:
    project = protocol.check_project_id(args.project, "--project")
    store = Store.open()
    for b in wake.list_bindings(store, project):
        _out(
            COLUMN_SEPARATOR.join((b.lane, b.host, b.session_id, b.permission_mode, b.bound_utc)),
            keep=COLUMN_SEPARATOR,
        )
    return EXIT_OK


# --- Claude Stop hook ----------------------------------------------------------------


def _cmd_hook_stop(args: argparse.Namespace) -> int:
    """D41, D42, D46: print the block reply or nothing, and always exit 0.

    Even a closed standard output is silent here: a broken hook must never block a
    window from stopping, and the host reads only stdout and the exit code.
    """
    reply = hook.run_stop(sys.stdin)
    if reply is None:
        return EXIT_OK
    try:
        _out(reply)
        _flush_stdout()
    except OutputError:
        _silence_stdout()
    return EXIT_OK


def _settings_target(args: argparse.Namespace) -> Path:
    """``--settings``, or the user's settings file (``hook.default_settings_path``)."""
    settings: str | None = args.settings
    return Path(settings) if settings is not None else hook.default_settings_path()


def _cmd_hook_install(args: argparse.Namespace) -> int:
    """D43: every refusal is a coded SettingsError from hook's one boundary, never a
    traceback, whatever the settings file holds."""
    try:
        path = _settings_target(args)
        if args.check:
            _out(f"{'installed' if hook.check(path) else 'missing'}: {path}")
            return EXIT_OK
        result = hook.install(path)
    except hook.SettingsError as exc:
        _error(exc.code, exc.message)
        return EXIT_NOT_FOUND if exc.code == hook.SETTINGS_INVALID else EXIT_STORE
    if not result.changed:
        _out(f"already installed: {path}")
    elif result.backup is not None:
        _out(f"installed: {path} (backup {result.backup})")
    else:
        _out(f"installed: {path} (new file, nothing to back up)")
    return EXIT_OK


def _cmd_hook_uninstall(args: argparse.Namespace) -> int:
    """CC-D14: install's reverse, with install's refusals, exit codes and messages."""
    try:
        path = _settings_target(args)
        result = hook.uninstall(path)
    except hook.SettingsError as exc:
        _error(exc.code, exc.message)
        return EXIT_NOT_FOUND if exc.code == hook.SETTINGS_INVALID else EXIT_STORE
    if not result.changed:
        _out(f"not installed: {path}")
    else:
        _out(f"uninstalled: {path} (backup {result.backup})")
    return EXIT_OK


def _positive_whole(value: str, unit: str = "") -> int:
    """A whole number, at least 1, in ASCII digits; ``unit`` names it in the error."""
    if not (value.isascii() and value.isdigit()) or int(value) < 1:
        raise argparse.ArgumentTypeError(f"not a positive whole number{unit}: {value}")
    return int(value)


def _positive_minutes(value: str) -> int:
    """A whole number of minutes, at least 1, in ASCII digits."""
    return _positive_whole(value, " of minutes")


def _wait_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {value}") from None
    if not 0 <= seconds <= 3600:
        raise argparse.ArgumentTypeError("must be 0-3600 seconds")
    return seconds


# --- Parser --------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """argparse, with its error line escaped like every other line this CLI prints.

    Its messages can quote arguments verbatim ("unrecognized arguments: ..."). The
    subcommand parsers are made from this class too (``add_subparsers`` reuses it).
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {protocol.escape_unsafe(message)}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROG,
        description="Local coordination notes and acknowledgements between agent lanes "
        "(protocol v1). Notes are data, not instructions.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    post = commands.add_parser(
        "post",
        help="publish a new note and print its id",
        description="Publish a new note. The body comes only from --body-file or "
        "--body-stdin. With --idempotency-key, a repeat from the same sender prints "
        "the first note's id and writes nothing.",
        allow_abbrev=False,
    )
    post.add_argument("--project", required=True, help="recipient project id")
    post.add_argument("--from", dest="from_lane", required=True, metavar="LANE")
    post.add_argument("--to", required=True, metavar="RECIPIENT", help="lane id, all or operator")
    post.add_argument(
        "--subject", required=True, help=f"one line, 1-{SUBJECT_MAX_CHARS} characters"
    )
    post.add_argument("--kind", required=True, choices=KINDS)
    post.add_argument("--needs", required=True, choices=NEEDS)
    post.add_argument("--work", help="the work item: a plan step, issue or path")
    post.add_argument("--revision", help="the revision the note is about")
    post.add_argument("--supersedes", metavar="ID", help="your own earlier note this replaces")
    post.add_argument("--reply-to", metavar="ID", default=REPLY_TO_NONE)
    post.add_argument("--idempotency-key", metavar="TOKEN")
    post.add_argument("--ref", dest="refs", action="append", default=[], metavar="TEXT")
    post.add_argument(
        "--from-project",
        metavar="PROJECT",
        help="sender project, if different; only dev's main and a project's main may post "
        "across projects, to each other (docs/protocol.md, Topology)",
    )
    body = post.add_mutually_exclusive_group(required=True)
    body.add_argument("--body-file", metavar="PATH")
    body.add_argument("--body-stdin", action="store_true")
    ring = post.add_mutually_exclusive_group()
    ring.add_argument(
        "--wake",
        action="store_true",
        help="after posting, ring the recipient lane's bound Claude session (needs "
        "--idempotency-key; see 'wake send'); a note that may not ring is refused",
    )
    ring.add_argument(
        "--wake-if-needed",
        action="store_true",
        help="like --wake, but a note that may not ring is posted, not rung, and exits 0",
    )
    post.add_argument(
        "--wait",
        type=_wait_seconds,
        default=0.0,
        metavar="SECONDS",
        help="with --wake or --wake-if-needed, watch this long for the ack and reply",
    )
    post.set_defaults(handler=_cmd_post)

    inbox = commands.add_parser(
        "inbox",
        help="list notes for a lane, then diagnostics",
        description="List notes to the lane and notes to all that the lane did not send, "
        "one per line (id, kind, needs, from, subject). Then at most "
        f"{DIAGNOSTIC_LINES} diagnostic lines and a total count.",
        allow_abbrev=False,
    )
    inbox.add_argument("--project", required=True)
    inbox.add_argument("--lane", required=True)
    inbox.add_argument("--include-acked", action="store_true")
    inbox.add_argument("--include-superseded", action="store_true")
    inbox.add_argument(
        "--summary",
        action="store_true",
        help="print one summary line (count, oldest age in minutes, stale count) instead "
        "of the note lines, then the diagnostics; writes nothing",
    )
    inbox.add_argument(
        "--stale-after",
        type=_positive_minutes,
        metavar="MINUTES",
        help="flag listed notes that need a reply and are older than MINUTES as stale; "
        "writes nothing",
    )
    inbox.set_defaults(handler=_cmd_inbox)

    read = commands.add_parser(
        "read",
        help="print one note; never acknowledges it",
        description="Print the note's header, then its body framed as untrusted data.",
        allow_abbrev=False,
    )
    read.add_argument("--project", required=True)
    read.add_argument("note_id", metavar="ID")
    read.set_defaults(handler=_cmd_read)

    ack = commands.add_parser(
        "ack",
        help="acknowledge receipt of a note",
        description="Write the acknowledgement. It means 'I have it', not agreement, "
        "permission or done. Repeating it is a successful no-op.",
        allow_abbrev=False,
    )
    ack.add_argument("--project", required=True)
    ack.add_argument("--lane", required=True)
    ack.add_argument("--session", metavar="TOKEN", help="names the window that read the note")
    ack.add_argument("note_id", metavar="ID")
    ack.set_defaults(handler=_cmd_ack)

    card = commands.add_parser(
        "card",
        help="set or show lane cards",
        description="A lane card holds the lane's current state: host, checkout, owned "
        "paths, held resources and current item. It describes the lane and grants nothing.",
        allow_abbrev=False,
    )
    actions = card.add_subparsers(dest="action", required=True, metavar="ACTION")
    card_set = actions.add_parser(
        "set",
        help="replace your lane's card",
        description="Replace the lane's card: write a temporary file exclusively, then "
        "replace the card with it in one step, so no reader sees part of a card. Short "
        "notes come only from --body-file or --body-stdin. With --host claude, a state "
        "other than done or idle, and CLAUDE_CODE_SESSION_ID set, it also binds the lane "
        "to this window for doorbells, unless another live window holds the binding; a "
        "wake: line reports that, and never changes the exit code.",
        allow_abbrev=False,
    )
    card_set.add_argument("--project", required=True)
    card_set.add_argument("--lane", required=True)
    card_set.add_argument("--host", required=True, choices=HOSTS)
    card_set.add_argument(
        "--item",
        required=True,
        metavar="TEXT",
        help=f"the current work item, one line of at most {CARD_ITEM_MAX_CHARS} characters",
    )
    card_set.add_argument("--state", required=True, choices=CARD_STATES)
    card_set.add_argument("--window", metavar="TOKEN", help="names the current window")
    card_set.add_argument("--checkout", metavar="TEXT", help="the checkout the lane works in")
    card_set.add_argument("--branch", metavar="TEXT")
    card_set.add_argument(
        "--own",
        dest="owns",
        action="append",
        default=[],
        metavar="TEXT",
        help="a path or item the lane owns (repeatable)",
    )
    card_set.add_argument(
        "--hold",
        dest="holds",
        action="append",
        default=[],
        metavar="TEXT",
        help="a shared resource the lane holds now (repeatable)",
    )
    card_set.add_argument(
        "--next", dest="next_checkpoint", metavar="TEXT", help="the next checkpoint"
    )
    card_body = card_set.add_mutually_exclusive_group()
    card_body.add_argument("--body-file", metavar="PATH")
    card_body.add_argument("--body-stdin", action="store_true")
    card_set.set_defaults(handler=_cmd_card_set)

    card_show = actions.add_parser(
        "show",
        help="print one card, or one line per lane",
        description="With --lane, print that lane's card, its notes framed as untrusted "
        "data. Without it, print one line per lane (lane, host, state, item, updated), then "
        f"at most {DIAGNOSTIC_LINES} diagnostic lines and a total count.",
        allow_abbrev=False,
    )
    card_show.add_argument("--project", required=True)
    card_show.add_argument("--lane")
    card_show.add_argument(
        "--overlaps",
        action="store_true",
        help="list mode only: after the card lines, one line per overlapping owns or holds "
        "entry between two lanes that are not done; writes nothing",
    )
    card_show.add_argument(
        "--live",
        action="store_true",
        help="list mode only: add two columns to each card line, the bound window's state "
        "(idle, busy, waiting: <what>, not running, unbound, codex) and refused=<n>, the "
        "lane's unacknowledged notes whose latest ring was refused as busy; one claude "
        "agents call; writes nothing",
    )
    card_show.set_defaults(handler=_cmd_card_show)

    status_cmd = commands.add_parser(
        "status",
        help="every lane of a project, or of every project you may see; read-only",
        description="Print one table row per lane (project, lane, state, host, window, "
        "updated, open, working on), each project's operator decisions and flags, at "
        f"most {DIAGNOSTIC_LINES} diagnostic lines and a summary line. Never shows a "
        "note body or subject and writes nothing. Who may see which project: "
        f"{status.TOPOLOGY_REF}.",
        allow_abbrev=False,
    )
    status_cmd.add_argument(
        "--project",
        action="append",
        help="a project; repeat for several, shown in the order given, duplicates dropped",
    )
    status_cmd.add_argument(
        "--all-projects",
        action="store_true",
        help="every project in the store (the workspace main, or an unbound terminal)",
    )
    status_cmd.add_argument(
        "--mine",
        action="store_true",
        help="the projects of this window's live bindings, sorted (needs a bound window)",
    )
    status_cmd.add_argument(
        "--live",
        action="store_true",
        help="leave done lanes out of the rows; each project keeps its true done count",
    )
    status_cmd.add_argument(
        "--stale-after",
        type=functools.partial(_positive_whole, unit=" of hours"),
        default=status.STALE_AFTER_DEFAULT_HOURS,
        metavar="H",
        help="flag active, waiting or blocked lanes whose card is older than H hours "
        f"(default {status.STALE_AFTER_DEFAULT_HOURS})",
    )
    status_cmd.add_argument(
        "--json", action="store_true", help=f"print one JSON object ({status.SCHEMA})"
    )
    status_cmd.set_defaults(handler=_cmd_status)

    wake_cmd = commands.add_parser(
        "wake",
        help="bind a lane's host session, or ring it about a note",
        description="A content-free doorbell: it tells a bound Claude or Codex session the id "
        "of a note addressed to its lane, and the lane reads the note itself. Delivery is "
        "not reading; ack and reply are reported only when their files exist.",
        allow_abbrev=False,
    )
    wake_actions = wake_cmd.add_subparsers(dest="action", required=True, metavar="ACTION")
    wake_bind = wake_actions.add_parser(
        "bind",
        help="bind your lane to this host window (run it from that window)",
        description="Record the lane's exact host session id. Run it from the lane's own "
        "window with --session set to CLAUDE_CODE_SESSION_ID or CODEX_THREAD_ID. "
        "Without --permission-mode, the window's configured default mode is read from "
        "Claude's settings files.",
        allow_abbrev=False,
    )
    wake_bind.add_argument("--project", required=True)
    wake_bind.add_argument("--lane", required=True)
    wake_bind.add_argument("--host", required=True, choices=(wake.HOST_CLAUDE, wake.HOST_CODEX))
    wake_bind.add_argument("--session", required=True, metavar="SESSION_ID")
    wake_bind.add_argument(
        "--permission-mode",
        choices=wake.PERMISSION_MODES,
        help="only if this window was started with a mode flag or switched modes; by "
        "default the mode is read from Claude's settings files",
    )
    wake_bind.set_defaults(handler=_cmd_wake_bind)
    wake_send = wake_actions.add_parser(
        "send",
        help="ring a lane's bound session about a note already posted to it",
        description="Claude refuses busy or stale sessions. Codex queues to the bound "
        "thread without interrupting a turn. Never rings twice for a note and thread. "
        "Exit 5: not queued or delivered; the "
        "note stays posted.",
        allow_abbrev=False,
    )
    wake_send.add_argument("--project", required=True)
    wake_send.add_argument("--lane", required=True)
    wake_send.add_argument(
        "--wait",
        type=_wait_seconds,
        default=0.0,
        metavar="SECONDS",
        help="watch this long for the ack and reply",
    )
    wake_send.add_argument("note_id", metavar="ID")
    wake_send.set_defaults(handler=_cmd_wake_send)
    wake_show = wake_actions.add_parser(
        "show",
        help="list wake bindings",
        description="One line per bound lane: lane, host, session id, permission mode, bound.",
        allow_abbrev=False,
    )
    wake_show.add_argument("--project", required=True)
    wake_show.set_defaults(handler=_cmd_wake_show)

    hook_cmd = commands.add_parser(
        "hook",
        help="the Claude Stop hook that surfaces unread unblocking notes",
        description="One user-level Claude Stop hook runs 'passing-notes hook stop' at the "
        "end of every turn in every Claude window. In a window bound to a lane it blocks "
        "the stop once per unread note that may ring, naming ids and headers, never bodies.",
        allow_abbrev=False,
    )
    hook_actions = hook_cmd.add_subparsers(dest="action", required=True, metavar="ACTION")
    hook_stop = hook_actions.add_parser(
        "stop",
        help="run as the Claude Stop hook (reads the host's JSON on standard input)",
        description="Reads session_id and stop_hook_active from the Stop-hook JSON on "
        'standard input. Prints {"decision": "block", "reason": ...} for notes not yet '
        "surfaced to this session, or nothing. Always exits 0; any error prints nothing.",
        allow_abbrev=False,
    )
    hook_stop.set_defaults(handler=_cmd_hook_stop)
    hook_install = hook_actions.add_parser(
        "install",
        help="add the Stop hook to a Claude settings file",
        description="Merge one Stop entry running 'passing-notes hook stop' (timeout "
        f"{hook.HOOK_TIMEOUT_S} s) into the settings file, keeping every other byte. "
        "Writes a .bak copy first; a second run writes nothing. Exit 3: not valid JSON, "
        "file unchanged.",
        allow_abbrev=False,
    )
    hook_install.add_argument(
        "--settings",
        metavar="PATH",
        help="the settings file (default: settings.json in CLAUDE_CONFIG_DIR, or ~/.claude)",
    )
    hook_install.add_argument(
        "--check",
        action="store_true",
        help="print 'installed' or 'missing' and write nothing",
    )
    hook_install.set_defaults(handler=_cmd_hook_install)
    hook_uninstall = hook_actions.add_parser(
        "uninstall",
        help="remove the Stop hook from a Claude settings file",
        description="Remove every Stop entry whose command is exactly 'passing-notes hook "
        "stop', and any group, hooks.Stop or hooks that leaves empty, keeping every other "
        "byte. Writes a .bak copy first; with no such entry it writes nothing. Exit 3: not "
        "valid JSON, file unchanged.",
        allow_abbrev=False,
    )
    hook_uninstall.add_argument(
        "--settings",
        metavar="PATH",
        help="the settings file (default: settings.json in CLAUDE_CONFIG_DIR, or ~/.claude)",
    )
    hook_uninstall.set_defaults(handler=_cmd_hook_uninstall)
    order_only = (
        "A queue orders turns on a shared resource and nothing more: a turn grants nothing, "
        "assigns no work, and is never a project's own lock."
    )
    reserve = commands.add_parser(
        "reserve",
        help="take the turn on a shared resource, or join its line",
        description="Take the turn on a free resource, or join the end of its line, and "
        "print the holder line (and your waiter line). Repeating it writes nothing. With "
        f"--cancel, leave the line. {order_only}",
        allow_abbrev=False,
    )
    reserve.add_argument("resource", metavar="RESOURCE", help="the resource id, such as engine")
    reserve.add_argument("--project", required=True)
    reserve.add_argument("--lane", required=True, help="your own lane")
    reserve.add_argument(
        "--minutes",
        type=_positive_minutes,
        metavar="MINUTES",
        help=f"the length of your turn (default {TURN_DEFAULT_MINUTES}); a waiter's applies "
        "from when it is promoted",
    )
    reserve.add_argument(
        "--reason",
        metavar="TEXT",
        help=f"why you need it, one line of at most {QUEUE_REASON_MAX_CHARS} characters",
    )
    reserve.add_argument(
        "--cancel",
        action="store_true",
        help="leave the line instead (a holder uses release); not in the line is a no-op",
    )
    reserve.set_defaults(handler=_cmd_reserve)

    release = commands.add_parser(
        "release",
        help="end your turn on a shared resource; the first waiter becomes holder",
        description="End your turn and make the first waiter the holder, then print the "
        "new holder line. A waiter whose card is done is dropped (a 'skipped' line). The "
        "new holder gets one reservation note from you, rung through the wake path; exit 5 "
        "when that doorbell is not delivered (the turn and the note stand). A lane not in "
        f"the queue prints '{NOTHING_TO_RELEASE}' and writes nothing, unless it granted the "
        "current turn and that turn's note is missing, which it then posts and rings. A "
        f"waiter is refused (use reserve --cancel). With --holder, the coordinator ends "
        "that lane's turn instead, exactly as its own release would, and tells the new "
        f"holder from the coordinator. {order_only}",
        allow_abbrev=False,
    )
    release.add_argument("resource", metavar="RESOURCE")
    release.add_argument("--project", required=True)
    release.add_argument("--lane", required=True, help="your own lane")
    release.add_argument(
        "--holder",
        metavar="LANE",
        help="coordinator only: end this lane's turn (it must hold the resource)",
    )
    release.set_defaults(handler=_cmd_release)

    queue_cmd = commands.add_parser(
        "queue",
        help="show and arrange resource queues",
        description=order_only,
        allow_abbrev=False,
    )
    queue_actions = queue_cmd.add_subparsers(dest="action", required=True, metavar="ACTION")
    queue_show = queue_actions.add_parser(
        "show",
        help="print who holds each resource and who waits, in order",
        description="Per resource: the holder line (resource, lane, granted, deadline; or "
        "'-' when free), then one waiter line per waiter in order (resource, position, "
        f"lane, since). Then at most {DIAGNOSTIC_LINES} diagnostic lines and a total count. "
        "Writes nothing.",
        allow_abbrev=False,
    )
    queue_show.add_argument("resource", metavar="RESOURCE", nargs="?")
    queue_show.add_argument("--project", required=True)
    queue_show.set_defaults(handler=_cmd_queue_show)

    coordinator_only = (
        f"Only the project's coordinator (lane {queue.DEFAULT_COORDINATOR} until queue config "
        "--coordinator names another) may do this; any other lane is refused with "
        "not-coordinator (exit 2) and nothing is written. Lanes declare their own ids, so "
        "this is identity, not security."
    )
    queue_move = queue_actions.add_parser(
        "move",
        help="coordinator: reorder the waiters of a resource",
        description="Move one waiter to place N among the waiters (--to, from 1) or just "
        "behind another waiter (--after), then print the queue lines. It reorders waiters "
        "only: the holder is never moved, and no one is rung. A move that leaves the order "
        f"as it was writes nothing. {coordinator_only}",
        allow_abbrev=False,
    )
    queue_move.add_argument("resource", metavar="RESOURCE")
    queue_move.add_argument("--project", required=True)
    queue_move.add_argument("--lane", required=True, help="your own lane, the coordinator")
    queue_move.add_argument("--waiter", required=True, metavar="LANE", help="the lane to move")
    place = queue_move.add_mutually_exclusive_group(required=True)
    place.add_argument("--to", type=_positive_whole, metavar="N", help="its new place, from 1")
    place.add_argument("--after", metavar="LANE", help="the waiter it moves behind")
    queue_move.set_defaults(handler=_cmd_queue_move)

    queue_config = queue_actions.add_parser(
        "config",
        help="print the queue config, or (coordinator) change it",
        description="With no setting flag, print the effective config, one tab-separated "
        "line each: coordinator, stall-minutes, default-turn-minutes, then turn-minutes "
        "per resource listed. Any lane may print it. With setting flags, change it and "
        f"print the result. {coordinator_only}",
        allow_abbrev=False,
    )
    queue_config.add_argument("--project", required=True)
    queue_config.add_argument("--lane", required=True, help="your own lane")
    queue_config.add_argument(
        "--coordinator", metavar="LANE", help="name the lane that coordinates the queues"
    )
    queue_config.add_argument(
        "--resource", metavar="RESOURCE", help="the resource whose default turn --minutes sets"
    )
    queue_config.add_argument(
        "--minutes",
        type=_positive_minutes,
        metavar="MINUTES",
        help=f"the default turn length on --resource (otherwise {TURN_DEFAULT_MINUTES})",
    )
    queue_config.add_argument(
        "--stall-minutes",
        type=_positive_minutes,
        metavar="MINUTES",
        help=f"how long a promoted holder may leave its turn unanswered before it is called "
        f"stalled (default {protocol.STALL_DEFAULT_MINUTES})",
    )
    queue_config.set_defaults(handler=_cmd_queue_config)

    queue_check = queue_actions.add_parser(
        "check",
        help="report overrun, stalled and (with --lock-holder) mismatched turns",
        description="Print one tab-separated row per finding: 'past-deadline' (resource, "
        "holder, minutes over its deadline), 'stalled' (resource, holder, minutes since a "
        "release granted it a turn whose reservation note it has not acknowledged within "
        "the stall time), and, only with --lock-holder, 'mismatch' (resource, queue holder "
        "or '-', lock holder or 'none'). Then one 'queue-check: past_deadline=<n> "
        f"stalled=<n> mismatch=<n>' line and at most {DIAGNOSTIC_LINES} diagnostic lines "
        "and a total count. A holder past its deadline is reported, never evicted; a "
        "mismatch is never fixed and no lock file is ever read. Without --ring it writes "
        "nothing. With --ring --lane, each stall gets one note to the holder and one to "
        "the coordinator, from you, rung through the wake path, and none to you. A stall "
        "note whose doorbell was delivered, by any lane's run, or that its recipient "
        "acknowledged prints 'stall: already rung <id>' and rings nothing; a stored note "
        "that was never rung is rung by the next run. Exit 5 when a doorbell is not "
        "delivered (the note stays).",
        allow_abbrev=False,
    )
    queue_check.add_argument("resource", metavar="RESOURCE", nargs="?")
    queue_check.add_argument("--project", required=True)
    queue_check.add_argument("--lane", help="your own lane; goes with --ring")
    queue_check.add_argument(
        "--ring",
        action="store_true",
        help="post and ring each stall's notes, once per stall (needs --lane)",
    )
    queue_check.add_argument(
        "--lock-holder",
        metavar="LANE|none",
        help="who the project's own lock script says holds RESOURCE's lock; a different "
        "queue holder prints a mismatch row (needs RESOURCE)",
    )
    queue_check.set_defaults(handler=_cmd_queue_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _harden_streams()
    try:
        code = _run(argv)
        _flush_stdout()  # a closed pipe surfaces here, not in the interpreter's exit
    except OutputError as exc:
        _silence_stdout()
        _error(exc.code, exc.message)
        return EXIT_OUTPUT
    return code


def _run(argv: Sequence[str] | None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: --help/--version (0) or a usage error (2)
        # argparse ignores its own write errors; main's final flush still reports them.
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    handler: Handler = args.handler
    try:
        return handler(args)
    except CliError as exc:
        _error(exc.code, exc.message)
        return exc.exit_code
    except wake.WakeError as exc:
        _error(exc.code, exc.message)
        return EXIT_WAKE
    except queue.QueueRefusal as exc:
        _error(exc.code, exc.message)
        return EXIT_USAGE
    except queue.QueueInvalidError as exc:
        _error(exc.code, exc.message)
        return EXIT_NOT_FOUND
    except ProtocolError as exc:
        _error(INVALID_ARGUMENT, str(exc))
        return EXIT_USAGE
    except NotFoundError as exc:
        _error(exc.code, exc.message)
        return EXIT_NOT_FOUND
    except StoreError as exc:
        _error(exc.code, exc.message)
        return EXIT_STORE
    except OSError as exc:
        # Body input and standard output errors are mapped where they happen (exit 2
        # and exit 1), so an OSError reaching here came from the store.
        _error(STORE_UNUSABLE, os_reason(exc))
        return EXIT_STORE
