"""``passing-notes ack --older-than H [--dry-run]``: clear old fyi and broadcast notes in
one step (plan D3, D65), through the real console script and in process."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import (
    PROJECT,
    EmptyStatusHost,
    Invoke,
    note_id,
    note_text,
    place_ack,
    place_note,
    run_cli,
)
from test_status import tree

from passing_notes import cli, liveness, protocol, status, sweep
from passing_notes.store import Store

NOTE_COUNT = 290
OLDER_THAN_HOURS = 48
FIXED_NOW = datetime(2030, 1, 10, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Kind:
    """One category of fixture note, and whether ``--older-than 48`` must ack it."""

    name: str
    to: str
    needs: str
    kind: str
    old: bool
    expected: bool
    sender: str = "lane-a"
    pre_acked: bool = False


# Mixed kinds and ages for one main. Only old fyi notes and old broadcasts (never an
# operator decision) are acknowledged; everything else stays as it was.
KINDS = (
    Kind("old fyi to main", "main", "fyi", "fyi", old=True, expected=True),
    Kind("young fyi to main", "main", "fyi", "fyi", old=False, expected=False),
    Kind("old fyi broadcast", "all", "fyi", "fyi", old=True, expected=True),
    Kind("old reply broadcast", "all", "reply", "question", old=True, expected=True),
    Kind("old ack broadcast", "all", "ack", "change-notice", old=True, expected=True),
    Kind("young reply broadcast", "all", "reply", "question", old=False, expected=False),
    Kind(
        "old decision broadcast", "all", "operator-decision", "question", old=True, expected=False
    ),
    Kind("old direct reply", "main", "reply", "question", old=True, expected=False),
    Kind("old direct ack", "main", "ack", "handoff", old=True, expected=False),
    Kind("old direct decision", "main", "operator-decision", "question", old=True, expected=False),
    Kind("old fyi to another lane", "lane-c", "fyi", "fyi", old=True, expected=False),
    Kind("old own broadcast", "all", "fyi", "fyi", old=True, expected=False, sender="main"),
    Kind("old fyi already acked", "main", "fyi", "fyi", old=True, expected=False, pre_acked=True),
    Kind("old decision to operator", "operator", "operator-decision", "question", True, False),
)


@dataclass
class Fixture:
    expected: list[str] = field(default_factory=list)
    untouched: dict[str, Kind] = field(default_factory=dict)


def put(
    root: Path,
    created: datetime,
    sender: str,
    to: str,
    hex4: str,
    *,
    needs: str = "fyi",
    kind: str = "fyi",
) -> str:
    stamp = created.strftime("%Y%m%dT%H%M%SZ")
    text = note_text(
        stamp=stamp,
        created_utc=protocol.format_utc(created),
        sender=sender,
        recipient=to,
        hex4=hex4,
        needs=needs,
        kind=kind,
    )
    nid = note_id(stamp, sender, to, hex4)
    place_note(root, text, f"{nid}.md")
    return nid


def build(root: Path, now: datetime) -> Fixture:
    """NOTE_COUNT notes cycling through KINDS. Old notes are at least 49 hours old and
    young ones at most 47, so a console script started seconds later sees the same split."""
    found = Fixture()
    for i in range(NOTE_COUNT):
        k = KINDS[i % len(KINDS)]
        age = timedelta(hours=49, minutes=i) if k.old else timedelta(hours=47, minutes=-i)
        nid = put(
            root,
            now - age,
            k.sender,
            k.to,
            f"{i:04x}",
            needs=k.needs,
            kind=k.kind,
        )
        if k.pre_acked:
            data = protocol.render_ack(protocol.make_ack("main", now)).encode("utf-8")
            place_ack(root, data, protocol.ack_file_name(nid, "main"))
        if k.expected:
            found.expected.append(nid)
        else:
            found.untouched[nid] = k
    found.expected.sort()  # inbox order: by note id
    return found


def ack_files(root: Path) -> set[str]:
    folder = root / "acks" / PROJECT
    return {p.name for p in folder.iterdir()} if folder.exists() else set()


def bulk(*extra: str) -> list[str]:
    return [
        "ack",
        "--project",
        PROJECT,
        "--lane",
        "main",
        "--older-than",
        str(OLDER_THAN_HOURS),
        *extra,
    ]


def test_bulk_ack_of_a_290_note_backlog_through_the_console_script(store_root: Path) -> None:
    """D65 acceptance: the dry run lists exactly the old fyi and to-all notes and writes
    nothing; the real run writes one ack per listed id and prints the same ids, leaving
    every direct reply, ack and operator-decision note unacked; a repeat is a no-op."""
    fixture = build(store_root, datetime.now(UTC).replace(microsecond=0))
    assert len(fixture.expected) + len(fixture.untouched) == NOTE_COUNT
    assert len(fixture.expected) > 0
    before = tree(store_root)
    acks_before = ack_files(store_root)

    dry = run_cli(bulk("--dry-run"), root=store_root)
    assert dry.returncode == 0, dry.stderr
    assert dry.stderr == ""
    assert dry.lines == [f"would ack {nid}" for nid in fixture.expected] + [
        f"would ack: {len(fixture.expected)}"
    ]
    assert tree(store_root) == before

    real = run_cli(bulk(), root=store_root)
    assert real.returncode == 0, real.stderr
    assert real.stderr == ""
    assert real.lines == [f"acked {nid}" for nid in fixture.expected] + [
        f"acked: {len(fixture.expected)}"
    ]
    written = ack_files(store_root) - acks_before
    assert written == {protocol.ack_file_name(nid, "main") for nid in fixture.expected}
    store = Store(store_root)
    for nid in fixture.expected:
        text = store.ack_path(PROJECT, nid, "main").read_text(encoding="utf-8")
        assert text.splitlines()[-1] == protocol.ACK_RECEIVED  # receipt, nothing more
    for nid, k in fixture.untouched.items():
        if not k.pre_acked:
            assert not store.ack_path(PROJECT, nid, "main").exists(), k.name
    names = {k.name for k in fixture.untouched.values()}
    assert {"old direct reply", "old direct ack", "old direct decision"} <= names

    again = run_cli(bulk(), root=store_root)
    assert again.returncode == 0, again.stderr
    assert again.lines == ["acked: 0"]
    assert ack_files(store_root) - acks_before == written


@pytest.mark.parametrize(
    "args",
    [
        ("--older-than", "48", "20300101T120000Z-lane-a-to-main-0001"),
        (),
    ],
    ids=["both", "neither"],
)
def test_exactly_one_of_id_and_older_than_through_the_console_script(
    store_root: Path, args: tuple[str, ...]
) -> None:
    result = run_cli(["ack", "--project", PROJECT, "--lane", "main", *args], root=store_root)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("passing-notes: error: invalid-argument: ")
    assert "exactly one of ID and --older-than" in result.stderr
    assert not (store_root / "acks").exists()


def test_dry_run_needs_older_than(store_root: Path, invoke: Invoke) -> None:
    nid = put(store_root, FIXED_NOW, "lane-a", "main", "0001", needs="fyi")
    result = invoke("ack", "--project", PROJECT, "--lane", "main", "--dry-run", nid)
    assert result.returncode == 2
    assert "invalid-argument: --dry-run applies only with --older-than" in result.stderr
    assert not (store_root / "acks").exists()


@pytest.mark.parametrize("hours", ["0", "-1", "1.5", "x"])
def test_older_than_takes_a_positive_whole_number(invoke: Invoke, hours: str) -> None:
    result = invoke("ack", "--project", PROJECT, "--lane", "main", "--older-than", hours)
    assert result.returncode == 2
    assert result.stdout == ""


def test_the_single_id_form_is_unchanged(store_root: Path, invoke: Invoke) -> None:
    nid = put(store_root, FIXED_NOW, "lane-a", "main", "0001", needs="reply", kind="question")
    args = ("ack", "--project", PROJECT, "--lane", "main", "--session", "window-1", nid)
    assert invoke(*args).lines == [f"acknowledged {nid} as main"]
    assert invoke(*args).lines == [f"already acknowledged: {nid} by main"]


def test_session_applies_to_bulk_acks(
    store_root: Path, invoke: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_now_utc", lambda: FIXED_NOW)
    old = [
        put(store_root, FIXED_NOW - timedelta(hours=72, minutes=i), "lane-a", "all", f"{i:04x}")
        for i in range(2)
    ]
    result = invoke(*bulk("--session", "window-7"))
    assert result.returncode == 0, result.stderr
    assert result.lines == [f"acked {nid}" for nid in sorted(old)] + ["acked: 2"]
    store = Store(store_root)
    for nid in old:
        lines = store.ack_path(PROJECT, nid, "main").read_text(encoding="utf-8").splitlines()
        assert "reader: main" in lines and "session: window-7" in lines


def test_the_age_is_strictly_more_than_h_hours(
    store_root: Path, invoke: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_now_utc", lambda: FIXED_NOW)
    exactly = put(
        store_root, FIXED_NOW - timedelta(hours=48), "lane-a", "main", "0001", needs="fyi"
    )
    past = put(
        store_root,
        FIXED_NOW - timedelta(hours=48, seconds=1),
        "lane-a",
        "main",
        "0002",
        needs="fyi",
    )
    future = put(store_root, FIXED_NOW + timedelta(hours=1), "lane-a", "main", "0003", needs="fyi")
    result = invoke(*bulk("--dry-run"))
    assert result.lines == [f"would ack {past}", "would ack: 1"]
    assert exactly not in result.stdout and future not in result.stdout


def test_bulk_ack_and_the_sweep_share_one_kind_rule(
    store_root: Path, tmp_path: Path, invoke: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``protocol.bulk_ackable`` is the one owner of the D65 kind rule: patch it once and
    both ``ack --older-than`` and the sweep's backlog-bulk row follow."""
    monkeypatch.setattr(cli, "_now_utc", lambda: FIXED_NOW)
    old = FIXED_NOW - timedelta(hours=72)
    fyi = put(store_root, old, "lane-a", "main", "0001", needs="fyi")
    reply = put(store_root, old, "lane-a", "main", "0002", needs="reply", kind="question")
    store = Store(store_root)

    def backlog_bulk() -> str | None:
        observer = liveness.Observer(EmptyStatusHost(), tmp_path / "data")
        read = status._project_block(store, PROJECT, FIXED_NOW, 24, observer)
        rows = [r for r in sweep._note_rows(read, set(), FIXED_NOW) if r.kind == "backlog-bulk"]
        return rows[0].evidence if rows else None

    assert invoke(*bulk("--dry-run")).lines == [f"would ack {fyi}", "would ack: 1"]
    assert backlog_bulk() == f"1 notes; oldest {fyi}"
    seen: list[str] = []

    def only_replies(note: protocol.Note) -> bool:
        seen.append(note.id)
        return note.needs == "reply"

    monkeypatch.setattr(protocol, "bulk_ackable", only_replies)
    assert invoke(*bulk("--dry-run")).lines == [f"would ack {reply}", "would ack: 1"]
    assert backlog_bulk() == f"1 notes; oldest {reply}"
    assert seen.count(reply) == 2  # once from each caller
