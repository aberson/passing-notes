"""Resource queues (plan Steps 5 and 6): reserve, release, reserve --cancel, queue show,
and the reservation note and doorbell a release sends the new holder.

Every test drives the production ``cli.main`` (the ``invoke`` fixture) or the console
script (``run_cli``) over the autouse temporary store. Turn times come from a fixed
clock: ``cli._now_utc`` is replaced, as it is for the inbox views. The Claude host is
faked (``FakeHost``, as in tests/test_wake.py), so a doorbell is a recorded call.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import (
    CYRILLIC_IE,
    ESC,
    LINK_KINDS,
    PROJECT,
    TAB,
    ZWSP,
    Invoke,
    MakeLink,
    Result,
    child_env,
    run_cli,
    run_together,
    snapshot,
)

from passing_notes import cli, protocol, queue, store, wake

LEFTOVER = "leftover .tmp file; never read as a queue record"
T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
ENGINE = "engine"


class Clock:
    """A clock the tests move by hand; installed as ``cli._now_utc``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = T0
        monkeypatch.setattr(cli, "_now_utc", lambda: self.now)

    def at(self, minutes: int) -> Clock:
        self.now = T0 + timedelta(minutes=minutes)
        return self


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


# One live, idle Claude session per lane, each with its own window name.
LANE_SESSIONS = {
    "lane-a": "aaaaaaaa-2222-4333-8444-555555555555",
    "lane-b": "bbbbbbbb-2222-4333-8444-555555555555",
    "lane-c": "cccccccc-2222-4333-8444-555555555555",
    "lane-d": "dddddddd-2222-4333-8444-555555555555",
}


def window_name(lane: str) -> str:
    return f"example - {lane}"


@dataclass
class FakeHost:
    """The Claude host, faked as in tests/test_wake.py: every doorbell is recorded."""

    sessions: list[wake.Session] = field(default_factory=list)
    sent: list[tuple[str, str, str]] = field(default_factory=list)

    def list_sessions(self) -> list[wake.Session]:
        return list(self.sessions)

    def send(self, address: str, text: str, permission_mode: str) -> wake.SendResult:
        self.sent.append((address, text, permission_mode))
        return wake.SendResult(True, f"msg-{len(self.sent)}", "delivered")

    def rung(self) -> list[str]:
        """The lane each doorbell went to, in order."""
        lanes = {window_name(lane): lane for lane in LANE_SESSIONS}
        return [lanes[address] for address, _, _ in self.sent]


class NoCodex:
    def queue(self, thread_id: str, message: str) -> str:
        raise AssertionError("no lane here is bound to Codex")


@pytest.fixture(autouse=True)
def host(monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    """Every test: a fake host, so no release ever reaches a real Claude or Codex."""
    fake = FakeHost(
        sessions=[
            wake.Session(sid, window_name(lane), "idle", "interactive", 1234)
            for lane, sid in LANE_SESSIONS.items()
        ]
    )
    monkeypatch.setattr(cli, "make_wake_host", lambda: fake)
    monkeypatch.setattr(cli, "make_codex_host", NoCodex)
    return fake


def bind(invoke: Invoke, *lanes: str) -> None:
    for lane in lanes:
        result = invoke(
            "wake",
            "bind",
            "--project",
            PROJECT,
            "--lane",
            lane,
            "--host",
            "claude",
            "--session",
            LANE_SESSIONS[lane],
            "--permission-mode",
            "dontAsk",
        )
        assert result.returncode == 0, result.stderr


def utc(minutes: int) -> str:
    return protocol.format_utc(T0 + timedelta(minutes=minutes))


def holder_line(lane: str, granted: int, deadline: int, resource: str = ENGINE) -> str:
    return TAB.join(["holder", resource, lane, utc(granted), utc(deadline)])


def free_line(resource: str = ENGINE) -> str:
    return TAB.join(["holder", resource, "-"])


def waiter_line(n: int, lane: str, since: int, resource: str = ENGINE) -> str:
    return TAB.join(["waiter", resource, str(n), lane, utc(since)])


def reserve(invoke: Invoke, lane: str, *extra: str, resource: str = ENGINE) -> list[str]:
    result = invoke("reserve", resource, "--project", PROJECT, "--lane", lane, *extra)
    assert result.returncode == 0, result.stderr
    return result.lines


def queue_rows(result: Result) -> list[str]:
    """A release's queue lines: everything before the reservation note's ``note:`` line."""
    rows: list[str] = []
    for line in result.lines:
        if line.startswith("note: "):
            break
        rows.append(line)
    return rows


def queue_folder(root: Path) -> Path:
    return root / "queues" / PROJECT


def record_file(root: Path, resource: str = ENGINE) -> Path:
    return queue_folder(root) / f"{resource}.queue.json"


def record_json(root: Path, resource: str = ENGINE) -> dict[str, object]:
    data = json.loads(record_file(root, resource).read_bytes().decode("utf-8"))
    assert isinstance(data, dict)
    return data


def parsed(root: Path, resource: str = ENGINE) -> queue.Record:
    data = record_file(root, resource).read_bytes()
    return queue.parse_record(data, project=PROJECT, resource=resource)


def three_in_line(invoke: Invoke, clock: Clock) -> None:
    """lane-a holds engine (from minute 0); lane-b (minute 1, 30-minute turn) and
    lane-c (minute 2) wait."""
    reserve(invoke, "lane-a")
    clock.at(1)
    reserve(invoke, "lane-b", "--minutes", "30", "--reason", "Step 2 soak")
    clock.at(2)
    reserve(invoke, "lane-c")


# --- Reserve and show ------------------------------------------------------------------------


def test_reserve_on_an_empty_queue_takes_the_turn_for_the_default_60_minutes(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    result = invoke("reserve", ENGINE, "--project", PROJECT, "--lane", "lane-a")
    assert result.returncode == 0, result.stderr
    assert result.lines == [holder_line("lane-a", 0, protocol.TURN_DEFAULT_MINUTES)]
    assert result.stderr == ""
    assert record_json(store_root) == {
        "resource": ENGINE,
        "project": PROJECT,
        "grant_seq": 1,
        "holder": {
            "lane": "lane-a",
            "granted_utc": utc(0),
            "deadline_utc": utc(60),
            "granted_by": "lane-a",  # a turn taken on an empty queue
            "reason": None,
        },
        "waiters": [],
    }


def test_minutes_overrides_the_default_turn_length(invoke: Invoke, clock: Clock) -> None:
    assert reserve(invoke, "lane-a", "--minutes", "45") == [holder_line("lane-a", 0, 45)]


def test_later_lanes_wait_in_order_and_queue_show_prints_every_row(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    assert reserve(invoke, "lane-a") == [holder_line("lane-a", 0, 60)]
    clock.at(1)
    second = reserve(invoke, "lane-b", "--minutes", "30", "--reason", "Step 2 soak")
    assert second == [holder_line("lane-a", 0, 60), waiter_line(1, "lane-b", 1)]
    clock.at(2)
    third = reserve(invoke, "lane-c")
    assert third == [holder_line("lane-a", 0, 60), waiter_line(2, "lane-c", 2)]
    rows = [holder_line("lane-a", 0, 60), waiter_line(1, "lane-b", 1), waiter_line(2, "lane-c", 2)]
    shown = invoke("queue", "show", "--project", PROJECT)
    assert shown.returncode == 0, shown.stderr
    assert shown.lines == [*rows, "diagnostics: 0"]
    one = invoke("queue", "show", ENGINE, "--project", PROJECT)
    assert one.lines == [*rows, "diagnostics: 0"]
    record = record_json(store_root)
    assert record["waiters"] == [
        {"lane": "lane-b", "since_utc": utc(1), "minutes": 30, "reason": "Step 2 soak"},
        {"lane": "lane-c", "since_utc": utc(2), "minutes": None, "reason": None},
    ]


def test_queue_show_lists_every_resource_sorted_and_shows_a_named_free_one(
    invoke: Invoke, clock: Clock
) -> None:
    reserve(invoke, "lane-a", resource="play-checkout")
    reserve(invoke, "lane-b", resource="engine")
    reserve(invoke, "lane-a", resource="gpu")
    shown = invoke("queue", "show", "--project", PROJECT)
    assert shown.lines == [
        holder_line("lane-b", 0, 60, "engine"),
        holder_line("lane-a", 0, 60, "gpu"),
        holder_line("lane-a", 0, 60, "play-checkout"),
        "diagnostics: 0",
    ]
    never = invoke("queue", "show", "never-used", "--project", PROJECT)
    assert never.returncode == 0
    assert never.lines == [free_line("never-used"), "diagnostics: 0"]
    empty = invoke("queue", "show", "--project", "other-project")
    assert empty.lines == ["diagnostics: 0"]


def test_queue_show_writes_nothing(invoke: Invoke, clock: Clock, store_root: Path) -> None:
    three_in_line(invoke, clock)
    before = snapshot(store_root)
    assert invoke("queue", "show", "--project", PROJECT).returncode == 0
    assert invoke("queue", "show", ENGINE, "--project", PROJECT).returncode == 0
    assert snapshot(store_root) == before
    assert invoke("queue", "show", "--project", "other-project").returncode == 0
    assert not (store_root / "queues" / "other-project").exists()


# --- Release ---------------------------------------------------------------------------------


def test_release_by_the_holder_promotes_the_first_waiter(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    three_in_line(invoke, clock)
    clock.at(10)
    result = invoke("release", ENGINE, "--project", PROJECT, "--lane", "lane-a")
    assert result.returncode == cli.EXIT_WAKE, result.stderr  # lane-b is not bound (D25)
    assert queue_rows(result) == [holder_line("lane-b", 10, 40)]  # lane-b's own 30 minutes
    record = record_json(store_root)
    assert record["grant_seq"] == 2
    assert record["holder"] == {
        "lane": "lane-b",
        "granted_utc": utc(10),
        "deadline_utc": utc(40),
        "granted_by": "lane-a",
        "reason": "Step 2 soak",
    }
    shown = invoke("queue", "show", "--project", PROJECT)
    assert shown.lines == [
        holder_line("lane-b", 10, 40),
        waiter_line(1, "lane-c", 2),
        "diagnostics: 0",
    ]
    clock.at(20)
    second = invoke("release", ENGINE, "--project", PROJECT, "--lane", "lane-b")
    assert queue_rows(second) == [holder_line("lane-c", 20, 80)]  # the default for lane-c
    assert (parsed(store_root).grant_seq, parsed(store_root).holder) == (
        3,
        queue.Holder("lane-c", utc(20), utc(80), "lane-b", None),
    )
    clock.at(30)
    last = invoke("release", ENGINE, "--project", PROJECT, "--lane", "lane-c")
    assert last.returncode == 0
    assert last.lines == [free_line()]
    record = record_json(store_root)
    assert (record["holder"], record["waiters"], record["grant_seq"]) == (None, [], 3)
    clock.at(31)
    assert reserve(invoke, "lane-b") == [holder_line("lane-b", 31, 91)]
    assert parsed(store_root).grant_seq == 4  # every grant has its own number


def test_release_by_a_waiter_is_refused_and_writes_nothing(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    three_in_line(invoke, clock)
    before = snapshot(store_root)
    result = invoke("release", ENGINE, "--project", PROJECT, "--lane", "lane-c")
    assert result.returncode == 2
    assert result.stdout == ""
    assert "error: not-holder:" in result.stderr and "--cancel" in result.stderr
    assert snapshot(store_root) == before


def test_release_by_a_lane_not_in_the_queue_is_a_harmless_no_op(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    nothing = invoke("release", ENGINE, "--project", PROJECT, "--lane", "lane-a")
    assert (nothing.returncode, nothing.stdout) == (0, "nothing to release\n")
    assert not (store_root / "queues").exists()  # no record: nothing written at all
    three_in_line(invoke, clock)
    before = snapshot(store_root)
    absent = invoke("release", ENGINE, "--project", PROJECT, "--lane", "lane-d")
    assert (absent.returncode, absent.stdout, absent.stderr) == (0, "nothing to release\n", "")
    assert snapshot(store_root) == before


def test_repeated_reserve_and_release_are_harmless_no_ops(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    three_in_line(invoke, clock)
    before = snapshot(store_root)
    clock.at(5)
    holder = invoke("reserve", ENGINE, "--project", PROJECT, "--lane", "lane-a", "--minutes", "5")
    assert holder.returncode == 0
    assert holder.lines == [holder_line("lane-a", 0, 60)]
    assert "already holds it on engine; nothing written" in holder.stderr
    waiter = invoke("reserve", ENGINE, "--project", PROJECT, "--lane", "lane-c")
    assert waiter.returncode == 0
    assert waiter.lines == [holder_line("lane-a", 0, 60), waiter_line(2, "lane-c", 2)]
    assert "already is waiting at position 2 on engine; nothing written" in waiter.stderr
    assert snapshot(store_root) == before
    clock.at(10)
    first = invoke("release", ENGINE, "--project", PROJECT, "--lane", "lane-a")
    assert queue_rows(first) == [holder_line("lane-b", 10, 40)]
    after_first = snapshot(store_root)
    again = invoke("release", ENGINE, "--project", PROJECT, "--lane", "lane-a")
    assert (again.returncode, again.lines) == (0, ["nothing to release"])
    assert snapshot(store_root) == after_first


# --- Cancel ----------------------------------------------------------------------------------


def test_cancel_removes_a_waiter_and_prints_the_queue(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    three_in_line(invoke, clock)
    result = invoke("reserve", ENGINE, "--project", PROJECT, "--lane", "lane-b", "--cancel")
    assert result.returncode == 0, result.stderr
    assert result.lines == [holder_line("lane-a", 0, 60), waiter_line(1, "lane-c", 2)]
    record = parsed(store_root)
    assert [w.lane for w in record.waiters] == ["lane-c"]
    assert record.grant_seq == 1  # cancelling grants nothing
    clock.at(10)
    released = invoke("release", ENGINE, "--project", PROJECT, "--lane", "lane-a")
    assert queue_rows(released) == [holder_line("lane-c", 10, 70)]


def test_cancel_by_the_holder_is_refused_and_writes_nothing(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    three_in_line(invoke, clock)
    before = snapshot(store_root)
    result = invoke("reserve", ENGINE, "--project", PROJECT, "--lane", "lane-a", "--cancel")
    assert result.returncode == 2
    assert "error: holder-cannot-cancel:" in result.stderr and "release" in result.stderr
    assert snapshot(store_root) == before


def test_cancel_by_an_absent_lane_is_a_no_op(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    lone = invoke("reserve", ENGINE, "--project", PROJECT, "--lane", "lane-a", "--cancel")
    assert (lone.returncode, lone.lines) == (0, ["nothing to cancel"])
    assert not (store_root / "queues").exists()
    three_in_line(invoke, clock)
    before = snapshot(store_root)
    absent = invoke("reserve", ENGINE, "--project", PROJECT, "--lane", "lane-d", "--cancel")
    assert (absent.returncode, absent.lines) == (0, ["nothing to cancel"])
    assert snapshot(store_root) == before


@pytest.mark.parametrize("extra", [["--minutes", "5"], ["--reason", "why"]])
def test_cancel_takes_no_turn_options(invoke: Invoke, store_root: Path, extra: list[str]) -> None:
    result = invoke("reserve", ENGINE, "--project", PROJECT, "--lane", "lane-a", "--cancel", *extra)
    assert result.returncode == 2
    assert "error: invalid-argument: --minutes and --reason do not apply" in result.stderr
    assert not store_root.exists()


# --- Limits and validation -------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        f"zero{ZWSP}width",
        f"escape{ESC}[31m",
        f"tab{TAB}inside",
        "x" * (protocol.QUEUE_REASON_MAX_CHARS + 1),
        " leading space",
    ],
    ids=["zero-width", "control", "tab", "too-long", "space"],
)
def test_a_bad_reason_exits_2_and_writes_nothing(
    invoke: Invoke, store_root: Path, reason: str
) -> None:
    result = invoke("reserve", ENGINE, "--project", PROJECT, "--lane", "lane-a", "--reason", reason)
    assert result.returncode == 2
    assert "error: invalid-argument: --reason:" in result.stderr
    assert not store_root.exists()


def test_a_reason_of_200_characters_is_stored(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    reason = CYRILLIC_IE + "y" * (protocol.QUEUE_REASON_MAX_CHARS - 1)  # a visible non-ASCII letter
    reserve(invoke, "lane-a", "--reason", reason)
    holder = parsed(store_root).holder
    assert holder is not None and holder.reason == reason


@pytest.mark.parametrize(
    "resource", ["con", "a--b", "lpt1", "Engine", "nul", "x" * 33, "engine.lock", "a/b"]
)
def test_an_invalid_resource_id_exits_2_and_writes_nothing(
    invoke: Invoke, store_root: Path, resource: str
) -> None:
    for args in (
        ["reserve", resource, "--project", PROJECT, "--lane", "lane-a"],
        ["reserve", resource, "--project", PROJECT, "--lane", "lane-a", "--cancel"],
        ["release", resource, "--project", PROJECT, "--lane", "lane-a"],
        ["queue", "show", resource, "--project", PROJECT],
    ):
        result = invoke(*args)
        assert result.returncode == 2, args
        assert "error: invalid-argument: RESOURCE:" in result.stderr
    assert not store_root.exists()


@pytest.mark.parametrize("args", [["--minutes", "0"], ["--minutes", "-5"], ["--minutes", "1.5"]])
def test_minutes_must_be_a_positive_whole_number(
    invoke: Invoke, store_root: Path, args: list[str]
) -> None:
    result = invoke("reserve", ENGINE, "--project", PROJECT, "--lane", "lane-a", *args)
    assert result.returncode == 2
    assert not store_root.exists()


def test_a_huge_minutes_value_caps_the_deadline(invoke: Invoke, clock: Clock) -> None:
    lines = reserve(invoke, "lane-a", "--minutes", "9" * 30)
    assert lines == [TAB.join(["holder", ENGINE, "lane-a", utc(0), "9999-12-31T23:59:59Z"])]


def test_a_queue_holds_at_most_20_waiters(invoke: Invoke, clock: Clock, store_root: Path) -> None:
    reserve(invoke, "lane-0")
    for n in range(1, protocol.QUEUE_MAX_WAITERS + 1):
        reserve(invoke, f"lane-{n}")
    before = snapshot(store_root)
    result = invoke("reserve", ENGINE, "--project", PROJECT, "--lane", "lane-late")
    assert result.returncode == 2
    assert "error: queue-full:" in result.stderr and "20 waiters" in result.stderr
    assert snapshot(store_root) == before
    assert len(parsed(store_root).waiters) == protocol.QUEUE_MAX_WAITERS


def test_a_record_that_would_exceed_8_kib_is_refused(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    """Long non-ASCII reasons can outgrow QUEUE_MAX_BYTES before QUEUE_MAX_WAITERS."""
    reason = chr(0x4E00) * protocol.QUEUE_REASON_MAX_CHARS  # 600 bytes in UTF-8
    reserve(invoke, "lane-0", "--reason", reason)
    refused = None
    for n in range(1, protocol.QUEUE_MAX_WAITERS + 1):
        result = invoke(
            "reserve", ENGINE, "--project", PROJECT, "--lane", f"lane-{n}", "--reason", reason
        )
        if result.returncode != 0:
            refused = result
            break
    assert refused is not None and refused.returncode == 2
    assert "error: queue-full:" in refused.stderr and "8192-byte limit" in refused.stderr
    assert record_file(store_root).stat().st_size <= protocol.QUEUE_MAX_BYTES
    parsed(store_root)  # still valid


# --- Damaged files (D32) ---------------------------------------------------------------------


def _valid_record_bytes() -> bytes:
    record = queue.grant(queue.empty_record(PROJECT, ENGINE), "lane-a", "lane-a", T0, None, None)
    return queue.render_record(record)


DAMAGED: dict[str, bytes] = {
    "invalid-json": b'{"resource": "engine", ',
    "oversize": _valid_record_bytes().rstrip(b"\n") + b" " * protocol.QUEUE_MAX_BYTES + b"\n",
    "wrong-shape": json.dumps({"resource": ENGINE, "project": PROJECT}).encode("utf-8"),
    "other-resource": _valid_record_bytes().replace(b'"engine"', b'"gpu"'),
    "duplicate-key": b'{"resource": "engine", "resource": "engine"}',
    "bom": b"\xef\xbb\xbf" + _valid_record_bytes(),
    # Within 8 KiB, but past Python's digit limit for converting text to an int.
    "huge-number": _valid_record_bytes().replace(b'"grant_seq": 1', b'"grant_seq": ' + b"9" * 5000),
}


def _plant(root: Path, kind: str) -> None:
    folder = queue_folder(root)
    folder.mkdir(parents=True, exist_ok=True)
    if kind == "folder":
        (folder / f"{ENGINE}.queue.json").mkdir()
    else:
        (folder / f"{ENGINE}.queue.json").write_bytes(DAMAGED[kind])


@pytest.mark.parametrize("kind", [*DAMAGED, "folder"])
def test_a_damaged_record_is_never_changed(
    invoke: Invoke, clock: Clock, store_root: Path, kind: str
) -> None:
    _plant(store_root, kind)
    before = snapshot(store_root)
    for args in (
        ["reserve", ENGINE, "--project", PROJECT, "--lane", "lane-b"],
        ["reserve", ENGINE, "--project", PROJECT, "--lane", "lane-b", "--cancel"],
        ["release", ENGINE, "--project", PROJECT, "--lane", "lane-a"],
        ["release", ENGINE, "--project", PROJECT, "--lane", "lane-b"],
    ):
        result = invoke(*args)
        assert result.returncode == 3, (args, result.stderr)
        assert result.stdout == ""
        assert f"error: queue-invalid: queues/{PROJECT}/engine.queue.json" in result.stderr
        assert snapshot(store_root) == before
    assert sorted(p.name for p in queue_folder(store_root).iterdir()) == ["engine.queue.json"]
    if kind == "folder":
        assert record_file(store_root).is_dir()
    for args in (
        ["queue", "show", "--project", PROJECT],
        ["queue", "show", ENGINE, "--project", PROJECT],
    ):
        shown = invoke(*args)
        assert shown.returncode == 0
        assert len(shown.lines) == 2, shown.lines  # no holder row: the record is not understood
        assert shown.lines[0].startswith(f"diagnostic: queues/{PROJECT}/engine.queue.json: ")
        assert shown.lines[1] == "diagnostics: 1"
    assert snapshot(store_root) == before


def test_other_resources_still_work_beside_a_damaged_record(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    _plant(store_root, "invalid-json")
    assert reserve(invoke, "lane-a", resource="gpu") == [holder_line("lane-a", 0, 60, "gpu")]
    shown = invoke("queue", "show", "--project", PROJECT)
    assert len(shown.lines) == 3, shown.lines
    assert shown.lines[0] == holder_line("lane-a", 0, 60, "gpu")
    assert shown.lines[1].startswith(f"diagnostic: queues/{PROJECT}/engine.queue.json: invalid:")
    assert shown.lines[2] == "diagnostics: 1"


def test_a_leftover_tmp_is_reported_and_never_read(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    reserve(invoke, "lane-a")
    leftover = queue_folder(store_root) / "engine.queue.json.0a1b2c3d.tmp"
    leftover.write_bytes(b"not json at all")
    clock.at(1)
    assert reserve(invoke, "lane-b") == [holder_line("lane-a", 0, 60), waiter_line(1, "lane-b", 1)]
    for args in (
        ["queue", "show", "--project", PROJECT],
        ["queue", "show", ENGINE, "--project", PROJECT],
    ):
        shown = invoke(*args)
        assert shown.lines == [
            holder_line("lane-a", 0, 60),
            waiter_line(1, "lane-b", 1),
            f"diagnostic: queues/{PROJECT}/engine.queue.json.0a1b2c3d.tmp: {LEFTOVER}",
            "diagnostics: 1",
        ]
    assert leftover.read_bytes() == b"not json at all"


def test_queue_show_diagnostics_are_bounded(invoke: Invoke, clock: Clock, store_root: Path) -> None:
    reserve(invoke, "lane-a")
    for n in range(25):
        (queue_folder(store_root) / f"engine.queue.json.{n:08x}.tmp").write_bytes(b"x")
    shown = invoke("queue", "show", "--project", PROJECT)
    diagnostics = [line for line in shown.lines if line.startswith("diagnostic: ")]
    assert len(diagnostics) == protocol.DIAGNOSTIC_LINES
    assert shown.lines[-1] == f"diagnostics: 25 ({protocol.DIAGNOSTIC_LINES} shown)"
    assert shown.lines[0] == holder_line("lane-a", 0, 60)


def test_queue_show_reports_names_that_are_not_records(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    reserve(invoke, "lane-a")
    folder = queue_folder(store_root)
    (folder / "notes.txt").write_bytes(b"x")
    (folder / "con.queue.json").write_bytes(b"x")
    (folder / "gpu.lock").write_bytes(b"x")  # a lock is transient: never reported
    (folder / "gpu.break").write_bytes(b"x")  # so is its breaker
    shown = invoke("queue", "show", "--project", PROJECT)
    assert shown.lines == [
        holder_line("lane-a", 0, 60),
        f"diagnostic: queues/{PROJECT}/con.queue.json: ignored: not named <resource>.queue.json",
        f"diagnostic: queues/{PROJECT}/notes.txt: ignored: not named <resource>.queue.json",
        "diagnostics: 2",
    ]


# --- The lock ----------------------------------------------------------------------------------


def test_a_stale_lock_is_broken(invoke: Invoke, clock: Clock, store_root: Path) -> None:
    folder = queue_folder(store_root)
    folder.mkdir(parents=True)
    lock = folder / "engine.lock"
    lock.write_bytes(b"left by a crashed writer")
    old = time.time() - protocol.QUEUE_LOCK_STALE_S - 10
    os.utime(lock, (old, old))
    assert reserve(invoke, "lane-a") == [holder_line("lane-a", 0, 60)]
    assert sorted(p.name for p in folder.iterdir()) == ["engine.queue.json"]


def _stale(path: Path) -> None:
    old = time.time() - protocol.QUEUE_LOCK_STALE_S - 10
    os.utime(path, (old, old))


def test_a_lock_another_waiter_broke_and_retook_is_never_removed(
    store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Waiters A and B both find the same stale lock; B breaks it and takes a fresh
    one before A acts on its stale look. A must not remove B's lock (two writers)."""
    folder = queue_folder(store_root)
    folder.mkdir(parents=True)
    lock = folder / "engine.lock"
    lock.write_bytes(b"left by a crashed writer")
    _stale(lock)
    real_lstat = os.lstat
    b_token = b"b" * 16
    interleaved: list[bool] = []

    def lstat_then_b_runs(path: str | os.PathLike[str]) -> os.stat_result:
        info = real_lstat(path)
        if not interleaved and Path(path) == lock:
            interleaved.append(True)
            queue.acquire_lock(lock, b_token)  # B breaks the stale lock and takes its own
        return info  # A goes on with its stale look

    monkeypatch.setattr(os, "lstat", lstat_then_b_runs)
    monkeypatch.setattr(queue, "LOCK_WAIT_S", 0.2)
    with pytest.raises(store.StoreError) as raised:
        queue.acquire_lock(lock, b"a" * 16)
    assert interleaved == [True]
    assert raised.value.code == queue.QUEUE_BUSY  # A waits for B instead of joining it
    assert lock.read_bytes() == b_token
    assert sorted(p.name for p in folder.iterdir()) == ["engine.lock"]


def test_a_writer_never_removes_a_lock_retaken_after_it_was_broken(
    store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A's lock goes stale; while A releases it, B tries to break and retake it. Either
    B is kept out until A is done, or A leaves B's lock in place."""
    folder = queue_folder(store_root)
    folder.mkdir(parents=True)
    lock = folder / "engine.lock"
    a_token, b_token = b"a" * 16, b"b" * 16
    queue.acquire_lock(lock, a_token)
    _stale(lock)
    real_read = store._read_bounded
    outcome: list[str] = []

    def read_then_b_runs(path: Path, limit: int) -> bytes:
        data = real_read(path, limit)
        if not outcome and Path(path) == lock:
            try:
                queue.acquire_lock(lock, b_token)
                outcome.append("b holds")
            except store.StoreError as exc:
                outcome.append(exc.code)
        return data

    monkeypatch.setattr(queue, "_read_bounded", read_then_b_runs)
    monkeypatch.setattr(queue, "LOCK_WAIT_S", 0.2)
    queue.release_lock(lock, a_token)
    if outcome == ["b holds"]:
        assert lock.read_bytes() == b_token
    else:
        assert outcome == [queue.QUEUE_BUSY] and not lock.exists()
    assert not (folder / "engine.break").exists()


def test_a_breaker_left_by_a_crashed_remover_is_removed_once_stale(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    folder = queue_folder(store_root)
    folder.mkdir(parents=True)
    for name in ("engine.lock", "engine.break"):
        (folder / name).write_bytes(b"left by a crashed writer")
        _stale(folder / name)
    assert reserve(invoke, "lane-a") == [holder_line("lane-a", 0, 60)]
    assert sorted(p.name for p in folder.iterdir()) == ["engine.queue.json"]


def test_a_fresh_lock_is_waited_for_then_reported_busy(
    invoke: Invoke, clock: Clock, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reserve(invoke, "lane-a")
    lock = queue_folder(store_root) / "engine.lock"
    lock.write_bytes(b"another writer")
    before = snapshot(store_root)
    monkeypatch.setattr(queue, "LOCK_WAIT_S", 0.2)
    for args in (
        ["reserve", ENGINE, "--project", PROJECT, "--lane", "lane-b"],
        ["release", ENGINE, "--project", PROJECT, "--lane", "lane-a"],
    ):
        result = invoke(*args)
        assert result.returncode == 4
        assert "error: queue-busy:" in result.stderr
    assert snapshot(store_root) == before  # the other writer's lock is left alone


# A child process that runs the production ``cli.main`` and, before its first attempt
# to take a queue lock, waits until every child has reached that point, so all of them
# contend for the lock at once.
QUEUE_RACER = """
import sys
import time
from pathlib import Path

from passing_notes import cli, queue

barrier, racers, *argv = sys.argv[1:]
take = queue.acquire_lock
arrived = []


def take_together(lock, token):
    if not arrived:
        arrived.append(lock.name)
        Path(barrier, token.decode("ascii")).touch()
        deadline = time.monotonic() + 30
        while len(list(Path(barrier).iterdir())) < int(racers):
            if time.monotonic() > deadline:
                break
            time.sleep(0.01)
    take(lock, token)


queue.acquire_lock = take_together
raise SystemExit(cli.main(argv))
"""


def test_concurrent_reserves_put_each_lane_in_the_queue_exactly_once(store_root: Path) -> None:
    lanes = [f"lane-{n}" for n in range(6)]
    runs = [["reserve", ENGINE, "--project", PROJECT, "--lane", lane] for lane in lanes]
    results = run_together(store_root, QUEUE_RACER, runs, "the queue lock")
    assert [r.returncode for r in results] == [0] * len(lanes), [r.stderr for r in results]
    record = parsed(store_root)  # valid
    assert record.holder is not None and record.grant_seq == 1
    in_queue = [record.holder.lane, *(w.lane for w in record.waiters)]
    assert sorted(in_queue) == sorted(lanes)  # each lane exactly once
    assert sorted(p.name for p in queue_folder(store_root).iterdir()) == ["engine.queue.json"]
    holders = [r for r in results if len(r.lines) == 1]
    assert len(holders) == 1 and holders[0].lines[0].split(TAB)[2] == record.holder.lane
    for result in results:  # every waiter printed its true place
        if len(result.lines) == 2:
            _, _, n, lane, _ = result.lines[1].split(TAB)
            assert record.position(lane) == int(n)


# --- Privacy and store guards ------------------------------------------------------------------


def test_queue_files_live_only_under_the_store_roots_queues_folder(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    three_in_line(invoke, clock)
    invoke("release", ENGINE, "--project", PROJECT, "--lane", "lane-a")
    invoke("reserve", ENGINE, "--project", PROJECT, "--lane", "lane-c", "--cancel")
    reserve(invoke, "lane-a", resource="gpu")
    invoke("queue", "show", "--project", PROJECT)
    files = sorted(snapshot(store_root))
    # The release's one reservation note (Step 6) is an ordinary note in messages/.
    assert [name for name in files if not name.startswith(f"messages/{PROJECT}/")] == [
        f"queues/{PROJECT}/engine.queue.json",
        f"queues/{PROJECT}/gpu.queue.json",
    ]


def test_a_store_inside_a_git_working_tree_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "fake-repo"
    (repo / ".git").mkdir(parents=True)
    root = repo / "notes-store"
    for args in (
        ["reserve", ENGINE, "--project", PROJECT, "--lane", "lane-a"],
        ["reserve", ENGINE, "--project", PROJECT, "--lane", "lane-a", "--cancel"],
        ["release", ENGINE, "--project", PROJECT, "--lane", "lane-a"],
        ["queue", "show", "--project", PROJECT],
    ):
        result = run_cli(args, root=root)
        assert result.returncode == 4, result.stderr
        assert "error: store-in-repo:" in result.stderr
    assert sorted(p.name for p in repo.iterdir()) == [".git"]


@pytest.mark.parametrize("kind", LINK_KINDS)
@pytest.mark.parametrize("level", ["queues", f"queues/{PROJECT}"])
def test_a_linked_queues_folder_is_refused(
    invoke: Invoke, store_root: Path, tmp_path: Path, dir_link: MakeLink, kind: str, level: str
) -> None:
    target = tmp_path / "elsewhere"  # outside any Git tree: only the link check applies
    target.mkdir()
    link = store_root / level
    link.parent.mkdir(parents=True, exist_ok=True)
    dir_link(link, target, kind)
    for args in (
        ["reserve", ENGINE, "--project", PROJECT, "--lane", "lane-a"],
        ["reserve", ENGINE, "--project", PROJECT, "--lane", "lane-a", "--cancel"],
        ["release", ENGINE, "--project", PROJECT, "--lane", "lane-a"],
        ["queue", "show", "--project", PROJECT],
        ["queue", "show", ENGINE, "--project", PROJECT],
    ):
        result = invoke(*args)
        assert result.returncode == 4, (args, result.stderr)
        assert "error: store-link:" in result.stderr
    assert list(target.iterdir()) == []


# --- The console script ------------------------------------------------------------------------


def test_the_console_script_reserves_releases_and_shows(store_root: Path) -> None:
    """The installed entry point, in separate processes, end to end."""
    env = child_env(store_root)
    for lane in ("lane-a", "lane-b"):
        result = run_cli(
            ["reserve", ENGINE, "--project", PROJECT, "--lane", lane], root=store_root, env=env
        )
        assert result.returncode == 0, result.stderr
    shown = run_cli(["queue", "show", "--project", PROJECT], root=store_root, env=env)
    assert shown.returncode == 0, shown.stderr
    holder, waiter, total = shown.lines
    assert holder.split(TAB)[:3] == ["holder", ENGINE, "lane-a"]
    assert waiter.split(TAB)[:4] == ["waiter", ENGINE, "1", "lane-b"]
    assert total == "diagnostics: 0"
    released = run_cli(
        ["release", ENGINE, "--project", PROJECT, "--lane", "lane-a"], root=store_root, env=env
    )
    assert released.returncode == cli.EXIT_WAKE, released.stderr  # lane-b is not bound
    assert released.lines[0].split(TAB)[:3] == ["holder", ENGINE, "lane-b"]
    record = parsed(store_root)
    assert record.holder is not None and record.holder.granted_by == "lane-a"
    assert record.grant_seq == 2
    help_text = run_cli(["reserve", "--help"], root=store_root, env=env)
    assert help_text.returncode == 0 and "--cancel" in help_text.stdout


# --- Existing output is unchanged ----------------------------------------------------------------


def test_the_queue_folder_adds_no_inbox_or_card_diagnostics(invoke: Invoke, clock: Clock) -> None:
    reserve(invoke, "lane-a")
    inbox = invoke("inbox", "--project", PROJECT, "--lane", "lane-a")
    assert inbox.lines == ["diagnostics: 0"]
    cards = invoke("card", "show", "--project", PROJECT)
    assert cards.lines == ["diagnostics: 0"]


# --- Release tells the new holder (Step 6) -----------------------------------------------------


def notes(root: Path) -> list[protocol.Note]:
    found, diagnostics = store.Store.open().scan_notes(PROJECT)
    assert diagnostics == []
    return sorted(found, key=lambda note: note.id)


def release(invoke: Invoke, lane: str) -> Result:
    return invoke("release", ENGINE, "--project", PROJECT, "--lane", lane)


def ring_lines(note: protocol.Note, lane: str, n: int) -> list[str]:
    """``_ring``'s lines for a doorbell delivered to ``lane``'s window as message ``n``."""
    wake_line = (
        f"wake: delivered to session {LANE_SESSIONS[lane]} ({window_name(lane)}) inbox, "
        f"message msg-{n}; delivery is not reading"
    )
    return [
        f"note: {note.id} is in the store",
        wake_line,
        "ack: not observed",
        "reply: not observed",
    ]


def inbox_ids(invoke: Invoke, lane: str) -> list[str]:
    result = invoke("inbox", "--project", PROJECT, "--lane", lane)
    assert result.returncode == 0, result.stderr
    return [line.split(TAB)[0] for line in result.lines if TAB in line]


def test_release_posts_one_reservation_note_and_rings_only_the_new_holder(
    invoke: Invoke, clock: Clock, store_root: Path, host: FakeHost
) -> None:
    bind(invoke, "lane-a", "lane-b", "lane-c")
    three_in_line(invoke, clock)
    assert notes(store_root) == []  # reserving tells no one; a self-taken turn has no note
    clock.at(10)
    first = release(invoke, "lane-a")
    assert first.returncode == 0, first.stderr
    (note,) = notes(store_root)
    assert first.lines == [holder_line("lane-b", 10, 40), *ring_lines(note, "lane-b", 1)]
    assert first.stderr == ""
    grant_seq = parsed(store_root).grant_seq
    assert grant_seq == 2
    assert (note.sender, note.to, note.kind, note.needs) == (
        (PROJECT, "lane-a"),
        "lane-b",
        "reservation",
        "reply",
    )
    assert note.subject == "Your turn on engine"
    assert note.work == ENGINE
    assert note.reply_to == protocol.REPLY_TO_NONE
    assert note.idempotency_key == f"turn-engine-{grant_seq}"
    assert wake.ring_reason(store.Store.open(), PROJECT, note) == "it needs reply"
    # The body is fixed text: the waiter's own --reason is caller content and stays out.
    assert "Step 2 soak" not in note.body
    for text in ("taken engine", "passing engine", "--kind answer", "--needs fyi", utc(40)):
        assert text in note.body, text
    # Exactly one doorbell, to lane-b; lane-c, further back, gets nothing.
    assert host.rung() == ["lane-b"]
    assert host.sent[0][1] == wake.doorbell_text(note.id)
    assert inbox_ids(invoke, "lane-c") == []
    assert inbox_ids(invoke, "lane-b") == [note.id]

    clock.at(20)
    second = release(invoke, "lane-b")
    assert second.returncode == 0, second.stderr
    first_note, third_note = notes(store_root)
    assert first_note == note
    assert second.lines == [holder_line("lane-c", 20, 80), *ring_lines(third_note, "lane-c", 2)]
    assert (third_note.sender, third_note.to) == ((PROJECT, "lane-b"), "lane-c")
    assert third_note.idempotency_key == "turn-engine-3"
    assert host.rung() == ["lane-b", "lane-c"]
    assert inbox_ids(invoke, "lane-c") == [third_note.id]  # exactly one for lane-c

    clock.at(30)
    last = release(invoke, "lane-c")
    assert (last.returncode, last.lines) == (0, [free_line()])  # nobody to tell
    assert len(notes(store_root)) == 2 and len(host.sent) == 2


def test_the_ring_policy_is_unchanged_for_queues() -> None:
    """A reservation note rings because it needs a reply, under the existing rule."""
    assert wake.UNBLOCKING_NEEDS == ("reply", "operator-decision")
    assert queue.TURN_NOTE_NEEDS in wake.UNBLOCKING_NEEDS
    assert queue.TURN_NOTE_KIND in protocol.KINDS


def test_a_retried_release_after_a_crash_before_the_post_posts_the_missing_note(
    invoke: Invoke, clock: Clock, store_root: Path, host: FakeHost
) -> None:
    bind(invoke, "lane-b", "lane-c")
    three_in_line(invoke, clock)
    # The crash: the promotion is written, then the process dies before posting.
    queue.release(store.Store.open(), PROJECT, ENGINE, "lane-a", T0 + timedelta(minutes=10))
    record = parsed(store_root)
    assert record.holder is not None and record.holder.granted_by == "lane-a"
    assert notes(store_root) == [] and host.sent == []
    retry = release(invoke, "lane-a")
    assert retry.returncode == 0, retry.stderr
    (note,) = notes(store_root)
    assert note.idempotency_key == f"turn-engine-{record.grant_seq}"
    assert (note.sender, note.to) == ((PROJECT, "lane-a"), "lane-b")
    assert retry.lines == [holder_line("lane-b", 10, 40), *ring_lines(note, "lane-b", 1)]
    assert "reservation note turn-engine-2 is missing; posting it now" in retry.stderr
    assert host.rung() == ["lane-b"]
    assert parsed(store_root) == record  # the retry changes no queue state
    # Once the note exists, a further retry posts and rings nothing: one note per grant.
    before = snapshot(store_root)
    again = release(invoke, "lane-a")
    assert (again.returncode, again.lines, again.stderr) == (0, ["nothing to release"], "")
    assert snapshot(store_root) == before
    assert len(notes(store_root)) == 1 and host.rung() == ["lane-b"]


def test_a_retry_after_a_complete_release_posts_and_rings_nothing(
    invoke: Invoke, clock: Clock, store_root: Path, host: FakeHost
) -> None:
    bind(invoke, "lane-b")
    three_in_line(invoke, clock)
    clock.at(10)
    assert release(invoke, "lane-a").returncode == 0
    before = snapshot(store_root)
    for lane in ("lane-a", "lane-d"):  # the granting lane, and a lane never in the queue
        again = release(invoke, lane)
        assert (again.returncode, again.lines, again.stderr) == (0, ["nothing to release"], "")
    assert snapshot(store_root) == before
    assert len(notes(store_root)) == 1 and host.rung() == ["lane-b"]


def test_only_the_lane_that_granted_the_turn_reposts_a_missing_note(
    invoke: Invoke, clock: Clock, store_root: Path, host: FakeHost
) -> None:
    bind(invoke, "lane-b")
    three_in_line(invoke, clock)
    queue.release(store.Store.open(), PROJECT, ENGINE, "lane-a", T0)
    before = snapshot(store_root)
    other = release(invoke, "lane-d")  # did not grant lane-b's turn
    assert (other.returncode, other.lines) == (0, ["nothing to release"])
    assert snapshot(store_root) == before and host.sent == []


def card_set(invoke: Invoke, lane: str, state: str) -> None:
    result = invoke(
        "card",
        "set",
        "--project",
        PROJECT,
        "--lane",
        lane,
        "--host",
        "claude",
        "--item",
        "Synthetic card",
        "--state",
        state,
    )
    assert result.returncode == 0, result.stderr


def test_a_waiter_whose_card_is_done_is_skipped_and_never_rung(
    invoke: Invoke, clock: Clock, store_root: Path, host: FakeHost
) -> None:
    bind(invoke, "lane-b", "lane-c")
    three_in_line(invoke, clock)
    card_set(invoke, "lane-b", "done")
    card_set(invoke, "lane-c", "active")
    clock.at(10)
    result = release(invoke, "lane-a")
    assert result.returncode == 0, result.stderr
    (note,) = notes(store_root)
    assert result.lines == [
        holder_line("lane-c", 10, 70),
        "skipped lane-b: card is done",
        *ring_lines(note, "lane-c", 1),
    ]
    assert note.to == "lane-c" and note.idempotency_key == "turn-engine-2"
    record = parsed(store_root)
    assert record.waiters == () and record.grant_seq == 2  # dropped, given no turn number
    assert host.rung() == ["lane-c"]
    assert inbox_ids(invoke, "lane-b") == []


def test_when_every_waiter_is_done_the_resource_becomes_free(
    invoke: Invoke, clock: Clock, store_root: Path, host: FakeHost
) -> None:
    three_in_line(invoke, clock)
    card_set(invoke, "lane-b", "done")
    card_set(invoke, "lane-c", "done")
    result = release(invoke, "lane-a")
    assert result.returncode == 0, result.stderr
    assert result.lines == [
        free_line(),
        "skipped lane-b: card is done",
        "skipped lane-c: card is done",
    ]
    record = parsed(store_root)
    assert (record.holder, record.waiters, record.grant_seq) == (None, (), 1)
    assert notes(store_root) == [] and host.sent == []


def test_a_waiter_with_an_invalid_card_is_promoted_and_reported(
    invoke: Invoke, clock: Clock, store_root: Path, host: FakeHost
) -> None:
    bind(invoke, "lane-b")
    three_in_line(invoke, clock)
    cards = store_root / "cards" / PROJECT
    cards.mkdir(parents=True)
    (cards / "lane-b.md").write_bytes(b"not a card\n")
    clock.at(10)
    result = release(invoke, "lane-a")
    assert result.returncode == 0, result.stderr
    assert queue_rows(result) == [holder_line("lane-b", 10, 40)]
    assert "the card of lane lane-b is invalid, so it was promoted as not done" in result.stderr
    assert host.rung() == ["lane-b"]


def test_a_ring_that_fails_keeps_the_promotion_and_the_note_and_exits_5(
    invoke: Invoke, clock: Clock, store_root: Path, host: FakeHost
) -> None:
    three_in_line(invoke, clock)  # lane-b has no wake binding
    clock.at(10)
    result = release(invoke, "lane-a")
    assert result.returncode == 5
    (note,) = notes(store_root)
    assert result.lines == [
        holder_line("lane-b", 10, 40),
        f"note: {note.id} is in the store",
        "wake: not delivered",
    ]
    assert "error: wake-no-binding:" in result.stderr
    holder = parsed(store_root).holder
    assert holder is not None and holder.lane == "lane-b"
    assert note.to == "lane-b" and host.sent == []
    # A retry rings nothing more: the note already exists, so the release is complete.
    assert release(invoke, "lane-a").lines == ["nothing to release"]
    assert len(notes(store_root)) == 1


def test_the_lane_rules_and_cli_doc_describe_the_queue() -> None:
    """D37: lanes take turns through the queue and answer reservation notes. The lane
    skills' queue lines (D34) are checked in tests/test_lane_open.py."""
    root = Path(__file__).resolve().parents[1]
    rules = " ".join((root / "docs" / "lane-rules.md").read_text(encoding="utf-8").split())
    assert "passing-notes reserve" in rules and "passing-notes release" in rules
    assert '"your turn" notes' in rules
    assert "--kind answer --reply-to <id> --needs fyi" in rules
    assert "`taken <resource>`" in rules and "`passing <resource>`" in rules
    cli_doc = " ".join((root / "docs" / "cli.md").read_text(encoding="utf-8").split())
    assert "skipped <lane>: card is done" in cli_doc and "turn-<resource>-<grant_seq>" in cli_doc


# --- Coordinator config, reordering and release (Step 7) -------------------------------------

COORD = "coord"


def config_file(root: Path) -> Path:
    return queue_folder(root) / "config.json"


def config_rows(coordinator: str, stall: int = 10, **turns: int) -> list[str]:
    rows = [
        TAB.join(["coordinator", coordinator]),
        TAB.join(["stall-minutes", str(stall)]),
        TAB.join(["default-turn-minutes", str(protocol.TURN_DEFAULT_MINUTES)]),
    ]
    rows.extend(TAB.join(["turn-minutes", r, str(m)]) for r, m in sorted(turns.items()))
    return rows


def queue_config(invoke: Invoke, lane: str, *extra: str) -> Result:
    return invoke("queue", "config", "--project", PROJECT, "--lane", lane, *extra)


def move(invoke: Invoke, lane: str, waiter: str, *place: str) -> Result:
    return invoke(
        "queue", "move", ENGINE, "--project", PROJECT, "--lane", lane, "--waiter", waiter, *place
    )


def coordinator_release(invoke: Invoke, lane: str, holder: str) -> Result:
    return invoke("release", ENGINE, "--project", PROJECT, "--lane", lane, "--holder", holder)


def four_in_line(invoke: Invoke, clock: Clock) -> None:
    """three_in_line, then lane-d waits too (minute 3)."""
    three_in_line(invoke, clock)
    clock.at(3)
    reserve(invoke, "lane-d")


def waiters(root: Path) -> list[str]:
    return [w.lane for w in parsed(root).waiters]


def test_with_no_config_main_is_the_coordinator_and_printing_writes_nothing(
    invoke: Invoke, store_root: Path
) -> None:
    for lane in ("main", "lane-a"):  # any lane may print the config
        result = queue_config(invoke, lane)
        assert (result.returncode, result.lines, result.stderr) == (0, config_rows("main"), "")
    assert snapshot(store_root) == {}
    assert queue.load_config(store.Store.open(), PROJECT).effective_coordinator == "main"


def test_main_names_the_coordinator_and_then_only_it_may_change_the_config(
    invoke: Invoke, store_root: Path
) -> None:
    refused = queue_config(invoke, "lane-a", "--coordinator", "lane-a")
    assert refused.returncode == 2 and refused.stdout == ""
    assert "error: not-coordinator: only the coordinator" in refused.stderr
    assert snapshot(store_root) == {}

    named = queue_config(invoke, "main", "--coordinator", COORD)
    assert (named.returncode, named.lines) == (0, config_rows(COORD)), named.stderr
    assert json.loads(config_file(store_root).read_bytes()) == {
        "project": PROJECT,
        "coordinator": COORD,
        "resource_minutes": {},
    }
    before = snapshot(store_root)
    for lane, extra in (
        ("main", ["--coordinator", "main"]),  # main no longer coordinates
        ("lane-a", ["--stall-minutes", "5"]),
        ("main", ["--resource", ENGINE, "--minutes", "30"]),
    ):
        result = queue_config(invoke, lane, *extra)
        assert result.returncode == 2, (lane, extra, result.stderr)
        expected = f"not-coordinator: only the coordinator of project {PROJECT} (lane {COORD})"
        assert expected in result.stderr
        assert result.stdout == ""
        assert snapshot(store_root) == before  # the config bytes unchanged
    assert queue_config(invoke, "main").lines == config_rows(COORD)  # still printable

    changed = queue_config(
        invoke, COORD, "--stall-minutes", "5", "--resource", ENGINE, "--minutes", "30"
    )
    assert changed.returncode == 0, changed.stderr
    assert changed.lines == config_rows(COORD, 5, engine=30)
    config = queue.load_config(store.Store.open(), PROJECT)
    assert (config.coordinator, config.stall_minutes, config.turn_minutes(ENGINE)) == (
        COORD,
        5,
        30,
    )
    assert config.turn_minutes("gpu") == protocol.TURN_DEFAULT_MINUTES
    # The coordinator can hand the role on; then it, too, is refused.
    assert queue_config(invoke, COORD, "--coordinator", "lane-b").returncode == 0
    assert queue_config(invoke, COORD, "--stall-minutes", "7").returncode == 2
    assert queue_config(invoke, "lane-b", "--stall-minutes", "7").lines == config_rows(
        "lane-b", 7, engine=30
    )


def test_a_config_change_that_changes_nothing_writes_nothing(
    invoke: Invoke, store_root: Path
) -> None:
    assert queue_config(invoke, "main", "--resource", ENGINE, "--minutes", "30").returncode == 0
    before = snapshot(store_root)
    again = queue_config(invoke, "main", "--resource", ENGINE, "--minutes", "30")
    assert (again.returncode, again.lines) == (0, config_rows("main", engine=30))
    assert "already says that; nothing written" in again.stderr
    assert snapshot(store_root) == before


@pytest.mark.parametrize(
    "extra",
    [
        ["--resource", ENGINE],
        ["--minutes", "30"],
        ["--resource", "con", "--minutes", "30"],
        ["--resource", ENGINE, "--minutes", "0"],
        ["--stall-minutes", "0"],
        ["--coordinator", "all"],
        ["--coordinator", "a--b"],
    ],
)
def test_queue_config_validates_its_flags(
    invoke: Invoke, store_root: Path, extra: list[str]
) -> None:
    result = queue_config(invoke, "main", *extra)
    assert result.returncode == 2, result.stderr
    assert snapshot(store_root) == {}


def test_a_per_resource_default_sets_a_new_turns_deadline_and_minutes_overrides_it(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    assert queue_config(invoke, "main", "--resource", ENGINE, "--minutes", "30").returncode == 0
    assert reserve(invoke, "lane-a") == [holder_line("lane-a", 0, 30)]  # the config's default
    assert reserve(invoke, "lane-a", resource="gpu") == [holder_line("lane-a", 0, 60, "gpu")]
    assert queue_config(invoke, "main", "--resource", "disk", "--minutes", "30").returncode == 0
    assert reserve(invoke, "lane-b", "--minutes", "45", resource="disk") == [
        holder_line("lane-b", 0, 45, "disk")  # --minutes overrides the config
    ]
    clock.at(1)
    reserve(invoke, "lane-b")  # no --minutes: decided when promoted
    clock.at(2)
    reserve(invoke, "lane-c", "--minutes", "15")
    # The default is read when the turn is granted, so a later change applies.
    assert queue_config(invoke, "main", "--resource", ENGINE, "--minutes", "20").returncode == 0
    clock.at(10)
    assert queue_rows(release(invoke, "lane-a")) == [holder_line("lane-b", 10, 30)]
    clock.at(12)
    assert queue_rows(release(invoke, "lane-b")) == [holder_line("lane-c", 12, 27)]  # its own 15


def test_a_non_coordinator_move_exits_2_and_writes_nothing(
    invoke: Invoke, clock: Clock, store_root: Path, host: FakeHost
) -> None:
    four_in_line(invoke, clock)
    before = snapshot(store_root)
    for lane in ("lane-a", "lane-d"):  # the holder, and a waiter moving itself
        result = move(invoke, lane, "lane-d", "--to", "1")
        assert result.returncode == 2 and result.stdout == ""
        assert "error: not-coordinator:" in result.stderr
        assert snapshot(store_root) == before
    assert queue_config(invoke, "main", "--coordinator", COORD).returncode == 0
    before = snapshot(store_root)
    result = move(invoke, "main", "lane-d", "--to", "1")  # main is no longer it
    assert result.returncode == 2 and "error: not-coordinator:" in result.stderr
    assert snapshot(store_root) == before and host.sent == []


def test_the_coordinators_move_changes_only_the_waiter_order(
    invoke: Invoke, clock: Clock, store_root: Path, host: FakeHost
) -> None:
    four_in_line(invoke, clock)
    holder = parsed(store_root).holder
    moved = move(invoke, "main", "lane-d", "--to", "1")
    assert moved.returncode == 0, moved.stderr
    assert moved.lines == [
        holder_line("lane-a", 0, 60),
        waiter_line(1, "lane-d", 3),
        waiter_line(2, "lane-b", 1),
        waiter_line(3, "lane-c", 2),
    ]
    record = parsed(store_root)
    assert record.holder == holder and record.grant_seq == 1  # the holder is untouched
    assert {w.lane: w for w in record.waiters}["lane-b"].minutes == 30  # entries kept whole
    assert move(invoke, "main", "lane-d", "--after", "lane-c").returncode == 0
    assert waiters(store_root) == ["lane-b", "lane-c", "lane-d"]
    assert move(invoke, "main", "lane-b", "--to", "3").returncode == 0
    assert waiters(store_root) == ["lane-c", "lane-d", "lane-b"]
    assert move(invoke, "main", "lane-b", "--after", "lane-c").returncode == 0
    assert waiters(store_root) == ["lane-c", "lane-b", "lane-d"]
    assert notes(store_root) == [] and host.sent == []  # a move rings no one


@pytest.mark.parametrize(
    ("waiter", "place", "code"),
    [
        ("lane-a", ["--to", "1"], "holder-cannot-move"),
        ("lane-a", ["--after", "lane-c"], "holder-cannot-move"),
        ("lane-x", ["--to", "1"], "not-waiting"),
        ("lane-b", ["--after", "lane-x"], "not-waiting"),
        ("lane-b", ["--after", "lane-a"], "not-waiting"),  # the holder is not a waiter
        ("lane-b", ["--to", "3"], "position-out-of-range"),
        ("lane-b", ["--after", "lane-b"], "invalid-argument"),
        ("lane-b", ["--to", "0"], None),
        ("lane-b", [], None),
        ("lane-b", ["--to", "1", "--after", "lane-c"], None),
    ],
)
def test_a_move_the_queue_refuses_writes_nothing(
    invoke: Invoke,
    clock: Clock,
    store_root: Path,
    waiter: str,
    place: list[str],
    code: str | None,
) -> None:
    three_in_line(invoke, clock)
    before = snapshot(store_root)
    result = move(invoke, "main", waiter, *place)
    assert result.returncode == 2 and result.stdout == ""
    if code is not None:
        assert f"error: {code}:" in result.stderr, result.stderr
    assert snapshot(store_root) == before


def test_a_move_on_a_queue_nobody_reserved_is_refused(invoke: Invoke, store_root: Path) -> None:
    result = move(invoke, "main", "lane-b", "--to", "1")
    assert result.returncode == 2 and "error: not-waiting:" in result.stderr
    assert snapshot(store_root) == {}


def test_a_move_that_keeps_the_order_writes_nothing(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    three_in_line(invoke, clock)
    before = snapshot(store_root)
    for place in (["--to", "2"], ["--after", "lane-b"]):
        result = move(invoke, "main", "lane-c", *place)
        assert result.returncode == 0, result.stderr
        assert result.lines == [
            holder_line("lane-a", 0, 60),
            waiter_line(1, "lane-b", 1),
            waiter_line(2, "lane-c", 2),
        ]
        assert "already at position 2 on engine; nothing written" in result.stderr
    assert snapshot(store_root) == before


def test_after_a_move_the_next_release_rings_the_moved_lane(
    invoke: Invoke, clock: Clock, store_root: Path, host: FakeHost
) -> None:
    bind(invoke, "lane-b", "lane-c", "lane-d")
    four_in_line(invoke, clock)
    assert move(invoke, "main", "lane-d", "--to", "1").returncode == 0
    clock.at(10)
    result = release(invoke, "lane-a")
    assert result.returncode == 0, result.stderr
    (note,) = notes(store_root)
    assert result.lines == [holder_line("lane-d", 10, 70), *ring_lines(note, "lane-d", 1)]
    assert note.to == "lane-d" and host.rung() == ["lane-d"]
    assert waiters(store_root) == ["lane-b", "lane-c"]


def test_the_coordinators_release_promotes_and_rings_as_the_holders_own_release_does(
    invoke: Invoke, clock: Clock, store_root: Path, host: FakeHost
) -> None:
    bind(invoke, "lane-a", "lane-b", "lane-c")
    three_in_line(invoke, clock)
    clock.at(10)
    result = coordinator_release(invoke, "main", "lane-a")
    assert result.returncode == 0, result.stderr
    (note,) = notes(store_root)
    assert result.lines == [holder_line("lane-b", 10, 40), *ring_lines(note, "lane-b", 1)]
    assert result.stderr == ""
    record = parsed(store_root)
    assert record.holder is not None
    assert (record.holder.lane, record.holder.granted_by, record.grant_seq) == ("lane-b", "main", 2)
    assert [w.lane for w in record.waiters] == ["lane-c"]
    # One reservation note, from the coordinator to the new holder; one doorbell.
    assert (note.sender, note.to, note.kind, note.needs) == (
        (PROJECT, "main"),
        "lane-b",
        "reservation",
        "reply",
    )
    assert note.subject == "Your turn on engine" and note.idempotency_key == "turn-engine-2"
    assert "lane main ended the last turn" in note.body
    assert host.rung() == ["lane-b"]
    assert inbox_ids(invoke, "lane-a") == [] and inbox_ids(invoke, "lane-c") == []
    # The ended holder is out of the line: its own release is now a no-op.
    assert release(invoke, "lane-a").lines == ["nothing to release"]
    # The new holder's own release afterwards works as before.
    clock.at(20)
    own = release(invoke, "lane-b")
    assert own.returncode == 0 and host.rung() == ["lane-b", "lane-c"]


def test_the_coordinators_release_skips_a_done_card_and_exits_5_on_a_failed_ring(
    invoke: Invoke, clock: Clock, store_root: Path, host: FakeHost
) -> None:
    three_in_line(invoke, clock)  # nobody is bound
    card_set(invoke, "lane-b", "done")
    clock.at(10)
    result = coordinator_release(invoke, "main", "lane-a")
    assert result.returncode == 5
    (note,) = notes(store_root)
    assert result.lines == [
        holder_line("lane-c", 10, 70),
        "skipped lane-b: card is done",
        f"note: {note.id} is in the store",
        "wake: not delivered",
    ]
    assert note.to == "lane-c" and host.sent == []


def test_a_retried_coordinator_release_reposts_a_missing_note_from_the_coordinator(
    invoke: Invoke, clock: Clock, store_root: Path, host: FakeHost
) -> None:
    bind(invoke, "lane-b")
    three_in_line(invoke, clock)
    # The crash: the coordinator's promotion is written, then the process dies.
    queue.release(store.Store.open(), PROJECT, ENGINE, "main", T0, holder="lane-a")
    assert notes(store_root) == []
    # Repeating the same command is refused: lane-a no longer holds engine.
    again = coordinator_release(invoke, "main", "lane-a")
    assert again.returncode == 2 and "error: not-holder:" in again.stderr
    assert "re-told by release without --holder" in again.stderr
    # The D22 retry works for the coordinator, which granted the turn.
    retry = release(invoke, "main")
    assert retry.returncode == 0, retry.stderr
    (note,) = notes(store_root)
    assert (note.sender, note.to, note.idempotency_key) == (
        (PROJECT, "main"),
        "lane-b",
        "turn-engine-2",
    )
    assert host.rung() == ["lane-b"]
    assert release(invoke, "main").lines == ["nothing to release"]
    assert len(notes(store_root)) == 1


def test_a_coordinator_release_by_another_lane_exits_2_and_writes_nothing(
    invoke: Invoke, clock: Clock, store_root: Path, host: FakeHost
) -> None:
    three_in_line(invoke, clock)
    before = snapshot(store_root)
    for lane in ("lane-b", "lane-a", "lane-d"):  # a waiter, the holder itself, an outsider
        result = coordinator_release(invoke, lane, "lane-a")
        assert result.returncode == 2 and result.stdout == ""
        assert "error: not-coordinator:" in result.stderr
        assert snapshot(store_root) == before
    assert notes(store_root) == [] and host.sent == []


def test_a_coordinator_release_of_a_lane_not_holding_exits_2_and_writes_nothing(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    nothing = coordinator_release(invoke, "main", "lane-a")  # no record at all
    assert nothing.returncode == 2 and "error: not-holder:" in nothing.stderr
    assert snapshot(store_root) == {}
    three_in_line(invoke, clock)
    before = snapshot(store_root)
    for named in ("lane-b", "lane-d"):
        result = coordinator_release(invoke, "main", named)
        assert result.returncode == 2 and "error: not-holder:" in result.stderr
        assert "engine is held by lane lane-a" in result.stderr
        assert snapshot(store_root) == before
    assert notes(store_root) == []
    bad = coordinator_release(invoke, "main", "a--b")
    assert bad.returncode == 2 and "error: invalid-argument: --holder" in bad.stderr


DAMAGED_CONFIG: dict[str, bytes] = {
    "invalid-json": b'{"project": ',
    "unknown-field": json.dumps({"project": PROJECT, "resource_minutes": {}, "x": 1}).encode(),
    "missing-field": json.dumps({"project": PROJECT}).encode(),
    "other-project": json.dumps({"project": "other", "resource_minutes": {}}).encode(),
    "bad-coordinator": json.dumps(
        {"project": PROJECT, "coordinator": "all", "resource_minutes": {}}
    ).encode(),
    "bad-minutes": json.dumps({"project": PROJECT, "resource_minutes": {ENGINE: 0}}).encode(),
    "bad-resource": json.dumps({"project": PROJECT, "resource_minutes": {"con": 5}}).encode(),
    "duplicate-key": b'{"project": "x", "project": "x", "resource_minutes": {}}',
    "oversize": b'{"project": "' + b" " * protocol.QUEUE_MAX_BYTES + b'"}',
}

CHANGING_VERBS: tuple[tuple[str, ...], ...] = (
    ("reserve", ENGINE, "--project", PROJECT, "--lane", "lane-d"),
    ("reserve", "gpu", "--project", PROJECT, "--lane", "lane-d"),
    ("reserve", ENGINE, "--project", PROJECT, "--lane", "lane-c", "--cancel"),
    ("release", ENGINE, "--project", PROJECT, "--lane", "lane-a"),
    ("release", ENGINE, "--project", PROJECT, "--lane", "main", "--holder", "lane-a"),
    ("queue", "move", ENGINE, "--project", PROJECT, "--lane", "main", "--waiter", "lane-c")
    + ("--to", "1"),
    ("queue", "config", "--project", PROJECT, "--lane", "main", "--stall-minutes", "5"),
    ("queue", "config", "--project", PROJECT, "--lane", "main"),
)


@pytest.mark.parametrize("kind", [*DAMAGED_CONFIG, "folder"])
def test_a_damaged_config_stops_every_changing_verb(
    invoke: Invoke, clock: Clock, store_root: Path, kind: str
) -> None:
    three_in_line(invoke, clock)
    if kind == "folder":
        config_file(store_root).mkdir()
    else:
        config_file(store_root).write_bytes(DAMAGED_CONFIG[kind])
    before = snapshot(store_root)
    for args in CHANGING_VERBS:
        result = invoke(*args)
        assert result.returncode == 3, (args, result.stderr)
        assert result.stdout == ""
        assert f"error: queue-invalid: queues/{PROJECT}/config.json" in result.stderr
        assert snapshot(store_root) == before
    if kind == "folder":
        assert config_file(store_root).is_dir()
    for show_args in (
        ["queue", "show", "--project", PROJECT],
        ["queue", "show", ENGINE, "--project", PROJECT],
    ):
        shown = invoke(*show_args)
        assert shown.returncode == 0
        assert shown.lines[:3] == [
            holder_line("lane-a", 0, 60),
            waiter_line(1, "lane-b", 1),
            waiter_line(2, "lane-c", 2),
        ]
        assert shown.lines[3].startswith(f"diagnostic: queues/{PROJECT}/config.json: ")
        assert shown.lines[4:] == ["diagnostics: 1"]
    assert snapshot(store_root) == before


def test_queue_show_reads_a_valid_config_quietly_and_reports_its_leftovers(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    assert queue_config(invoke, "main", "--resource", ENGINE, "--minutes", "30").returncode == 0
    reserve(invoke, "lane-a")
    folder = queue_folder(store_root)
    assert sorted(p.name for p in folder.iterdir()) == ["config.json", "engine.queue.json"]
    assert invoke("queue", "show", "--project", PROJECT).lines == [
        holder_line("lane-a", 0, 30),
        "diagnostics: 0",
    ]
    (folder / "config.json.lock").write_bytes(b"x")  # transient: a change in progress
    (folder / "config.json.break").write_bytes(b"x")
    (folder / "config.json.0a1b2c3d.tmp").write_bytes(b"x")
    for args in (
        ["queue", "show", "--project", PROJECT],
        ["queue", "show", ENGINE, "--project", PROJECT],
    ):
        assert invoke(*args).lines == [
            holder_line("lane-a", 0, 30),
            f"diagnostic: queues/{PROJECT}/config.json.0a1b2c3d.tmp: {LEFTOVER}",
            "diagnostics: 1",
        ]


def test_the_config_lock_never_collides_with_a_resource_lock(
    invoke: Invoke, clock: Clock, store_root: Path
) -> None:
    """A resource named ``config`` keeps its own files beside the config's."""
    assert queue_config(invoke, "main", "--resource", "config", "--minutes", "5").returncode == 0
    assert reserve(invoke, "lane-a", resource="config") == [holder_line("lane-a", 0, 5, "config")]
    folder = queue_folder(store_root)
    assert sorted(p.name for p in folder.iterdir()) == ["config.json", "config.queue.json"]
    assert queue.CONFIG_LOCK_NAME == "config.json.lock"
    with pytest.raises(protocol.ProtocolError):
        protocol.check_resource_id("config.json")


def test_the_console_script_configures_moves_and_releases_for_the_coordinator(
    store_root: Path,
) -> None:
    """The installed entry point, in separate processes, end to end."""
    env = child_env(store_root)

    def run(*args: str) -> Result:
        return run_cli(list(args), root=store_root, env=env)

    named = run("queue", "config", "--project", PROJECT, "--lane", "main", "--coordinator", COORD)
    assert named.returncode == 0, named.stderr
    assert named.lines[0] == TAB.join(["coordinator", COORD])
    config_args = ("--project", PROJECT, "--lane", COORD, "--resource", ENGINE, "--minutes", "25")
    assert run("queue", "config", *config_args).returncode == 0
    for lane in ("lane-a", "lane-b", "lane-c"):
        assert run("reserve", ENGINE, "--project", PROJECT, "--lane", lane).returncode == 0
    move_args = ("--project", PROJECT, "--lane", COORD, "--waiter", "lane-c", "--to", "1")
    moved = run("queue", "move", ENGINE, *move_args)
    assert moved.returncode == 0, moved.stderr
    assert [line.split(TAB)[3] for line in moved.lines[1:]] == ["lane-c", "lane-b"]
    refused = run("release", ENGINE, "--project", PROJECT, "--lane", "main", "--holder", "lane-a")
    assert refused.returncode == 2 and "not-coordinator" in refused.stderr
    released = run("release", ENGINE, "--project", PROJECT, "--lane", COORD, "--holder", "lane-a")
    assert released.returncode == cli.EXIT_WAKE, released.stderr  # lane-c is not bound
    assert released.lines[0].split(TAB)[:3] == ["holder", ENGINE, "lane-c"]
    record = parsed(store_root)
    assert record.holder is not None and record.holder.granted_by == COORD
    granted = datetime.strptime(record.holder.granted_utc, "%Y-%m-%dT%H:%M:%SZ")
    due = datetime.strptime(record.holder.deadline_utc, "%Y-%m-%dT%H:%M:%SZ")
    assert due - granted == timedelta(minutes=25)
    for action in ("move", "config"):
        help_text = run("queue", action, "--help")
        flat = " ".join(help_text.stdout.split())
        assert help_text.returncode == 0 and "Only the project's coordinator (lane main" in flat


def test_the_cli_doc_describes_the_coordinator_verbs() -> None:
    root = Path(__file__).resolve().parents[1]
    cli_doc = " ".join((root / "docs" / "cli.md").read_text(encoding="utf-8").split())
    for text in (
        "passing-notes queue config",
        "passing-notes queue move",
        "--holder",
        "not-coordinator",
        "holder-cannot-move",
        "not-waiting",
        "position-out-of-range",
        "identity, not security",
    ):
        assert text in cli_doc, text
