"""Unit tests for store.py: root resolution, store-in-repo, publication, scanning."""

from __future__ import annotations

import errno
import importlib.util
import os
import re
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import (
    CREATED,
    LINK_KINDS,
    PROJECT,
    Crash,
    MakeLink,
    StoreCall,
    place_ack,
    place_card,
    place_note,
    snapshot,
)

from passing_notes import protocol, store
from passing_notes.protocol import ACK_MAX_BYTES, NOTE_MAX_BYTES, PUBLISH_ATTEMPTS, ProtocolError
from passing_notes.store import (
    PUBLISH_FAILED,
    STORE_IN_REPO,
    STORE_LINK,
    STORE_UNUSABLE,
    TMP_HEX_CHARS,
    TMP_SUFFIX,
    NoteNotFoundError,
    Store,
    StoreError,
    find_git_worktree,
    is_link_or_reparse_point,
    resolve_root,
)

NOTE_ID = "20300101T120000Z-lane-a-to-lane-b-1a2b"


def temp_name_pattern(final_name: str) -> re.Pattern[str]:
    return re.compile(
        rf"{re.escape(final_name)}\.[0-9a-f]{{{TMP_HEX_CHARS}}}{re.escape(TMP_SUFFIX)}"
    )


def fixed_now() -> datetime:
    return CREATED


def hex_sequence(*values: str) -> Iterator[str]:
    yield from values


def make_draft(**overrides: object) -> protocol.Draft:
    fields: dict[str, object] = {
        "project": PROJECT,
        "from_lane": "lane-a",
        "to": "lane-b",
        "subject": "Synthetic subject",
        "kind": "fyi",
        "needs": "ack",
    }
    fields.update(overrides)
    return protocol.make_draft(**fields)  # type: ignore[arg-type]


# --- Root resolution ------------------------------------------------------------------


def test_root_override_and_default(tmp_path: Path) -> None:
    override = tmp_path / "custom"
    assert resolve_root({store.ROOT_ENV: str(override)}) == override
    assert resolve_root({store.LOCALAPPDATA_ENV: str(tmp_path)}) == tmp_path / "passing-notes"
    both = {store.ROOT_ENV: str(override), store.LOCALAPPDATA_ENV: str(tmp_path)}
    assert resolve_root(both) == override


@pytest.mark.parametrize(
    "environ",
    [{}, {store.ROOT_ENV: "relative/store"}, {store.LOCALAPPDATA_ENV: "relative"}],
)
def test_root_unusable(environ: dict[str, str]) -> None:
    with pytest.raises(StoreError) as caught:
        resolve_root(environ)
    assert caught.value.code == STORE_UNUSABLE


def test_root_that_is_a_file_is_unusable(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.write_text("not a folder", encoding="utf-8")
    with pytest.raises(StoreError) as caught:
        Store.open({store.ROOT_ENV: str(root)})
    assert caught.value.code == STORE_UNUSABLE


@pytest.mark.parametrize("marker_is_file", [False, True])
@pytest.mark.parametrize("relative", ["", "store", "deep/er/store"])
def test_store_in_repo_is_refused(tmp_path: Path, marker_is_file: bool, relative: str) -> None:
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    if marker_is_file:  # a worktree or submodule checkout has a .git file
        (repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    else:
        (repo / ".git").mkdir()
    root = repo / relative if relative else repo
    assert find_git_worktree(root) == repo
    with pytest.raises(StoreError) as caught:
        Store.open({store.ROOT_ENV: str(root)})
    assert caught.value.code == STORE_IN_REPO
    assert not (repo / "store").exists() and not (repo / "deep").exists()


def test_store_outside_repo_is_accepted(store_root: Path) -> None:
    assert find_git_worktree(store_root) is None
    assert Store.open().root == store_root


# --- Store folders that are links ---------------------------------------------------------


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_is_link_or_reparse_point(tmp_path: Path, dir_link: MakeLink, kind: str) -> None:
    real = tmp_path / "real"
    real.mkdir()
    a_file = tmp_path / "a-file"
    a_file.write_bytes(b"x")
    assert not is_link_or_reparse_point(real)
    assert not is_link_or_reparse_point(a_file)
    assert not is_link_or_reparse_point(tmp_path / "missing")
    assert not is_link_or_reparse_point(a_file / "below-a-file")
    link = tmp_path / "link"
    dir_link(link, real, kind)
    assert is_link_or_reparse_point(link)
    assert not is_link_or_reparse_point(link / "missing")


_MESSAGE_CALLS: list[StoreCall] = [
    lambda s: s.inbox(PROJECT, "lane-b"),
    lambda s: s.load_note(PROJECT, NOTE_ID),
    lambda s: s.find_idempotent(PROJECT, (PROJECT, "lane-a"), "key-1"),
    lambda s: s.publish_note(make_draft(), "Body.", now=fixed_now, hex_source=lambda: "1a2b"),
]
_ACK_CALLS: list[StoreCall] = [
    lambda s: s.inbox(PROJECT, "lane-b"),
    lambda s: s.publish_ack(PROJECT, NOTE_ID, protocol.make_ack("lane-b", CREATED)),
]


_FLAT_SCAN: list[StoreCall] = [lambda s: s.scan_flat()]


@pytest.mark.parametrize("kind", LINK_KINDS)
@pytest.mark.parametrize(
    ("level", "calls"),
    [
        ("messages", _FLAT_SCAN + _MESSAGE_CALLS),
        (f"messages/{PROJECT}", _MESSAGE_CALLS),
        ("acks", _FLAT_SCAN + _ACK_CALLS),
        (f"acks/{PROJECT}", _ACK_CALLS),
    ],
)
def test_store_folder_links_are_refused(
    store_root: Path,
    tmp_path: Path,
    dir_link: MakeLink,
    kind: str,
    level: str,
    calls: list[StoreCall],
) -> None:
    """Nothing is read or written through a linked messages, acks or project folder."""
    target = tmp_path / "elsewhere"  # outside any Git tree: only the link check applies
    target.mkdir()
    link = store_root / level
    link.parent.mkdir(parents=True, exist_ok=True)
    dir_link(link, target, kind)
    opened = Store.open()
    for call in calls:
        with pytest.raises(StoreError) as caught:
            call(opened)
        assert caught.value.code == STORE_LINK
        assert "symbolic link, junction or other reparse point" in caught.value.message
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_store_root_link_is_refused(
    store_root: Path, tmp_path: Path, dir_link: MakeLink, kind: str
) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    store_root.parent.mkdir(parents=True, exist_ok=True)
    dir_link(store_root, target, kind)
    with pytest.raises(StoreError) as caught:
        Store.open()
    assert caught.value.code == STORE_LINK
    assert list(target.iterdir()) == []


def test_a_link_planted_while_creating_folders_is_refused(
    opened: Store, tmp_path: Path, dir_link: MakeLink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The project folder is checked again after it is created, before any write."""
    target = tmp_path / "elsewhere"
    target.mkdir()

    def plant(name: str | os.PathLike[str], exist_ok: bool = False) -> None:
        # Creates the parents, then a rival plants a link where the folder belongs.
        folder = Path(name)
        folder.parent.mkdir(parents=True, exist_ok=True)  # os.mkdir, not os.makedirs
        dir_link(folder, target, "junction" if os.name == "nt" else "symlink")

    monkeypatch.setattr(os, "makedirs", plant)  # the os module store.py calls into
    with pytest.raises(StoreError) as caught:
        opened.publish_note(make_draft(), "Body.", now=fixed_now, hex_source=lambda: "1a2b")
    assert caught.value.code == STORE_LINK
    assert list(target.iterdir()) == []


# --- Paths are validated before they are built -------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.messages_dir(".."),
        lambda s: s.acks_dir("../x"),
        lambda s: s.note_path(PROJECT, "../../x"),
        lambda s: s.note_path("..", "20300101T120000Z-lane-a-to-lane-b-1a2b"),
        lambda s: s.ack_path(PROJECT, "20300101T120000Z-lane-a-to-lane-b-1a2b", ".."),
        lambda s: s.load_note(PROJECT, "..\\..\\x"),
        lambda s: s.scan_notes("a/b"),
        lambda s: s.inbox(PROJECT, "../lane-b"),
    ],
)
def test_paths_reject_bad_ids(opened: Store, call: object) -> None:
    with pytest.raises(ProtocolError):
        call(opened)  # type: ignore[operator]
    assert not opened.root.exists()


# --- Note publication -------------------------------------------------------------------


def test_publish_creates_folders_and_a_valid_note(opened: Store) -> None:
    note = opened.publish_note(make_draft(), "Body.", now=fixed_now, hex_source=lambda: "1a2b")
    path = opened.root / "messages" / PROJECT / f"{note.id}.md"
    assert path.is_file()
    assert protocol.parse_note_bytes(path.read_bytes(), file_stem=note.id) == note
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]  # no .tmp left


def test_publish_picks_new_hex_on_clash_and_never_replaces(opened: Store) -> None:
    first = opened.publish_note(make_draft(), "First.", now=fixed_now, hex_source=lambda: "aaaa")
    first_path = opened.root / "messages" / PROJECT / f"{first.id}.md"
    original = first_path.read_bytes()
    values = hex_sequence("aaaa", "bbbb")
    second = opened.publish_note(
        make_draft(), "Second.", now=fixed_now, hex_source=lambda: next(values)
    )
    assert second.id.endswith("-bbbb")
    assert first_path.read_bytes() == original


def test_leftover_tmp_files_never_block_a_note(opened: Store) -> None:
    """Hand-written and CLI-style leftovers of the same name stay; the note still lands."""
    folder = opened.root / "messages" / PROJECT
    folder.mkdir(parents=True)
    name = "20300101T120000Z-lane-a-to-lane-b-aaaa.md"
    leftovers = [folder / f"{name}.tmp", folder / f"{name}.0123abcd.tmp"]
    for leftover in leftovers:
        leftover.write_bytes(b"partial")
    note = opened.publish_note(make_draft(), "Body.", now=fixed_now, hex_source=lambda: "aaaa")
    assert f"{note.id}.md" == name
    assert all(leftover.read_bytes() == b"partial" for leftover in leftovers)
    _, diagnostics = opened.scan_notes(PROJECT)
    assert sorted(d.path.rsplit("/", 1)[1] for d in diagnostics) == sorted(
        p.name for p in leftovers
    )
    assert all("leftover .tmp" in d.message for d in diagnostics)


def test_each_attempt_uses_a_fresh_temporary_name(
    opened: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Notes and acks are written to ``<final name>.<random hex>.tmp``, then renamed."""
    real_rename = store.rename_no_replace
    seen: list[tuple[str, str]] = []

    def spy(src: Path, dst: Path) -> None:
        seen.append((src.name, dst.name))
        real_rename(src, dst)

    monkeypatch.setattr(store, "rename_no_replace", spy)
    note = opened.publish_note(make_draft(), "Body.", now=fixed_now, hex_source=lambda: "1a2b")
    opened.publish_ack(PROJECT, note.id, protocol.make_ack("lane-b", CREATED))
    assert [dst for _, dst in seen] == [f"{note.id}.md", f"{note.id}--lane-b.md"]
    for src, dst in seen:
        assert temp_name_pattern(dst).fullmatch(src), src
    assert seen[0][0] != seen[1][0]


def test_a_taken_temporary_name_is_drawn_again(
    opened: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = opened.root / "acks" / PROJECT
    folder.mkdir(parents=True)
    name = f"{NOTE_ID}--lane-b.md"
    taken = folder / f"{name}.00000000.tmp"
    taken.write_bytes(b"someone else's")
    values = hex_sequence("00000000", "11111111")
    monkeypatch.setattr(store, "default_tmp_hex", lambda: next(values))
    assert opened.publish_ack(PROJECT, NOTE_ID, protocol.make_ack("lane-b", CREATED)) is True
    assert taken.read_bytes() == b"someone else's"
    assert sorted(p.name for p in folder.iterdir()) == sorted([name, taken.name])


def test_ack_gives_up_when_every_temporary_name_is_taken(
    opened: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = opened.root / "acks" / PROJECT
    folder.mkdir(parents=True)
    taken = folder / f"{NOTE_ID}--lane-b.md.00000000.tmp"
    taken.write_bytes(b"x")
    calls: list[str] = []

    def same() -> str:
        calls.append("00000000")
        return "00000000"

    monkeypatch.setattr(store, "default_tmp_hex", same)
    with pytest.raises(StoreError) as caught:
        opened.publish_ack(PROJECT, NOTE_ID, protocol.make_ack("lane-b", CREATED))
    assert caught.value.code == PUBLISH_FAILED
    assert len(calls) == PUBLISH_ATTEMPTS
    assert sorted(p.name for p in folder.iterdir()) == [taken.name]


def test_publish_gives_up_after_the_attempt_limit(opened: Store) -> None:
    opened.publish_note(make_draft(), "Body.", now=fixed_now, hex_source=lambda: "dddd")
    before = snapshot(opened.root)
    calls: list[str] = []

    def same_hex() -> str:
        calls.append("dddd")
        return "dddd"

    with pytest.raises(StoreError) as caught:
        opened.publish_note(make_draft(), "Other.", now=fixed_now, hex_source=same_hex)
    assert caught.value.code == PUBLISH_FAILED
    assert len(calls) == PUBLISH_ATTEMPTS
    assert snapshot(opened.root) == before


def test_publish_survives_losing_the_rename_race(
    opened: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer creates the final name between our .tmp and our rename."""
    real_rename = store.rename_no_replace
    rival = b"rival bytes"
    raced: list[Path] = []

    def racing_rename(src: Path, dst: Path) -> None:
        if not raced:
            dst.write_bytes(rival)
            raced.append(dst)
        real_rename(src, dst)

    monkeypatch.setattr(store, "rename_no_replace", racing_rename)
    values = hex_sequence("eeee", "ffff")
    note = opened.publish_note(
        make_draft(), "Mine.", now=fixed_now, hex_source=lambda: next(values)
    )
    assert note.id.endswith("-ffff")
    assert raced[0].read_bytes() == rival  # the rival's note was not replaced
    names = sorted(p.name for p in raced[0].parent.iterdir())
    assert not [name for name in names if name.endswith(".tmp")]


@pytest.mark.parametrize("variant", [store.rename_no_replace, store.link_no_replace])
def test_no_replace_primitives_refuse_an_existing_target(tmp_path: Path, variant: object) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        variant(src, dst)  # type: ignore[operator]
    assert dst.read_bytes() == b"old"
    other = tmp_path / "other"
    variant(src, other)  # type: ignore[operator]
    assert other.read_bytes() == b"new" and not src.exists()


# --- Ack publication --------------------------------------------------------------------


def test_publish_ack_then_repeat_is_a_noop(opened: Store) -> None:
    note_id = NOTE_ID
    ack = protocol.make_ack("lane-b", CREATED, "window-1")
    assert opened.publish_ack(PROJECT, note_id, ack) is True
    path = opened.root / "acks" / PROJECT / f"{note_id}--lane-b.md"
    first = path.read_bytes()
    assert b"session: window-1\n" in first
    later = protocol.make_ack("lane-b", datetime(2031, 1, 1, tzinfo=UTC))
    assert opened.publish_ack(PROJECT, note_id, later) is False
    assert path.read_bytes() == first
    assert protocol.parse_ack_bytes(first, file_reader="lane-b").session == "window-1"


def test_a_crashed_ack_does_not_block_a_later_ack(
    opened: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an ack killed between creating its .tmp and renaming it."""
    folder = opened.root / "acks" / PROJECT
    final = folder / f"{NOTE_ID}--lane-b.md"
    real_rename = store.rename_no_replace

    def crash(src: Path, dst: Path) -> None:
        raise Crash

    monkeypatch.setattr(store, "rename_no_replace", crash)
    with pytest.raises(Crash):
        opened.publish_ack(PROJECT, NOTE_ID, protocol.make_ack("lane-b", CREATED))
    leftovers = sorted(folder.iterdir())
    assert len(leftovers) == 1 and temp_name_pattern(final.name).fullmatch(leftovers[0].name)
    crashed_bytes = leftovers[0].read_bytes()
    hand_written = folder / f"{final.name}.tmp"  # the fixed name a hand writer would use
    hand_written.write_bytes(b"partial")

    monkeypatch.setattr(store, "rename_no_replace", real_rename)
    later = protocol.make_ack("lane-b", CREATED, "window-2")
    assert opened.publish_ack(PROJECT, NOTE_ID, later) is True
    assert protocol.parse_ack_bytes(final.read_bytes(), file_reader="lane-b") == later
    assert leftovers[0].read_bytes() == crashed_bytes  # never deleted, only reported
    assert hand_written.read_bytes() == b"partial"
    acked, diagnostics = opened.scan_acks(PROJECT)
    assert acked == {(NOTE_ID, "lane-b")}
    assert sorted(d.path.rsplit("/", 1)[1] for d in diagnostics) == sorted(
        [leftovers[0].name, hand_written.name]
    )
    assert all("leftover .tmp" in d.message for d in diagnostics)


def test_publish_ack_loses_race_gracefully(opened: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    note_id = "20300101T120000Z-lane-a-to-lane-b-1a2b"
    real_rename = store.rename_no_replace

    def racing_rename(src: Path, dst: Path) -> None:
        dst.write_bytes(b"reader: lane-b\nread_utc: 2030-01-01T12:00:00Z\nreceived\n")
        real_rename(src, dst)

    monkeypatch.setattr(store, "rename_no_replace", racing_rename)
    assert opened.publish_ack(PROJECT, note_id, protocol.make_ack("lane-b", CREATED)) is False
    assert sorted(p.name for p in (opened.root / "acks" / PROJECT).iterdir()) == [
        f"{note_id}--lane-b.md"
    ]


def test_an_unreadable_ack_file_still_acknowledges(
    opened: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scan_acks and publish_ack agree: a regular file at the ack's name acknowledges
    even when reading it fails, and the failure is a diagnostic."""
    place_ack(store_root, b"reader: lane-b\n", f"{NOTE_ID}--lane-b.md")

    def refuse(path: Path, limit: int) -> bytes:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(store, "_read_bounded", refuse)
    acked, diagnostics = opened.scan_acks(PROJECT)
    assert acked == {(NOTE_ID, "lane-b")}
    assert [d.message for d in diagnostics] == ["unreadable: Permission denied"]
    assert opened.publish_ack(PROJECT, NOTE_ID, protocol.make_ack("lane-b", CREATED)) is False


# --- Loading and scanning ---------------------------------------------------------------


def test_load_note_not_found_and_invalid(opened: Store, store_root: Path) -> None:
    missing = "20300101T120000Z-lane-a-to-lane-b-1a2b"
    with pytest.raises(NoteNotFoundError) as caught:
        opened.load_note(PROJECT, missing)
    assert caught.value.code == store.NOT_FOUND
    place_note(store_root, b"---\nid: broken\n---\n", f"{missing}.md")
    with pytest.raises(NoteNotFoundError) as caught:
        opened.load_note(PROJECT, missing)
    assert caught.value.code == store.NOTE_INVALID


def test_load_note_oversize(opened: Store, store_root: Path) -> None:
    name = "20300101T120000Z-lane-a-to-lane-b-1a2b"
    place_note(store_root, b"x" * (NOTE_MAX_BYTES + 1), f"{name}.md")
    with pytest.raises(NoteNotFoundError, match="oversize"):
        opened.load_note(PROJECT, name)


def test_scan_reports_every_kind_of_problem(opened: Store, store_root: Path) -> None:
    good = opened.publish_note(make_draft(), "Body.", now=fixed_now, hex_source=lambda: "0001")
    place_note(store_root, b"x" * (NOTE_MAX_BYTES + 1), "20300101T120000Z-lane-a-to-lane-b-0002.md")
    place_note(store_root, b"---\nnot a note\n---\n", "20300101T120000Z-lane-a-to-lane-b-0003.md")
    place_note(store_root, b"partial", "20300101T120000Z-lane-a-to-lane-b-0004.md.tmp")
    place_note(store_root, b"stray", "notes.txt")
    (store_root / "messages" / "pilot-note.md").write_bytes(b"flat")
    (store_root / "acks").mkdir()
    (store_root / "acks" / "pilot-ack.md").write_bytes(b"flat")
    place_ack(store_root, b"reader: lane-b\nreceived\n", f"{good.id}--lane-b.md")
    place_ack(store_root, b"x" * (ACK_MAX_BYTES + 1), f"{good.id}--lane-c.md")
    place_ack(store_root, b"partial", f"{good.id}--lane-d.md.tmp")
    place_ack(store_root, b"stray", "readme.md")

    notes, note_diagnostics = opened.scan_notes(PROJECT)
    acked, ack_diagnostics = opened.scan_acks(PROJECT)
    flat = opened.scan_flat()

    assert [n.id for n in notes] == [good.id]
    messages = {d.path.rsplit("/", 1)[1]: d.message for d in note_diagnostics}
    assert messages["20300101T120000Z-lane-a-to-lane-b-0002.md"].startswith("oversize")
    assert messages["20300101T120000Z-lane-a-to-lane-b-0003.md"].startswith("invalid")
    assert messages["20300101T120000Z-lane-a-to-lane-b-0004.md.tmp"] == (
        "leftover .tmp file; never read as a note"
    )
    assert messages["notes.txt"] == "ignored: not named <note id>.md"
    ack_messages = {d.path.rsplit("/", 1)[1]: d.message for d in ack_diagnostics}
    assert ack_messages[f"{good.id}--lane-b.md"].startswith("invalid")
    assert ack_messages[f"{good.id}--lane-c.md"].startswith("oversize")
    assert ack_messages[f"{good.id}--lane-d.md.tmp"] == "leftover .tmp file; never read as an ack"
    assert ack_messages["readme.md"] == "ignored: not named <note id>--<lane>.md"
    # An existing ack file means acknowledged, as the ack command treats it.
    assert acked == {(good.id, "lane-b"), (good.id, "lane-c")}
    assert sorted(d.path for d in flat) == ["acks/pilot-ack.md", "messages/pilot-note.md"]
    assert all("unmigrated" in d.message for d in flat)


def test_store_takes_file_name_shapes_from_protocol(
    store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One source: store.py imports NOTE_SUFFIX, ACK_NAME_SHAPE and CARD_NAME_SHAPE from
    protocol and never restates them.

    A fresh copy of store.py is loaded while protocol holds other values, built at run
    time so that each is a new object no other module holds. The copy must hold that
    very object (``is``), and must name files and word its diagnostics with the new
    values. A store.py that defined its own ".md" would hold a different value, so
    neither check can pass by Python sharing equal string constants.
    """
    suffix = protocol.NOTE_SUFFIX.replace("md", "note")
    shape = f"<note id>{protocol.ACK_SEPARATOR}<lane>{suffix}"
    card_shape = f"<lane>{suffix}"
    monkeypatch.setattr(protocol, "NOTE_SUFFIX", suffix)
    monkeypatch.setattr(protocol, "ACK_NAME_SHAPE", shape)
    monkeypatch.setattr(protocol, "CARD_NAME_SHAPE", card_shape)
    spec = importlib.util.spec_from_file_location("store_variant", store.__file__)
    assert spec is not None and spec.loader is not None
    variant = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, variant)  # dataclasses look it up
    spec.loader.exec_module(variant)

    assert variant.NOTE_SUFFIX is suffix
    opened = variant.Store.open()
    assert opened.note_path(PROJECT, NOTE_ID).name == f"{NOTE_ID}{suffix}"
    place_note(store_root, b"x", "stray.md")
    place_ack(store_root, b"x", "stray.md")
    place_card(store_root, b"x", "stray.txt")
    _, note_diagnostics = opened.scan_notes(PROJECT)
    _, ack_diagnostics = opened.scan_acks(PROJECT)
    _, card_diagnostics = opened.scan_cards(PROJECT)
    assert [d.message for d in note_diagnostics] == [f"ignored: not named <note id>{suffix}"]
    assert [d.message for d in ack_diagnostics] == [f"ignored: not named {shape}"]
    assert [d.message for d in card_diagnostics] == [f"ignored: not named {card_shape}"]


def test_reading_never_writes(opened: Store, store_root: Path) -> None:
    note = opened.publish_note(make_draft(), "Body.", now=fixed_now, hex_source=lambda: "0001")
    before = snapshot(store_root)
    opened.inbox(PROJECT, "lane-b", include_acked=True, include_superseded=True)
    opened.load_note(PROJECT, note.id)
    opened.scan_flat()
    assert snapshot(store_root) == before
    assert not (store_root / "acks").exists()


def test_inbox_on_a_missing_store_creates_nothing(opened: Store, store_root: Path) -> None:
    view = opened.inbox(PROJECT, "lane-b")
    assert view.entries == () and view.diagnostics == ()
    assert not store_root.exists()


def test_find_idempotent_matches_sender_and_key(opened: Store) -> None:
    keyed = make_draft(idempotency_key="key-1")
    first = opened.publish_note(keyed, "Body.", now=fixed_now, hex_source=lambda: "0001")
    assert opened.find_idempotent(PROJECT, ("example-project", "lane-a"), "key-1") == first
    assert opened.find_idempotent(PROJECT, ("example-project", "lane-c"), "key-1") is None
    assert opened.find_idempotent(PROJECT, ("other-project", "lane-a"), "key-1") is None
    assert opened.find_idempotent(PROJECT, ("example-project", "lane-a"), "key-2") is None
