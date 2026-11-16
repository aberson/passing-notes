"""Resource queues: one first-come turn order per named shared resource, per project.

Layout (docs/protocol.md, "Resource queues")::

    <root>/queues/<project>/<resource>.queue.json   one record per resource
    <root>/queues/<project>/<resource>.lock         transient exclusive lock
    <root>/queues/<project>/<resource>.break        transient: held to remove the lock

A queue records **order only**. A turn is a place in line the lane asked for: it
grants nothing, assigns no work, and is never a project's own lock file, which this
module never reads, takes, writes or deletes.

Write rule (plan P24). Every change takes the exclusive ``<resource>.lock`` (created
exclusively; broken once older than QUEUE_LOCK_STALE_S), held only for the
read-modify-write of the record. The lock is only ever removed, by its own writer or
as stale, while ``<resource>.break`` is held (also created exclusively), and the
remover checks again inside it that the lock is still its own or still stale. Nothing
else removes the lock, and nothing creates it while it exists, so a remover can never
delete a lock another writer has just taken. The new record is written to a fresh, exclusively
created ``<resource>.queue.json.<random hex>.tmp`` and moved over the record in one
atomic replace, as a lane card is (decision D15). So a reader finds the old record or
the new one, never part of one, and two changing verbs never lose each other's update.

A record that is invalid, oversize, or anything but a regular file is never acted on:
every changing verb refuses it (``queue-invalid``, exit 3) and writes nothing, and
``queue show`` reports it as a bounded diagnostic, as it does a leftover ``.tmp`` file,
which is never read (decision D32).

Telling the new holder (plan D23, D30). A release that grants a turn to another lane
returns the new record; after the lock is released, ``cli`` posts one reservation note
built by ``turn_draft`` and ``turn_body`` (fixed synthetic text, no caller content) and
rings it through the ordinary wake path. Its idempotency key ``turn_key`` names the
grant, so each grant has at most one note. At promotion a waiter whose card is ``done``
is dropped from the line and never told (plan D24).

The verbs here change records and return what changed; ``cli`` parses arguments and
prints. Turn times come from the caller's clock (``cli._now_utc``), so tests fix them.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from passing_notes import protocol
from passing_notes.protocol import (
    QUEUE_LOCK_STALE_S,
    QUEUE_MAX_BYTES,
    QUEUE_MAX_WAITERS,
    TURN_DEFAULT_MINUTES,
    Draft,
    OversizeError,
    ProtocolError,
)
from passing_notes.store import (
    NOT_FOUND,
    PUBLISH_FAILED,
    QUEUES_DIR,
    STORE_UNUSABLE,
    TMP_SUFFIX,
    CardNotFoundError,
    CodedError,
    Diagnostic,
    Store,
    StoreError,
    _read_bounded,
    _remove_quietly,
    display_name,
    os_reason,
    replace_file,
)

QUEUE_SUFFIX: Final = ".queue.json"
LOCK_SUFFIX: Final = ".lock"
BREAK_SUFFIX: Final = ".break"
QUEUE_NAME_SHAPE: Final = f"<resource>{QUEUE_SUFFIX}"
# The record's fields, in the order they are written.
RECORD_FIELDS: Final = ("resource", "project", "grant_seq", "holder", "waiters")
HOLDER_FIELDS: Final = ("lane", "granted_utc", "deadline_utc", "granted_by", "reason")
WAITER_FIELDS: Final = ("lane", "since_utc", "minutes", "reason")

# Stable error codes. QUEUE_INVALID exits 3; the refusals exit 2; QUEUE_BUSY exits 4.
QUEUE_INVALID: Final = "queue-invalid"
NOT_HOLDER: Final = "not-holder"
HOLDER_CANNOT_CANCEL: Final = "holder-cannot-cancel"
QUEUE_FULL: Final = "queue-full"
QUEUE_BUSY: Final = "queue-busy"

# A changing verb waits this long for the lock. It is longer than QUEUE_LOCK_STALE_S, so
# a lock left by a crashed writer is broken within the wait; a lock that stays fresh
# for all of it (a writer that never finishes) is reported as QUEUE_BUSY.
LOCK_WAIT_S: Final = QUEUE_LOCK_STALE_S + 5
_LOCK_POLL_S: Final = 0.01
_LOCK_TOKEN_BYTES: Final = 8
_LEFTOVER_TMP: Final = f"leftover {TMP_SUFFIX} file; never read as a queue record"
_NOT_A_QUEUE_NAME: Final = f"ignored: not named {QUEUE_NAME_SHAPE}"
_NOT_A_REGULAR_FILE: Final = "ignored: not a regular file"
# The latest deadline a record can hold: UTC_FORMAT has a four-digit year.
_LATEST: Final = datetime.max.replace(microsecond=0)


class QueueRefusal(CodedError):
    """A change the queue refuses before writing anything (exit 2)."""


class QueueInvalidError(CodedError):
    """The record is invalid, oversize or not a regular file; nothing is written (exit 3)."""


@dataclass(frozen=True)
class Holder:
    lane: str
    granted_utc: str
    deadline_utc: str
    granted_by: str
    reason: str | None


@dataclass(frozen=True)
class Waiter:
    lane: str
    since_utc: str
    # None: the turn length is decided when the waiter is promoted (the default then).
    minutes: int | None
    reason: str | None


@dataclass(frozen=True)
class Record:
    resource: str
    project: str
    grant_seq: int
    holder: Holder | None
    waiters: tuple[Waiter, ...]

    def position(self, lane: str) -> int | None:
        """``lane``'s place among the waiters, from 1; None if it is not waiting."""
        for index, waiter in enumerate(self.waiters, start=1):
            if waiter.lane == lane:
                return index
        return None

    def holds(self, lane: str) -> bool:
        return self.holder is not None and self.holder.lane == lane


def empty_record(project: str, resource: str) -> Record:
    """A queue nobody has reserved: no holder, no waiters, no grant yet."""
    return Record(resource, project, 0, None, ())


# --- Turn times ------------------------------------------------------------------------


def deadline(granted: datetime, minutes: int) -> str:
    """``granted`` plus ``minutes``, as UTC text; capped at the latest time UTC text holds."""
    try:
        moment = granted + timedelta(minutes=minutes)
    except OverflowError:
        moment = _LATEST.replace(tzinfo=granted.tzinfo)
    return protocol.format_utc(moment)


def turn_minutes(minutes: int | None) -> int:
    """The length of a turn being granted: its own ``--minutes``, else the default.

    The seam for a per-resource default (plan Step 7).
    """
    return TURN_DEFAULT_MINUTES if minutes is None else minutes


def grant(
    record: Record, lane: str, by: str, now: datetime, minutes: int | None, reason: str | None
) -> Record:
    """``record`` with ``lane`` as its holder from ``now``, granted by ``by``.

    Every grant increments ``grant_seq``, so each turn has its own number.
    """
    holder = Holder(
        lane=lane,
        granted_utc=protocol.format_utc(now),
        deadline_utc=deadline(now, turn_minutes(minutes)),
        granted_by=by,
        reason=reason,
    )
    return replace(record, grant_seq=record.grant_seq + 1, holder=holder)


def never_skip(lane: str) -> bool:
    """The skip test of a promotion that skips no waiter."""
    return False


@dataclass(frozen=True)
class Promotion:
    """``promote_next``'s result: the new record and the waiters dropped on the way."""

    record: Record
    skipped: tuple[str, ...]


def promote_next(
    record: Record, by: str, now: datetime, skip: Callable[[str], bool] = never_skip
) -> Promotion:
    """End the current turn: the first waiter not skipped becomes holder, granted by ``by``.

    Each waiter at the front for which ``skip(lane)`` is true is dropped from the line
    (plan D24: its card is done). With no waiter left the resource is left free. Only
    the one grant increments ``grant_seq``; a dropped waiter gets no turn number.
    """
    skipped: list[str] = []
    waiters = list(record.waiters)
    while waiters and skip(waiters[0].lane):
        skipped.append(waiters.pop(0).lane)
    if not waiters:
        return Promotion(replace(record, holder=None, waiters=()), tuple(skipped))
    first, *rest = waiters
    promoted = grant(record, first.lane, by, now, first.minutes, first.reason)
    return Promotion(replace(promoted, waiters=tuple(rest)), tuple(skipped))


# --- The reservation note -------------------------------------------------------------

# A reservation note asks the new holder for an answer, so ``wake.ring_reason`` rings it
# as it stands (``needs: reply``); the ring policy is not changed for queues.
TURN_NOTE_KIND: Final = "reservation"
TURN_NOTE_NEEDS: Final = "reply"
TURN_KEY_PREFIX: Final = "turn"
# The subjects of the new holder's answer (docs/lane-rules.md, "Shared resources").
TAKEN: Final = "taken"
PASSING: Final = "passing"


def turn_key(resource: str, grant_seq: int) -> str:
    """``turn-<resource>-<grant_seq>``: the key of that grant's one reservation note.

    A resource id is at most 32 characters, so the key fits the 64-character token
    limit for any turn number below 10**26; make_draft checks the token in any case.
    """
    return f"{TURN_KEY_PREFIX}-{resource}-{grant_seq}"


def _holder_of(record: Record) -> Holder:
    if record.holder is None:
        raise ValueError(f"{record.resource} is free: there is no holder to tell")
    return record.holder


def turn_draft(record: Record, sender: str) -> Draft:
    """The reservation note from ``sender`` telling the holder of ``record`` its turn."""
    holder = _holder_of(record)
    return protocol.make_draft(
        project=record.project,
        from_lane=sender,
        to=holder.lane,
        subject=f"Your turn on {record.resource}",
        kind=TURN_NOTE_KIND,
        needs=TURN_NOTE_NEEDS,
        work=record.resource,
        idempotency_key=turn_key(record.resource, record.grant_seq),
    )


def turn_body(record: Record, sender: str) -> str:
    """The reservation note's body: fixed text over ids and times, no caller content."""
    holder = _holder_of(record)
    resource, project, lane = record.resource, record.project, holder.lane
    release_cmd = f"passing-notes release {resource} --project {project} --lane {lane}"
    started = (
        f"Your turn on {resource} in project {project} has started: lane {sender} "
        f"ended the last turn and lane {lane} now holds {resource}."
    )
    times = (
        f"Turn {record.grant_seq}, granted {holder.granted_utc}, deadline {holder.deadline_utc}."
    )
    meaning = (
        "The queue orders turns only: take the project's own lock by its own rules. "
        "This note grants nothing else and assigns no work."
    )
    answer = (
        f"Answer this note once, without --wake: passing-notes post --project {project} "
        f"--from {lane} --to {sender} --kind answer --reply-to <this note's id> "
        f'--needs fyi --subject "{TAKEN} {resource}" when you take the turn, or '
        f'--subject "{PASSING} {resource}" when you pass it, with a short body file.'
    )
    passing = f"If you pass, then run {release_cmd} so the next waiter gets the turn."
    done = f"When your turn is done, run {release_cmd}."
    return f"{started}\n{times}\n{meaning}\n\n{answer}\n{passing}\n{done}"


# --- Cards at promotion ---------------------------------------------------------------

CARD_STATE_DONE: Final = "done"


@dataclass
class CardCheck:
    """The skip test of a release: a waiter whose card is ``done`` is dropped (D24).

    A waiter with no card is promoted, and the wake path decides at ring time. A waiter
    whose card is invalid is promoted too, as not done, and its lane is kept in
    ``invalid`` so the caller reports it rather than passing over it silently.
    """

    store: Store
    project: str
    invalid: list[str]

    def __call__(self, lane: str) -> bool:
        try:
            card = self.store.load_card(self.project, lane)
        except CardNotFoundError as exc:
            if exc.code != NOT_FOUND:
                self.invalid.append(lane)
            return False
        return card.state == CARD_STATE_DONE


# --- The record format ----------------------------------------------------------------


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    duplicate = next((key for key in keys if keys.count(key) > 1), None)
    if duplicate is not None:
        raise ProtocolError(f"field {protocol.show(duplicate)} appears more than once")
    return dict(pairs)


def _no_constant(name: str) -> object:
    raise ProtocolError(f"{name} is not a JSON number")


def _fields(value: object, names: tuple[str, ...], what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{what}: expected an object")
    if set(value) != set(names):
        missing = sorted(set(names) - set(value))
        extra = sorted(set(value) - set(names))
        detail = "; ".join(
            part
            for part in (
                f"missing {', '.join(missing)}" if missing else "",
                f"unknown {', '.join(protocol.show(k) for k in extra)}" if extra else "",
            )
            if part
        )
        raise ProtocolError(f"{what}: fields must be {', '.join(names)} ({detail})")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{field}: expected text")
    return value


def _count(value: object, field: str, least: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < least:
        raise ProtocolError(f"{field}: expected a whole number of at least {least}")
    return value


def _reason(value: object, field: str) -> str | None:
    if value is None:
        return None
    return protocol.check_queue_reason(_text(value, field), field)


def _holder(value: object) -> Holder | None:
    if value is None:
        return None
    data = _fields(value, HOLDER_FIELDS, "holder")
    return Holder(
        lane=protocol.check_lane_id(_text(data["lane"], "holder.lane"), "holder.lane"),
        granted_utc=protocol.check_utc(
            _text(data["granted_utc"], "holder.granted_utc"), "holder.granted_utc"
        ),
        deadline_utc=protocol.check_utc(
            _text(data["deadline_utc"], "holder.deadline_utc"), "holder.deadline_utc"
        ),
        granted_by=protocol.check_lane_id(
            _text(data["granted_by"], "holder.granted_by"), "holder.granted_by"
        ),
        reason=_reason(data["reason"], "holder.reason"),
    )


def _waiter(value: object, n: int) -> Waiter:
    what = f"waiters[{n}]"
    data = _fields(value, WAITER_FIELDS, what)
    minutes = data["minutes"]
    return Waiter(
        lane=protocol.check_lane_id(_text(data["lane"], f"{what}.lane"), f"{what}.lane"),
        since_utc=protocol.check_utc(
            _text(data["since_utc"], f"{what}.since_utc"), f"{what}.since_utc"
        ),
        minutes=None if minutes is None else _count(minutes, f"{what}.minutes", 1),
        reason=_reason(data["reason"], f"{what}.reason"),
    )


def parse_record(data: bytes, *, project: str, resource: str) -> Record:
    """The record in ``data`` for ``resource`` in ``project``; ProtocolError if invalid.

    Strict: exactly the documented fields, no repeated key, the folder's project and
    the file's resource, each lane at most once, and waiters only behind a holder.
    """
    if len(data) > QUEUE_MAX_BYTES:
        raise OversizeError(f"{len(data)} bytes or more (limit {QUEUE_MAX_BYTES})")
    text = protocol.decode_text(data, "queue record")
    try:
        loaded: object = json.loads(text, object_pairs_hook=_object, parse_constant=_no_constant)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"not valid JSON: {exc.msg} at line {exc.lineno}") from None
    except ProtocolError:
        raise
    except ValueError:
        # A number past Python's digit limit for int conversion (sys.int_info).
        raise ProtocolError("not valid JSON: a number has too many digits") from None
    except RecursionError:
        raise ProtocolError("not valid JSON: nested too deeply") from None
    top = _fields(loaded, RECORD_FIELDS, "record")
    if top["resource"] != resource:
        raise ProtocolError(f"resource: {protocol.show(str(top['resource']))} is not {resource}")
    if top["project"] != project:
        raise ProtocolError(f"project: {protocol.show(str(top['project']))} is not {project}")
    grant_seq = _count(top["grant_seq"], "grant_seq", 0)
    holder = _holder(top["holder"])
    listed = top["waiters"]
    if not isinstance(listed, list):
        raise ProtocolError("waiters: expected a list")
    if len(listed) > QUEUE_MAX_WAITERS:
        raise ProtocolError(f"waiters: more than {QUEUE_MAX_WAITERS}")
    waiters = tuple(_waiter(item, n) for n, item in enumerate(listed))
    lanes = [w.lane for w in waiters] + ([holder.lane] if holder else [])
    repeated = next((lane for lane in lanes if lanes.count(lane) > 1), None)
    if repeated is not None:
        raise ProtocolError(f"lane {repeated} is in the queue more than once")
    if holder is None and waiters:
        raise ProtocolError("waiters: a queue with waiters must have a holder")
    if holder is not None and grant_seq < 1:
        raise ProtocolError("grant_seq: a queue with a holder has had at least one grant")
    return Record(resource, project, grant_seq, holder, waiters)


def render_record(record: Record) -> bytes:
    """The record as UTF-8 JSON in field order; QueueRefusal if over QUEUE_MAX_BYTES."""
    holder = record.holder
    data = {
        "resource": record.resource,
        "project": record.project,
        "grant_seq": record.grant_seq,
        "holder": None
        if holder is None
        else {name: getattr(holder, name) for name in HOLDER_FIELDS},
        "waiters": [{name: getattr(w, name) for name in WAITER_FIELDS} for w in record.waiters],
    }
    body = (json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    if len(body) > QUEUE_MAX_BYTES:
        raise QueueRefusal(
            QUEUE_FULL,
            f"the {record.resource} queue record would be {len(body)} bytes, over the "
            f"{QUEUE_MAX_BYTES}-byte limit; nothing written (shorter --reason texts fit)",
        )
    return body


# --- Paths, reading and locking -------------------------------------------------------


def record_name(resource: str) -> str:
    return f"{protocol.check_resource_id(resource)}{QUEUE_SUFFIX}"


def record_path(store: Store, project: str, resource: str) -> Path:
    """``queues/<project>/<resource>.queue.json``; ids checked and links refused first."""
    name = record_name(resource)
    return store.queues_dir(project) / name


def _rel(project: str, name: str) -> str:
    return f"{QUEUES_DIR}/{project}/{display_name(name)}"


def _exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise StoreError(STORE_UNUSABLE, f"cannot inspect {path}: {os_reason(exc)}") from None
    return True


def load(store: Store, project: str, resource: str) -> Record:
    """The record of ``resource``; an empty record if there is none.

    QueueInvalidError if anything but a valid record is at its name.
    """
    path = record_path(store, project, resource)
    rel = _rel(project, path.name)
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return empty_record(project, resource)
    except OSError as exc:
        raise StoreError(STORE_UNUSABLE, f"cannot inspect {path}: {os_reason(exc)}") from None
    if not stat.S_ISREG(info.st_mode):
        raise QueueInvalidError(QUEUE_INVALID, f"{rel} is not a regular file; nothing written")
    if info.st_size > QUEUE_MAX_BYTES:
        raise QueueInvalidError(
            QUEUE_INVALID,
            f"{rel} is oversize ({info.st_size} bytes, limit {QUEUE_MAX_BYTES}); nothing written",
        )
    try:
        data = _read_bounded(path, QUEUE_MAX_BYTES)
    except OSError as exc:
        raise StoreError(STORE_UNUSABLE, f"cannot read {path}: {os_reason(exc)}") from None
    try:
        return parse_record(data, project=project, resource=resource)
    except ProtocolError as exc:
        raise QueueInvalidError(
            QUEUE_INVALID, f"{rel} is invalid: {exc}; nothing written"
        ) from None


def _try_lock(lock: Path, token: bytes) -> bool:
    """Create ``lock`` exclusively holding ``token``; False if something is at its name."""
    try:
        with open(lock, "xb") as handle:
            handle.write(token)
        return True
    except FileExistsError:
        return False
    except PermissionError:
        # Windows refuses to create a name whose earlier file is still being deleted.
        if os.name == "nt":
            return False
        raise StoreError(STORE_UNUSABLE, f"cannot create {lock}: permission denied") from None
    except OSError as exc:
        raise StoreError(STORE_UNUSABLE, f"cannot create {lock}: {os_reason(exc)}") from None


def _age(path: Path) -> float | None:
    """Seconds since ``path`` (a lock) was written; None if nothing is at its name."""
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise StoreError(STORE_UNUSABLE, f"cannot inspect {path}: {os_reason(exc)}") from None
    if not stat.S_ISREG(info.st_mode):
        raise StoreError(
            STORE_UNUSABLE, f"{path} is not a regular file, so it is not a queue lock; remove it"
        )
    return time.time() - info.st_mtime


def _breaker(lock: Path) -> Path:
    return lock.with_suffix(BREAK_SUFFIX)


def _take_breaker(breaker: Path, token: bytes) -> bool:
    """Try once to hold ``breaker``. One left by a crashed remover is removed when stale.

    A remover holds it only for one check and one removal, so a stale one is rare.
    """
    if _try_lock(breaker, token):
        return True
    age = _age(breaker)
    if age is not None and age > QUEUE_LOCK_STALE_S:
        _remove_quietly(breaker)
    return False


def _drop_breaker(breaker: Path, token: bytes) -> None:
    try:
        held = _read_bounded(breaker, len(token))
    except OSError:
        return
    if held == token:
        _remove_quietly(breaker)


def _break_if_stale(lock: Path, token: bytes) -> None:
    """Remove ``lock`` if it is older than QUEUE_LOCK_STALE_S (its writer has died).

    The removal happens only while holding the breaker, after checking the age again
    inside it: another waiter may have broken the same stale lock and taken a fresh
    one since the first look, and that fresh lock must stay.
    """
    age = _age(lock)
    if age is None or age <= QUEUE_LOCK_STALE_S:
        return
    breaker = _breaker(lock)
    if not _take_breaker(breaker, token):
        return  # another remover is at work; look again on the next try
    try:
        age = _age(lock)
        if age is not None and age > QUEUE_LOCK_STALE_S:
            _remove_quietly(lock)
    finally:
        _drop_breaker(breaker, token)


def acquire_lock(lock: Path, token: bytes) -> None:
    """Take the exclusive lock at ``lock``, waiting up to LOCK_WAIT_S for it."""
    give_up = time.monotonic() + LOCK_WAIT_S
    while not _try_lock(lock, token):
        _break_if_stale(lock, token)
        if time.monotonic() > give_up:
            raise StoreError(
                QUEUE_BUSY,
                f"{lock} stayed locked for {LOCK_WAIT_S} seconds; another writer is changing "
                "this queue now, so try again",
            )
        time.sleep(_LOCK_POLL_S)


def release_lock(lock: Path, token: bytes) -> None:
    """Remove the lock if it is still this writer's (it was not broken as stale).

    The check and the removal happen while holding the breaker, so a lock broken as
    stale and taken by another writer in between is never removed. If the breaker
    cannot be had within LOCK_WAIT_S the lock is left, to be broken once stale.
    """
    breaker = _breaker(lock)
    give_up = time.monotonic() + LOCK_WAIT_S
    while not _take_breaker(breaker, token):
        if time.monotonic() > give_up:
            return
        time.sleep(_LOCK_POLL_S)
    try:
        try:
            held = _read_bounded(lock, len(token))
        except OSError:
            return
        if held == token:
            _remove_quietly(lock)
    finally:
        _drop_breaker(breaker, token)


@contextmanager
def locked(store: Store, project: str, resource: str) -> Iterator[Path]:
    """Hold ``<resource>.lock`` (creating the folders if needed); yield the record path."""
    name = record_name(resource)
    folder = store.make_queues_dir(project)
    lock = folder / f"{resource}{LOCK_SUFFIX}"
    token = secrets.token_hex(_LOCK_TOKEN_BYTES).encode("ascii")
    acquire_lock(lock, token)
    try:
        yield folder / name
    finally:
        release_lock(lock, token)


def write(store: Store, path: Path, record: Record) -> None:
    """Replace the record at ``path``: a fresh exclusive ``.tmp`` file, then one replace.

    Called only while the resource's lock is held.
    """
    data = render_record(record)
    tmp = store._fresh_temp(path, data)
    if tmp is None:
        raise StoreError(
            PUBLISH_FAILED,
            f"cannot write the {record.resource} queue: {protocol.PUBLISH_ATTEMPTS} temporary "
            f"names in {QUEUES_DIR}/{record.project} were all taken",
        )
    try:
        replace_file(tmp, path)
    except OSError as exc:
        _remove_quietly(tmp)
        raise StoreError(STORE_UNUSABLE, f"cannot replace {path}: {os_reason(exc)}") from None


# --- The verbs ------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """A verb's result: the record afterwards, and whether anything was written."""

    record: Record
    changed: bool
    # release only: the waiters dropped at promotion because their card is done (D24),
    # and the waiters whose card was invalid when checked (promoted as not done).
    skipped: tuple[str, ...] = ()
    invalid_cards: tuple[str, ...] = ()


def reserve(
    store: Store,
    project: str,
    resource: str,
    lane: str,
    now: datetime,
    *,
    minutes: int | None = None,
    reason: str | None = None,
) -> Change:
    """Take the turn on a free resource, or join the end of its line.

    A lane already holding or waiting changes nothing (decision D22).
    """
    with locked(store, project, resource) as path:
        record = load(store, project, resource)
        if record.holds(lane) or record.position(lane) is not None:
            return Change(record, False)
        if record.holder is None:
            record = grant(record, lane, lane, now, minutes, reason)
        else:
            if len(record.waiters) >= QUEUE_MAX_WAITERS:
                raise QueueRefusal(
                    QUEUE_FULL,
                    f"the {resource} queue already has {QUEUE_MAX_WAITERS} waiters, the most "
                    "it holds; nothing written",
                )
            waiter = Waiter(lane, protocol.format_utc(now), minutes, reason)
            record = replace(record, waiters=(*record.waiters, waiter))
        write(store, path, record)
        return Change(record, True)


def _refuse_waiter_release(record: Record, lane: str) -> None:
    position = record.position(lane)
    if position is not None:
        raise QueueRefusal(
            NOT_HOLDER,
            f"lane {lane} is waiting at position {position} for {record.resource}, not "
            "holding it; nothing written. Use reserve --cancel to leave the line",
        )


def release(
    store: Store,
    project: str,
    resource: str,
    lane: str,
    now: datetime,
) -> Change:
    """End ``lane``'s turn and promote the first waiter (granted by ``lane``).

    A lane not in the queue changes nothing; a waiter is refused (``not-holder``).
    Waiters at the front whose card is ``done`` are dropped in the same write (D24).
    The returned record names the new holder and its ``grant_seq``, which ``cli``
    uses, after the lock is released, to post and ring the reservation note. For a
    lane not in the queue the record read under the lock is returned, so ``cli`` can
    tell whether that lane granted the current turn (the D22 retry).
    """
    path = record_path(store, project, resource)
    if not _exists(path):
        return Change(empty_record(project, resource), False)  # nobody holds it
    with locked(store, project, resource) as path:
        record = load(store, project, resource)
        if not record.holds(lane):
            _refuse_waiter_release(record, lane)
            return Change(record, False)
        check = CardCheck(store, project, [])
        promotion = promote_next(record, lane, now, check)
        write(store, path, promotion.record)
        return Change(promotion.record, True, promotion.skipped, tuple(check.invalid))


def cancel(store: Store, project: str, resource: str, lane: str) -> Change:
    """Take ``lane`` out of the line. The holder is refused (``holder-cannot-cancel``)."""
    path = record_path(store, project, resource)
    if not _exists(path):
        return Change(empty_record(project, resource), False)
    with locked(store, project, resource) as path:
        record = load(store, project, resource)
        if record.holds(lane):
            raise QueueRefusal(
                HOLDER_CANNOT_CANCEL,
                f"lane {lane} holds {resource}; nothing written. Use release to end the turn",
            )
        if record.position(lane) is None:
            return Change(record, False)
        record = replace(record, waiters=tuple(w for w in record.waiters if w.lane != lane))
        write(store, path, record)
        return Change(record, True)


# --- Showing --------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueView:
    records: tuple[Record, ...]
    diagnostics: tuple[Diagnostic, ...]


def _belongs(name: str, resource: str | None) -> bool:
    if resource is None:
        return True
    record = f"{resource}{QUEUE_SUFFIX}"
    transient = (f"{resource}{LOCK_SUFFIX}", f"{resource}{BREAK_SUFFIX}")
    return name == record or name in transient or name.startswith(f"{record}.")


def show(store: Store, project: str, resource: str | None = None) -> QueueView:
    """Every valid record in the project (or only ``resource``'s), sorted, with diagnostics.

    Read-only. A damaged record and a leftover ``.tmp`` file are reported, never acted
    on; the ``.tmp`` is never read. With ``resource`` and no record, the resource is
    shown free.
    """
    protocol.check_project_id(project, "--project")
    if resource is not None:
        protocol.check_resource_id(resource, "RESOURCE")
    folder = store.queues_dir(project)
    rel_dir = f"{QUEUES_DIR}/{project}"
    diagnostics: list[Diagnostic] = []
    records: list[Record] = []
    present: set[str] = set()
    for entry in store._list(folder, rel_dir, diagnostics):
        name = entry.name
        if not _belongs(name, resource):
            continue
        rel = f"{rel_dir}/{display_name(name)}"
        if name.endswith(TMP_SUFFIX):
            diagnostics.append(Diagnostic(rel, _LEFTOVER_TMP))
            continue
        if _is_transient(name):
            if not _is_regular(entry):
                diagnostics.append(Diagnostic(rel, _NOT_A_REGULAR_FILE))
            continue  # a lock or breaker is transient: a change is in progress
        stem = name.removesuffix(QUEUE_SUFFIX)
        if stem == name or not _is_resource(stem):
            diagnostics.append(Diagnostic(rel, _NOT_A_QUEUE_NAME))
            continue
        present.add(stem)

        def parse(data: bytes, stem: str = stem) -> Record:
            return parse_record(data, project=project, resource=stem)

        record = store._read_entry(entry, rel, QUEUE_MAX_BYTES, parse, diagnostics)
        if record is not None:
            records.append(record)
    if resource is not None and resource not in present:
        records.append(empty_record(project, resource))
    return QueueView(tuple(sorted(records, key=lambda r: r.resource)), tuple(diagnostics))


def _is_resource(stem: str) -> bool:
    try:
        protocol.check_resource_id(stem)
    except ProtocolError:
        return False
    return True


def _is_transient(name: str) -> bool:
    """``<resource>.lock`` or ``<resource>.break``."""
    return any(
        name.endswith(suffix) and _is_resource(name.removesuffix(suffix))
        for suffix in (LOCK_SUFFIX, BREAK_SUFFIX)
    )


def _is_regular(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False
