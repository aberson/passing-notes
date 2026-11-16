"""Resource queues (plan Step 5): reserve, release, reserve --cancel and queue show.

Every test drives the production ``cli.main`` (the ``invoke`` fixture) or the console
script (``run_cli``) over the autouse temporary store. Turn times come from a fixed
clock: ``cli._now_utc`` is replaced, as it is for the inbox views.
"""

from __future__ import annotations

import json
import os
import time
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
    child_env,
    run_cli,
    run_together,
    snapshot,
)

from passing_notes import cli, protocol, queue, store

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
    assert result.returncode == 0, result.stderr
    assert result.lines == [holder_line("lane-b", 10, 40)]  # lane-b's own 30 minutes
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
    assert second.lines == [holder_line("lane-c", 20, 80)]  # the default for lane-c
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
    assert first.lines == [holder_line("lane-b", 10, 40)]
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
    assert released.lines == [holder_line("lane-c", 10, 70)]


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
    assert sorted(snapshot(store_root)) == [
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
    assert released.returncode == 0, released.stderr
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
