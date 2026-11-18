"""Resource queues: one first-come turn order per named shared resource, per project.

Layout (docs/protocol.md, "Resource queues")::

    <root>/queues/<project>/<resource>.queue.json   one record per resource
    <root>/queues/<project>/<resource>.lock         transient exclusive lock
    <root>/queues/<project>/<resource>.break        transient: held to remove the lock
    <root>/queues/<project>/config.json             the project's queue config
    <root>/queues/<project>/config.json.lock        transient: the config's own lock
    <root>/queues/<project>/config.json.break       transient: held to remove that lock
    <root>/queues/<project>/<resource>.stall.lock   transient: held to post a stall's notes
    <root>/queues/<project>/<resource>.stall.break  transient: held to remove that lock

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

The coordinator (plan P21, P22, P26, D27). ``config.json`` names the project's
coordinator lane (``main`` while it names none) and the default turn length per
resource. Only the coordinator changes the config, reorders waiters (``move``) and ends
another lane's turn (``release`` with ``holder``); any other lane is refused
(``not-coordinator``) before anything is written. Lanes self-declare their ids, as for
cards, so this is identity, not security. The config is written by the same rule as a
record, under its own ``config.json.lock``: no resource id holds a dot, so no resource's
file names can equal the config's. A damaged config makes every changing verb refuse,
as a damaged record does.

Checking (plan P25, D26, D28, D36). ``check`` reads records, the config, notes and acks
and writes nothing: a holder past its deadline is reported, never evicted; a holder
promoted by a release that has not acknowledged its reservation note within the stall
time is reported stalled; and a lock holder the caller names (the project's own lock
script says who holds its lock; no lock file is ever read here) is compared with the
queue's holder. ``post_stall`` posts a stall's notes (``stall_draft``,
``stall_coord_draft``), keyed per turn, while holding ``<resource>.stall.lock``: it
checks again inside that lock that the turn is still stalled and posts only the note
of each recipient not yet told, so concurrent runs and a retry after a crash tell each
recipient once. That lock is not the record's lock (P24 never holds a record's lock
while posting), and ``cli`` rings the posted notes after it is released.

The verbs here change records and return what changed; ``cli`` parses arguments and
prints. Turn times come from the caller's clock (``cli._now_utc``), so tests fix them.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import time
from collections.abc import Callable, Container, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, TypeVar

from passing_notes import protocol
from passing_notes.protocol import (
    QUEUE_LOCK_STALE_S,
    QUEUE_MAX_BYTES,
    QUEUE_MAX_WAITERS,
    STALL_DEFAULT_MINUTES,
    TURN_DEFAULT_MINUTES,
    Draft,
    Note,
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

# The project's queue config (plan P21, P22). A resource id has no dot, so these names
# can never be a resource's record, lock or breaker.
CONFIG_NAME: Final = "config.json"
CONFIG_LOCK_NAME: Final = f"{CONFIG_NAME}{LOCK_SUFFIX}"
# The config's fields, in the order they are written. ``coordinator`` and
# ``stall_minutes`` are left out while unset; ``resource_minutes`` is always written.
CONFIG_FIELDS: Final = ("project", "coordinator", "stall_minutes", "resource_minutes")
CONFIG_REQUIRED: Final = ("project", "resource_minutes")
# The coordinator while the config names none (docs/lane-rules.md, section 8).
DEFAULT_COORDINATOR: Final = protocol.MAIN_LANE

# Stable error codes. QUEUE_INVALID exits 3; the refusals exit 2; QUEUE_BUSY exits 4.
QUEUE_INVALID: Final = "queue-invalid"
NOT_HOLDER: Final = "not-holder"
HOLDER_CANNOT_CANCEL: Final = "holder-cannot-cancel"
QUEUE_FULL: Final = "queue-full"
QUEUE_BUSY: Final = "queue-busy"
NOT_COORDINATOR: Final = "not-coordinator"
NOT_WAITING: Final = "not-waiting"
HOLDER_CANNOT_MOVE: Final = "holder-cannot-move"
POSITION_OUT_OF_RANGE: Final = "position-out-of-range"

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
_Loaded = TypeVar("_Loaded")


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


def turn_minutes(minutes: int | None, default: int = TURN_DEFAULT_MINUTES) -> int:
    """The length of a turn being granted: its own ``--minutes``, else ``default``.

    ``default`` is the resource's turn length from the config (``Config.turn_minutes``),
    which is TURN_DEFAULT_MINUTES for a resource the config does not list (plan P22).
    """
    return default if minutes is None else minutes


def grant(
    record: Record,
    lane: str,
    by: str,
    now: datetime,
    minutes: int | None,
    reason: str | None,
    *,
    default_minutes: int = TURN_DEFAULT_MINUTES,
) -> Record:
    """``record`` with ``lane`` as its holder from ``now``, granted by ``by``.

    The turn lasts ``minutes``, else ``default_minutes``. Every grant increments
    ``grant_seq``, so each turn has its own number.
    """
    holder = Holder(
        lane=lane,
        granted_utc=protocol.format_utc(now),
        deadline_utc=deadline(now, turn_minutes(minutes, default_minutes)),
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
    record: Record,
    by: str,
    now: datetime,
    skip: Callable[[str], bool] = never_skip,
    *,
    default_minutes: int = TURN_DEFAULT_MINUTES,
) -> Promotion:
    """End the current turn: the first waiter not skipped becomes holder, granted by ``by``.

    Each waiter at the front for which ``skip(lane)`` is true is dropped from the line
    (plan D24: its card is done). With no waiter left the resource is left free. Only
    the one grant increments ``grant_seq``; a dropped waiter gets no turn number. A
    waiter that asked for no ``minutes`` gets ``default_minutes``, decided now.
    """
    skipped: list[str] = []
    waiters = list(record.waiters)
    while waiters and skip(waiters[0].lane):
        skipped.append(waiters.pop(0).lane)
    if not waiters:
        return Promotion(replace(record, holder=None, waiters=()), tuple(skipped))
    first, *rest = waiters
    promoted = grant(
        record, first.lane, by, now, first.minutes, first.reason, default_minutes=default_minutes
    )
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


def _load_json(data: bytes, what: str) -> object:
    """``data`` as strict JSON: UTF-8 without BOM, no repeated key, no NaN or Infinity."""
    text = protocol.decode_text(data, what)
    try:
        return json.loads(text, object_pairs_hook=_object, parse_constant=_no_constant)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"not valid JSON: {exc.msg} at line {exc.lineno}") from None
    except ProtocolError:
        raise
    except ValueError:
        # A number past Python's digit limit for int conversion (sys.int_info).
        raise ProtocolError("not valid JSON: a number has too many digits") from None
    except RecursionError:
        raise ProtocolError("not valid JSON: nested too deeply") from None


def parse_record(data: bytes, *, project: str, resource: str) -> Record:
    """The record in ``data`` for ``resource`` in ``project``; ProtocolError if invalid.

    Strict: exactly the documented fields, no repeated key, the folder's project and
    the file's resource, each lane at most once, and waiters only behind a holder.
    """
    if len(data) > QUEUE_MAX_BYTES:
        raise OversizeError(f"{len(data)} bytes or more (limit {QUEUE_MAX_BYTES})")
    top = _fields(_load_json(data, "queue record"), RECORD_FIELDS, "record")
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


# --- The config -----------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """The project's queue config; every field left unset takes its default (P21, P22)."""

    project: str
    coordinator: str | None = None
    stall_minutes: int | None = None
    # The default turn length per resource, sorted by resource id.
    resource_minutes: tuple[tuple[str, int], ...] = ()

    @property
    def effective_coordinator(self) -> str:
        """The coordinator lane: the one named, else ``main``."""
        return DEFAULT_COORDINATOR if self.coordinator is None else self.coordinator

    @property
    def effective_stall_minutes(self) -> int:
        return STALL_DEFAULT_MINUTES if self.stall_minutes is None else self.stall_minutes

    def turn_minutes(self, resource: str) -> int:
        """The default turn length on ``resource``: its own entry, else 60 minutes."""
        return dict(self.resource_minutes).get(resource, TURN_DEFAULT_MINUTES)


def _minutes_map(value: object) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, dict):
        raise ProtocolError("resource_minutes: expected an object")
    pairs: list[tuple[str, int]] = []
    for resource, minutes in value.items():
        protocol.check_resource_id(resource, "resource_minutes")
        pairs.append((resource, _count(minutes, f"resource_minutes.{resource}", 1)))
    return tuple(sorted(pairs))


def parse_config(data: bytes, *, project: str) -> Config:
    """The config in ``data`` for ``project``; ProtocolError if invalid.

    Strict, as a record is: only the documented fields, with ``project`` and
    ``resource_minutes`` always present, no repeated key, and the folder's project.
    """
    if len(data) > QUEUE_MAX_BYTES:
        raise OversizeError(f"{len(data)} bytes or more (limit {QUEUE_MAX_BYTES})")
    loaded = _load_json(data, "queue config")
    if not isinstance(loaded, dict):
        raise ProtocolError("config: expected an object")
    missing = [name for name in CONFIG_REQUIRED if name not in loaded]
    extra = sorted(name for name in loaded if name not in CONFIG_FIELDS)
    if missing or extra:
        detail = "; ".join(
            part
            for part in (
                f"missing {', '.join(missing)}" if missing else "",
                f"unknown {', '.join(protocol.show(k) for k in extra)}" if extra else "",
            )
            if part
        )
        raise ProtocolError(
            f"config: fields must be {', '.join(CONFIG_REQUIRED)}, and optionally "
            f"coordinator and stall_minutes ({detail})"
        )
    if loaded["project"] != project:
        raise ProtocolError(f"project: {protocol.show(str(loaded['project']))} is not {project}")
    coordinator: str | None = None
    if "coordinator" in loaded:
        coordinator = protocol.check_lane_id(
            _text(loaded["coordinator"], "coordinator"), "coordinator"
        )
    stall: int | None = None
    if "stall_minutes" in loaded:
        stall = _count(loaded["stall_minutes"], "stall_minutes", 1)
    return Config(project, coordinator, stall, _minutes_map(loaded["resource_minutes"]))


def render_config(config: Config) -> bytes:
    """The config as UTF-8 JSON in field order; QueueRefusal if over QUEUE_MAX_BYTES."""
    data: dict[str, object] = {"project": config.project}
    if config.coordinator is not None:
        data["coordinator"] = config.coordinator
    if config.stall_minutes is not None:
        data["stall_minutes"] = config.stall_minutes
    data["resource_minutes"] = dict(config.resource_minutes)
    body = (json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    if len(body) > QUEUE_MAX_BYTES:
        raise QueueRefusal(
            QUEUE_FULL,
            f"the queue config would be {len(body)} bytes, over the {QUEUE_MAX_BYTES}-byte "
            "limit; nothing written",
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

    def parse(data: bytes) -> Record:
        return parse_record(data, project=project, resource=resource)

    record = _load_file(path, project, parse)
    return empty_record(project, resource) if record is None else record


def config_path(store: Store, project: str) -> Path:
    """``queues/<project>/config.json``; links refused first."""
    return store.queues_dir(project) / CONFIG_NAME


def load_config(store: Store, project: str) -> Config:
    """The project's queue config; the defaults if there is none.

    QueueInvalidError if anything but a valid config is at its name.
    """

    def parse(data: bytes) -> Config:
        return parse_config(data, project=project)

    config = _load_file(config_path(store, project), project, parse)
    return Config(project) if config is None else config


def _load_file(path: Path, project: str, parse: Callable[[bytes], _Loaded]) -> _Loaded | None:
    """The file at ``path``, parsed; None if nothing is at its name.

    QueueInvalidError if it is anything but a regular file, oversize or invalid.
    """
    rel = _rel(project, path.name)
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
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
        return parse(data)
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
def _holding(lock: Path) -> Iterator[None]:
    """Hold the exclusive lock at ``lock`` for the body of the ``with``."""
    token = secrets.token_hex(_LOCK_TOKEN_BYTES).encode("ascii")
    acquire_lock(lock, token)
    try:
        yield
    finally:
        release_lock(lock, token)


@contextmanager
def locked(store: Store, project: str, resource: str) -> Iterator[Path]:
    """Hold ``<resource>.lock`` (creating the folders if needed); yield the record path."""
    name = record_name(resource)
    folder = store.make_queues_dir(project)
    with _holding(folder / f"{resource}{LOCK_SUFFIX}"):
        yield folder / name


@contextmanager
def config_locked(store: Store, project: str) -> Iterator[Path]:
    """Hold ``config.json.lock`` (creating the folders if needed); yield the config path."""
    folder = store.make_queues_dir(project)
    with _holding(folder / CONFIG_LOCK_NAME):
        yield folder / CONFIG_NAME


def write(store: Store, path: Path, record: Record) -> None:
    """Replace the record at ``path``: a fresh exclusive ``.tmp`` file, then one replace.

    Called only while the resource's lock is held.
    """
    _replace(store, path, render_record(record), f"the {record.resource} queue", record.project)


def write_config(store: Store, path: Path, config: Config) -> None:
    """Replace the config at ``path`` as a record is replaced; only under the config lock."""
    _replace(store, path, render_config(config), "the queue config", config.project)


def _replace(store: Store, path: Path, data: bytes, what: str, project: str) -> None:
    tmp = store._fresh_temp(path, data)
    if tmp is None:
        raise StoreError(
            PUBLISH_FAILED,
            f"cannot write {what}: {protocol.PUBLISH_ATTEMPTS} temporary "
            f"names in {QUEUES_DIR}/{project} were all taken",
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

    A lane already holding or waiting changes nothing (decision D22). A turn taken now
    lasts ``minutes``, else the resource's default from the config (plan P22).
    """
    config = load_config(store, project)
    with locked(store, project, resource) as path:
        record = load(store, project, resource)
        if record.holds(lane) or record.position(lane) is not None:
            return Change(record, False)
        if record.holder is None:
            default = config.turn_minutes(resource)
            record = grant(record, lane, lane, now, minutes, reason, default_minutes=default)
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


def _not_the_holder(record: Record, lane: str) -> QueueRefusal:
    holder = record.holder
    held = "is free" if holder is None else f"is held by lane {holder.lane}"
    return QueueRefusal(
        NOT_HOLDER,
        f"lane {lane} does not hold {record.resource} ({record.resource} {held}); nothing "
        "written. A turn already ended this way is re-told by release without --holder",
    )


def require_coordinator(config: Config, lane: str, action: str) -> None:
    """Refuse (``not-coordinator``) unless ``lane`` is the project's coordinator (P21)."""
    coordinator = config.effective_coordinator
    if lane != coordinator:
        raise QueueRefusal(
            NOT_COORDINATOR,
            f"only the coordinator of project {config.project} (lane {coordinator}) may "
            f"{action}, and lane {lane} is not it; nothing written",
        )


def release(
    store: Store,
    project: str,
    resource: str,
    lane: str,
    now: datetime,
    *,
    holder: str | None = None,
) -> Change:
    """End ``lane``'s turn and promote the first waiter (granted by ``lane``).

    A lane not in the queue changes nothing; a waiter is refused (``not-holder``).
    Waiters at the front whose card is ``done`` are dropped in the same write (D24).
    The returned record names the new holder and its ``grant_seq``, which ``cli``
    uses, after the lock is released, to post and ring the reservation note. For a
    lane not in the queue the record read under the lock is returned, so ``cli`` can
    tell whether that lane granted the current turn (the D22 retry). A waiter that
    asked for no minutes gets the resource's default from the config (plan P22).

    With ``holder``, ``lane`` must be the coordinator (else ``not-coordinator``) and
    ends ``holder``'s turn instead, exactly as ``holder``'s own release would, but
    granted by ``lane`` (plan P26). ``holder`` must hold the resource (else
    ``not-holder``). Either refusal writes nothing.
    """
    config = load_config(store, project)
    if holder is not None:
        require_coordinator(config, lane, f"end lane {holder}'s turn on {resource}")
    ending = lane if holder is None else holder
    path = record_path(store, project, resource)
    if not _exists(path):
        if holder is not None:
            raise _not_the_holder(empty_record(project, resource), holder)
        return Change(empty_record(project, resource), False)  # nobody holds it
    with locked(store, project, resource) as path:
        record = load(store, project, resource)
        if not record.holds(ending):
            if holder is not None:
                raise _not_the_holder(record, holder)
            _refuse_waiter_release(record, lane)
            return Change(record, False)
        check = CardCheck(store, project, [])
        default = config.turn_minutes(resource)
        promotion = promote_next(record, lane, now, check, default_minutes=default)
        write(store, path, promotion.record)
        return Change(promotion.record, True, promotion.skipped, tuple(check.invalid))


def _not_waiting(record: Record, lane: str, flag: str) -> QueueRefusal:
    if record.holds(lane) and flag == "--after":
        return QueueRefusal(
            NOT_WAITING,
            f"--after {lane}: lane {lane} holds {record.resource} and is not waiting; use "
            "--to 1 for the front of the line; nothing written",
        )
    if record.holds(lane):
        return QueueRefusal(
            HOLDER_CANNOT_MOVE,
            f"{flag} {lane}: lane {lane} holds {record.resource}, and a move reorders waiters "
            "only, never the holder; nothing written",
        )
    return QueueRefusal(
        NOT_WAITING,
        f"{flag} {lane}: lane {lane} is not waiting for {record.resource}; nothing written",
    )


def move(
    store: Store,
    project: str,
    resource: str,
    lane: str,
    waiter: str,
    *,
    to: int | None = None,
    after: str | None = None,
) -> Change:
    """Move ``waiter`` to place ``to`` (from 1) among the waiters, or just behind ``after``.

    Only the coordinator ``lane`` may (``not-coordinator``). It reorders waiters only:
    the holder is never moved or displaced (``holder-cannot-move``), a lane not in the
    line is refused (``not-waiting``), and so is a place past the last waiter
    (``position-out-of-range``); each refusal writes nothing. A move that leaves the
    order as it was writes nothing. No one is told or rung (plan D27).
    """
    if (to is None) == (after is None):
        raise ValueError("move takes exactly one of to and after")
    if after == waiter:
        raise ValueError("a waiter cannot move behind itself")
    config = load_config(store, project)
    require_coordinator(config, lane, f"reorder the {resource} queue")
    path = record_path(store, project, resource)
    if not _exists(path):
        raise _not_waiting(empty_record(project, resource), waiter, "--waiter")
    with locked(store, project, resource) as path:
        record = load(store, project, resource)
        position = record.position(waiter)
        if position is None:
            raise _not_waiting(record, waiter, "--waiter")
        moving = record.waiters[position - 1]
        rest = [w for w in record.waiters if w.lane != waiter]
        if to is not None:
            count = len(record.waiters)
            if not 1 <= to <= count:
                raise QueueRefusal(
                    POSITION_OUT_OF_RANGE,
                    f"--to {to}: the {resource} queue has {count} waiter"
                    f"{'' if count == 1 else 's'}, so a place is 1 to {count}; nothing written",
                )
            index = to - 1
        else:
            anchor = next((n for n, w in enumerate(rest) if w.lane == after), None)
            if anchor is None:
                raise _not_waiting(record, str(after), "--after")
            index = anchor + 1
        rest.insert(index, moving)
        if tuple(rest) == record.waiters:
            return Change(record, False)
        record = replace(record, waiters=tuple(rest))
        write(store, path, record)
        return Change(record, True)


@dataclass(frozen=True)
class ConfigChange:
    """``configure``'s result: the config afterwards, and whether anything was written."""

    config: Config
    changed: bool


def configure(
    store: Store,
    project: str,
    lane: str,
    *,
    coordinator: str | None = None,
    resource: str | None = None,
    minutes: int | None = None,
    stall_minutes: int | None = None,
) -> ConfigChange:
    """Set the coordinator, a resource's default turn minutes, or the stall minutes.

    Only the current coordinator ``lane`` may (``not-coordinator``, nothing written);
    while the config names none, that is ``main`` (plan P21). Values left None keep
    what the config says. A change that leaves the config as it was writes nothing.
    """
    if (resource is None) != (minutes is None):
        raise ValueError("resource and minutes go together")
    action = "change the queue config"
    require_coordinator(load_config(store, project), lane, action)
    with config_locked(store, project) as path:
        current = load_config(store, project)  # again, under the lock
        require_coordinator(current, lane, action)
        new = current
        if coordinator is not None:
            new = replace(new, coordinator=coordinator)
        if stall_minutes is not None:
            new = replace(new, stall_minutes=stall_minutes)
        if resource is not None and minutes is not None:
            table = dict(new.resource_minutes)
            table[resource] = minutes
            new = replace(new, resource_minutes=tuple(sorted(table.items())))
        if new == current:
            return ConfigChange(current, False)
        write_config(store, path, new)
        return ConfigChange(new, True)


def cancel(store: Store, project: str, resource: str, lane: str) -> Change:
    """Take ``lane`` out of the line. The holder is refused (``holder-cannot-cancel``)."""
    load_config(store, project)  # a damaged config stops every changing verb (D32)
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


def _is_config_name(name: str) -> bool:
    """``config.json``, its lock or breaker, or one of its ``.tmp`` files."""
    return name == CONFIG_NAME or name.startswith(f"{CONFIG_NAME}.")


def _belongs(name: str, resource: str | None) -> bool:
    """Whether ``queue show`` for ``resource`` looks at ``name``. The config applies to
    every resource, so its files are always looked at."""
    if resource is None or _is_config_name(name):
        return True
    record = f"{resource}{QUEUE_SUFFIX}"
    transient = tuple(
        f"{resource}{infix}{suffix}"
        for infix in ("", STALL_INFIX)
        for suffix in (LOCK_SUFFIX, BREAK_SUFFIX)
    )
    return name == record or name in transient or name.startswith(f"{record}.")


def show(
    store: Store, project: str, resource: str | None = None, *, max_entries: int | None = None
) -> QueueView:
    """Every valid record in the project (or only ``resource``'s), sorted, with diagnostics.

    Read-only. A damaged record and a leftover ``.tmp`` file are reported, never acted
    on; the ``.tmp`` is never read. With ``resource`` and no record, the resource is
    shown free. An optional entry cap reports incomplete evidence (status uses it).
    """
    protocol.check_project_id(project, "--project")
    if resource is not None:
        protocol.check_resource_id(resource, "RESOURCE")
    folder = store.queues_dir(project)
    rel_dir = f"{QUEUES_DIR}/{project}"
    diagnostics: list[Diagnostic] = []
    records: list[Record] = []
    present: set[str] = set()
    for entry in store._list(folder, rel_dir, diagnostics, max_entries=max_entries):
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
        if name == CONFIG_NAME:

            def parse_cfg(data: bytes) -> Config:
                return parse_config(data, project=project)

            # A valid config prints nothing here (``queue config`` prints it); a
            # damaged one is reported, as a damaged record is.
            store._read_entry(entry, rel, QUEUE_MAX_BYTES, parse_cfg, diagnostics)
            continue
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
    """``<resource>.lock`` or ``<resource>.break``, their ``.stall`` forms, or the
    config's lock or breaker."""
    for suffix in (LOCK_SUFFIX, BREAK_SUFFIX):
        if name.endswith(suffix):
            stem = name.removesuffix(suffix)
            return stem == CONFIG_NAME or _is_resource(stem.removesuffix(STALL_INFIX))
    return False


def _is_regular(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False


# --- Checking (plan P25, D26, D28, D36) -----------------------------------------------

# The first column of each ``queue check`` finding row (decision D28).
PAST_DEADLINE: Final = "past-deadline"
STALLED: Final = "stalled"
MISMATCH: Final = "mismatch"
# ``--lock-holder none``: the project's lock script says nobody holds its lock.
LOCK_HOLDER_NONE: Final = "none"
# The stall alarm (D26): one note to the holder and one to the coordinator per stall.
STALL_KEY_PREFIX: Final = "stall"
STALL_COORD_KEY_PREFIX: Final = "stall-coord"
STALL_NOTE_KIND: Final = TURN_NOTE_KIND
STALL_COORD_NOTE_KIND: Final = "blocker"
STALL_NOTE_NEEDS: Final = TURN_NOTE_NEEDS
# ``<resource>.stall.lock`` (and its ``.stall.break``) serializes posting a stall's notes.
STALL_INFIX: Final = ".stall"


def stall_key(resource: str, grant_seq: int) -> str:
    """``stall-<resource>-<grant_seq>``: the key of the holder's stall note for that turn."""
    return f"{STALL_KEY_PREFIX}-{resource}-{grant_seq}"


def stall_coord_key(resource: str, grant_seq: int) -> str:
    """``stall-coord-<resource>-<grant_seq>``: the key of the coordinator's stall note."""
    return f"{STALL_COORD_KEY_PREFIX}-{resource}-{grant_seq}"


def stall_keys(record: Record) -> tuple[str, str]:
    """Both stall-note keys of ``record``'s current turn (the holder's, the coordinator's)."""
    return (
        stall_key(record.resource, record.grant_seq),
        stall_coord_key(record.resource, record.grant_seq),
    )


def whole_minutes(span: timedelta) -> int:
    """``span`` in whole minutes, rounded down and never negative."""
    return max(int(span.total_seconds() // 60), 0)


def keyed(notes: Iterable[Note], key: str, resource: str) -> list[Note]:
    """The notes about ``resource`` carrying idempotency ``key``, from **every** sender,
    oldest first.

    Not ``Store.find_idempotent``, which matches one sender only: a stall rung by one
    lane must be found by a check run from any other lane (decision D26). The note's
    ``work`` must be ``resource`` too: resource ids may hold hyphens, so
    ``stall-coord-engine-2`` is both the coordinator's key on ``engine`` and the
    holder's key on ``coord-engine``.
    """
    return sorted(
        (n for n in notes if n.idempotency_key == key and n.work == resource),
        key=lambda n: n.id,
    )


def minutes_past_deadline(record: Record, now: datetime) -> int | None:
    """Whole minutes the holder is past its deadline at ``now``; None if not past it."""
    holder = record.holder
    if holder is None:
        return None
    over = now - protocol.parse_utc(holder.deadline_utc, "holder.deadline_utc")
    return whole_minutes(over) if over > timedelta(0) else None


def stalled_minutes(
    record: Record,
    now: datetime,
    stall_minutes: int,
    notes: Sequence[Note],
    acked: Container[tuple[str, str]],
) -> int | None:
    """Whole minutes since the stalled holder's grant; None if the holder is not stalled.

    Stalled (plan P25): the turn came by a release (``granted_by`` is another lane), at
    least ``stall_minutes`` have passed since the grant, and the holder has acknowledged
    neither that turn's reservation note (``turn_key``) nor its stall note
    (``stall_key``). A turn taken on an empty queue is never stalled. A missing
    reservation note (a crash before the post) counts as unacknowledged.
    """
    holder = record.holder
    if holder is None or holder.granted_by == holder.lane:
        return None
    elapsed = now - protocol.parse_utc(holder.granted_utc, "holder.granted_utc")
    if elapsed.total_seconds() < stall_minutes * 60:
        return None
    for key in (turn_key(record.resource, record.grant_seq), stall_keys(record)[0]):
        for note in keyed(notes, key, record.resource):
            if note.to == holder.lane and (note.id, holder.lane) in acked:
                return None
    return whole_minutes(elapsed)


@dataclass(frozen=True)
class Finding:
    """One ``queue check`` row: its kind, the record, and its value.

    ``minutes`` is the minutes over the deadline (past-deadline) or since the grant
    (stalled). ``lock_holder`` is the ``--lock-holder`` value a mismatch compares with.
    """

    kind: str
    record: Record
    minutes: int = 0
    lock_holder: str = LOCK_HOLDER_NONE


@dataclass(frozen=True)
class CheckReport:
    """``check``'s result.

    ``config`` is None when the config is damaged (its diagnostic is in
    ``diagnostics``); the stall time is then the default and nothing may be rung.
    """

    findings: tuple[Finding, ...]
    diagnostics: tuple[Diagnostic, ...]
    config: Config | None
    stall_minutes: int

    def count(self, kind: str) -> int:
        return sum(1 for finding in self.findings if finding.kind == kind)


def check(
    store: Store,
    project: str,
    resource: str | None,
    now: datetime,
    *,
    lock_holder: str | None = None,
) -> CheckReport:
    """Report past-deadline, stalled and (with ``lock_holder``) mismatched turns.

    Read-only: it reads queue records, the config, notes and acks, and writes nothing.
    A holder past its deadline is reported, never evicted. ``lock_holder`` is what the
    project's own lock script says holds the lock of ``resource`` (a lane, or
    LOCK_HOLDER_NONE); it is only compared, never read from a lock file and never
    fixed. Without it no mismatch is reported.
    """
    if lock_holder is not None and resource is None:
        raise ValueError("a lock holder names one resource's lock")
    view = show(store, project, resource)
    try:
        config: Config | None = load_config(store, project)
    except QueueInvalidError:
        config = None  # show() has already reported it as a diagnostic
    stall = (config or Config(project)).effective_stall_minutes
    notes, _ = store.scan_notes(project)
    acked, _ = store.scan_acks(project)
    findings: list[Finding] = []
    for record in view.records:
        over = minutes_past_deadline(record, now)
        if over is not None:
            findings.append(Finding(PAST_DEADLINE, record, over))
        idle = stalled_minutes(record, now, stall, notes, acked)
        if idle is not None:
            findings.append(Finding(STALLED, record, idle))
        if lock_holder is not None:
            queued = None if record.holder is None else record.holder.lane
            locked_by = None if lock_holder == LOCK_HOLDER_NONE else lock_holder
            if queued != locked_by:
                findings.append(Finding(MISMATCH, record, lock_holder=lock_holder))
    return CheckReport(tuple(findings), view.diagnostics, config, stall)


def _release_cmd(record: Record, lane: str, holder: str | None = None) -> str:
    cmd = f"passing-notes release {record.resource} --project {record.project} --lane {lane}"
    return cmd if holder is None else f"{cmd} --holder {holder}"


def stall_draft(record: Record, sender: str) -> Draft:
    """The stall note from ``sender`` to the holder of ``record`` (D26)."""
    holder = _holder_of(record)
    return protocol.make_draft(
        project=record.project,
        from_lane=sender,
        to=holder.lane,
        subject=f"Your turn on {record.resource} looks stalled",
        kind=STALL_NOTE_KIND,
        needs=STALL_NOTE_NEEDS,
        work=record.resource,
        idempotency_key=stall_key(record.resource, record.grant_seq),
    )


def stall_body(record: Record, sender: str, minutes: int, stall_minutes: int) -> str:
    """The holder's stall note body: fixed text over ids and times, no caller content."""
    holder = _holder_of(record)
    resource, project = record.resource, record.project
    found = (
        f"Lane {sender} ran passing-notes queue check: your turn {record.grant_seq} on "
        f"{resource} in project {project}, granted {holder.granted_utc} by lane "
        f"{holder.granted_by}, has had no acknowledgement of its reservation note for "
        f"{minutes} minutes (the stall time is {stall_minutes} minutes)."
    )
    meaning = (
        "The queue orders turns only: take the project's own lock by its own rules. "
        "This note grants nothing else and assigns no work."
    )
    key = turn_key(resource, record.grant_seq)
    answer = (
        f"Read and acknowledge your reservation note (key {key}), then answer it as it "
        f'says: "{TAKEN} {resource}" when you take the turn, or "{PASSING} {resource}" '
        f"when you pass it, and then run {_release_cmd(record, holder.lane)}. "
        "Acknowledging this note also clears the stall."
    )
    return f"{found}\n{meaning}\n\n{answer}"


def stall_coord_draft(record: Record, sender: str, coordinator: str) -> Draft:
    """The stall note from ``sender`` to the coordinator about ``record``'s holder (D26)."""
    holder = _holder_of(record)
    return protocol.make_draft(
        project=record.project,
        from_lane=sender,
        to=coordinator,
        subject=f"Stalled turn on {record.resource}: lane {holder.lane}",
        kind=STALL_COORD_NOTE_KIND,
        needs=STALL_NOTE_NEEDS,
        work=record.resource,
        idempotency_key=stall_coord_key(record.resource, record.grant_seq),
    )


def stall_coord_body(
    record: Record, sender: str, coordinator: str, minutes: int, stall_minutes: int
) -> str:
    """The coordinator's stall note body: fixed text over ids and times, no caller content."""
    holder = _holder_of(record)
    found = (
        f"Lane {sender} ran passing-notes queue check: lane {holder.lane} has held "
        f"{record.resource} in project {record.project} since {holder.granted_utc} (turn "
        f"{record.grant_seq}, granted by lane {holder.granted_by}) and has not acknowledged "
        f"its reservation note for {minutes} minutes (the stall time is {stall_minutes} "
        "minutes)."
    )
    waiting = f"Waiters behind it: {len(record.waiters)}."
    action = (
        "As the coordinator you may end that turn and promote and ring the next waiter "
        f"with {_release_cmd(record, coordinator, holder.lane)}. Nothing has been changed: "
        "the check reports a stalled or overrun turn and never ends it."
    )
    return f"{found}\n{waiting}\n\n{action}"


# --- Posting a stall's notes (plan D26, D36) -------------------------------------------


def stall_lock_name(resource: str) -> str:
    """``<resource>.stall.lock``: held while a stall's notes are checked for and posted."""
    return f"{resource}{STALL_INFIX}{LOCK_SUFFIX}"


@contextmanager
def stall_locked(store: Store, project: str, resource: str) -> Iterator[None]:
    """Hold ``<resource>.stall.lock`` (creating the folders if needed).

    The record lock's rule under its own name, so a stall note is never posted while
    the record's lock is held (P24) and a release never waits for a check.
    """
    folder = store.make_queues_dir(project)
    with _holding(folder / stall_lock_name(resource)):
        yield


def stall_notes(notes: Iterable[Note], record: Record) -> list[Note]:
    """The stall notes of ``record``'s current turn already in the store, from any
    sender, oldest first (either key, ``work`` equal to the resource)."""
    found = (n for key in stall_keys(record) for n in keyed(notes, key, record.resource))
    return sorted(found, key=lambda n: n.id)


def stall_told(found: Iterable[Note], project: str) -> set[str]:
    """The lanes a turn's stall notes have told: each note's recipient and its sender.

    A lane that sent a stall note ran the check itself, so it knows of the stall and
    gets no note of its own (D36); no later run sends it one either.
    """
    told: set[str] = set()
    for note in found:
        told.add(note.to)
        if note.sender == (project, note.from_lane):
            told.add(note.from_lane)
    return told


@dataclass(frozen=True)
class StallPost:
    """What ``post_stall`` found and did for one stalled turn.

    ``stalled`` is False when, checked again under the stall lock, the turn is no
    longer stalled (released, or acknowledged, meanwhile); nothing is posted then.
    ``found`` are the turn's stall notes already in the store; ``posted`` the notes
    this call stored. ``alarms`` holds, for each recipient, the one stall note that
    tells it, found or just posted, the holder's first: the caller reports each one
    whose doorbell is recorded (``wake.rung``) and rings each other one, except a note
    to the sender, which ran the check itself and is never rung (D36). Posting
    is decided here from the note store; ringing is decided at ring time from the
    wake ring records, so a note stored but never rung (a failed ring, or a crash
    before it) is rung by the next run.
    """

    stalled: bool
    found: tuple[Note, ...] = ()
    posted: tuple[Note, ...] = ()
    alarms: tuple[Note, ...] = ()


def post_stall(
    store: Store,
    record: Record,
    sender: str,
    coordinator: str,
    now: datetime,
    stall_minutes: int,
) -> StallPost:
    """Post ``record``'s stall notes from ``sender``: one per recipient per stall.

    Holding ``<resource>.stall.lock``: read the record again and give up if its turn
    changed; read notes and acks again and give up if the turn is no longer stalled.
    The recipients are the holder and the coordinator (one lane when they are the
    same). A recipient with a stall note already in the store gets no second one, and
    that note is its alarm. ``sender`` itself (D36), and a recipient that sent a stall
    note (it ran the check itself, ``stall_told``), get no note posted. Every other
    recipient gets its one note, posted now, as its alarm. So two runs at once post
    each note once between them, and a run after a crash between the two posts posts
    only the missing one. Nothing is rung here: the caller rings ``alarms`` after the
    stall lock is released, never under it (P24).
    """
    project, resource = record.project, record.resource
    with stall_locked(store, project, resource):
        try:
            current = load(store, project, resource)
        except QueueInvalidError:
            return StallPost(stalled=False)
        if current.grant_seq != record.grant_seq or current.holder != record.holder:
            return StallPost(stalled=False)
        notes, _ = store.scan_notes(project)
        acked, _ = store.scan_acks(project)
        minutes = stalled_minutes(current, now, stall_minutes, notes, acked)
        if minutes is None:
            return StallPost(stalled=False)
        found = stall_notes(notes, current)
        told = stall_told(found, project)
        holder = _holder_of(current).lane
        drafts: list[tuple[str, Draft, str]] = [
            (
                holder,
                stall_draft(current, sender),
                stall_body(current, sender, minutes, stall_minutes),
            )
        ]
        if coordinator != holder:
            drafts.append(
                (
                    coordinator,
                    stall_coord_draft(current, sender, coordinator),
                    stall_coord_body(current, sender, coordinator, minutes, stall_minutes),
                )
            )
        posted: list[Note] = []
        alarms: list[Note] = []
        for lane, draft, body in drafts:
            stored = [note for note in found if note.to == lane]
            if stored:
                alarms.append(stored[0])  # oldest first, so every run picks the same one
            elif lane != sender and lane not in told:
                note = store.publish_note(draft, body)
                posted.append(note)
                alarms.append(note)
    return StallPost(True, found=tuple(found), posted=tuple(posted), alarms=tuple(alarms))
