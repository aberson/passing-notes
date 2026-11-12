"""Lane cards: the format (protocol.py), replace-publication and scanning (store.py),
and ``card set`` / ``card show`` through the production entry points.

Every card-specific test lives here, one section per layer. Elsewhere cards appear only
in the docs/protocol.md parity checks (test_protocol_doc.py) and as rows of checks that
hold for every command or file kind: traversal, help, closed output, the repository
and the store's file name shapes."""

from __future__ import annotations

import dataclasses
import errno
import io
import json
import os
import re
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import pytest
from conftest import (
    CARD_FULL,
    CARD_MINIMAL,
    ESC,
    LINE_SEPARATOR,
    LINK_KINDS,
    LONE_SURROGATE,
    PROJECT,
    RLO,
    TAB,
    ZWSP,
    Crash,
    Invoke,
    MakeLink,
    Result,
    StoreCall,
    body_lines,
    card_set_args,
    lf_file,
    module_command,
    place_card,
    race,
    run_cli,
    run_together,
    snapshot,
    unsafe_in,
)

from passing_notes import protocol, store
from passing_notes.cli import BODY_UNREADABLE, CARD_BODY_BEGIN, CARD_BODY_END
from passing_notes.protocol import (
    CARD_BRANCH_MAX_CHARS,
    CARD_ITEM_MAX_CHARS,
    CARD_LINE_MAX_CHARS,
    CARD_LIST_MAX_ITEMS,
    CARD_MAX_BYTES,
    CARD_NEXT_CHECKPOINT_MAX_CHARS,
    DIAGNOSTIC_LINES,
    OversizeError,
    ProtocolError,
)
from passing_notes.store import (
    CARD_INVALID,
    NOT_FOUND,
    PUBLISH_FAILED,
    READ_BUSY_RETRIES,
    REPLACE_BUSY_RETRIES,
    STORE_LINK,
    STORE_UNUSABLE,
    TMP_HEX_CHARS,
    CardNotFoundError,
    Store,
    StoreError,
)

UPDATED = datetime(2030, 1, 1, 12, 30, 0, tzinfo=UTC)  # the time in CARD_FULL
FULL_TEXT = CARD_FULL.decode("utf-8")


def full_card(**overrides: object) -> protocol.Card:
    """The card CARD_FULL holds, built by make_card, with ``overrides`` applied."""
    fields: dict[str, object] = {
        "project": PROJECT,
        "lane": "lane-b",
        "host": "codex",
        "item": "Step 2 tests",
        "state": "active",
        "updated": UPDATED,
        "window": "example-window-1",
        "checkout": "C:/work/example-project",
        "branch": "step-2",
        "owns": ("src/example/", "docs/example.md"),
        "holds": ("engine lock",),
        "next_checkpoint": "after the unit suite passes",
        "body": "Synthetic card notes.\nSecond line.",
    }
    fields.update(overrides)
    return protocol.make_card(**fields)  # type: ignore[arg-type]


def card_folder(root: Path) -> Path:
    return root / "cards" / PROJECT


# --- Format -------------------------------------------------------------------------------


def test_make_card_renders_every_field_in_protocol_order() -> None:
    card = full_card()
    assert protocol.render_card_bytes(card) == CARD_FULL
    assert protocol.parse_card_bytes(CARD_FULL, file_stem="lane-b", folder_project=PROJECT) == card


def test_a_minimal_card_omits_absent_fields_and_writes_empty_lists() -> None:
    card = protocol.make_card(
        project=PROJECT,
        lane="lane-a",
        host="claude",
        item="Synthetic item",
        state="idle",
        updated=UPDATED,
    )
    assert protocol.render_card_bytes(card) == CARD_MINIMAL
    assert (card.owns, card.holds, card.body) == ((), (), "")
    assert protocol.parse_card_bytes(CARD_MINIMAL) == card


def test_reader_accepts_any_order_unknown_keys_and_crlf() -> None:
    moved = FULL_TEXT.replace("updated_utc: 2030-01-01T12:30:00Z\n", "")
    unknown = "updated_utc: 2030-01-01T12:30:00Z\nx-future: 1\n  - future item\n    more\n"
    text = moved.replace("---\nlane:", f"---\n{unknown}lane:", 1).replace("\n", "\r\n")
    assert protocol.parse_card(text, file_stem="lane-b", folder_project=PROJECT) == full_card()


_FIELD_RULES = [
    ({"lane": "../lane-b"}, "lane"),
    ({"project": "Example"}, "project"),
    ({"host": "copilot"}, "host"),
    ({"window": "has space"}, "window"),
    ({"checkout": "c" * (CARD_LINE_MAX_CHARS + 1)}, "checkout"),
    ({"branch": "b" * (CARD_BRANCH_MAX_CHARS + 1)}, "branch"),
    ({"owns": ["o"] * (CARD_LIST_MAX_ITEMS + 1)}, "owns"),
    ({"holds": ["h" * (CARD_LINE_MAX_CHARS + 1)]}, "holds entry 1"),
    ({"item": "i" * (CARD_ITEM_MAX_CHARS + 1)}, "item"),
    ({"state": "paused"}, "state"),
    ({"next_checkpoint": "n" * (CARD_NEXT_CHECKPOINT_MAX_CHARS + 1)}, "next_checkpoint"),
]


@pytest.mark.parametrize(
    ("overrides", "field"), _FIELD_RULES, ids=[field for _, field in _FIELD_RULES]
)
def test_make_card_checks_every_field_by_its_own_rule(
    overrides: dict[str, object], field: str
) -> None:
    """One value per field that breaks its rule: the error names that field."""
    with pytest.raises(ProtocolError, match=f"^{re.escape(field)}: "):
        full_card(**overrides)


def test_every_card_limit_admits_its_maximum() -> None:
    """The bounds are inclusive: each one-line field at its limit, and a full list."""
    card = full_card(
        checkout="c" * CARD_LINE_MAX_CHARS,
        branch="b" * CARD_BRANCH_MAX_CHARS,
        item="i" * CARD_ITEM_MAX_CHARS,
        next_checkpoint="n" * CARD_NEXT_CHECKPOINT_MAX_CHARS,
        owns=[f"path-{n}" for n in range(CARD_LIST_MAX_ITEMS)],
        holds=["h" * CARD_LINE_MAX_CHARS],
    )
    assert len(card.owns) == CARD_LIST_MAX_ITEMS


_HIDING = [
    ({"item": "Step 2\nstate: done"}, "item"),  # would smuggle in a header line
    ({"checkout": f"C:/work/{RLO}x"}, "checkout"),
    ({"branch": f"step{TAB}2"}, "branch"),
    ({"next_checkpoint": f"after{ZWSP}tests"}, "next_checkpoint"),
    ({"owns": [f"src/{LONE_SURROGATE}"]}, "owns entry 1"),
    ({"holds": [f"lock{LINE_SEPARATOR}x"]}, "holds entry 1"),
]


@pytest.mark.parametrize(("overrides", "field"), _HIDING, ids=[field for _, field in _HIDING])
def test_one_line_card_values_refuse_what_can_hide_or_break_text(
    overrides: dict[str, object], field: str
) -> None:
    """Every one-line card value refuses a line break, control, format, surrogate or
    separator character: none can add a header line or hide what is printed."""
    with pytest.raises(ProtocolError, match=f"^{re.escape(field)}: character"):
        full_card(**overrides)


def test_the_size_limit_binds_the_body_the_rendered_card_and_a_card_read() -> None:
    body_limit = f"body: larger than the card limit of {CARD_MAX_BYTES} bytes"
    with pytest.raises(ProtocolError, match=body_limit):
        full_card(body="x" * (CARD_MAX_BYTES + 1))
    lists_at_their_limits = ["x" * CARD_LINE_MAX_CHARS] * CARD_LIST_MAX_ITEMS
    with pytest.raises(ProtocolError, match=rf"after rendering \(limit {CARD_MAX_BYTES}\)"):
        full_card(owns=lists_at_their_limits, holds=lists_at_their_limits)
    at_limit = CARD_FULL + b"x" * (CARD_MAX_BYTES - len(CARD_FULL))
    assert protocol.parse_card_bytes(at_limit).lane == "lane-b"
    with pytest.raises(OversizeError):
        protocol.parse_card_bytes(at_limit + b"x")


INVALID_CARDS = {
    "missing field": (FULL_TEXT.replace("state: active\n", ""), "missing required field(s): state"),
    "missing list": (
        FULL_TEXT.replace("holds:\n  - engine lock\n", ""),
        "missing required field(s): holds",
    ),
    "duplicate field": (
        FULL_TEXT.replace("host: codex\n", "host: codex\nhost: claude\n"),
        "duplicate field host",
    ),
    "list item outside a list": (
        FULL_TEXT.replace("host: codex\n", "host: codex\n  - stray\n"),
        "list item outside owns or holds",
    ),
    "item on the key line": (
        FULL_TEXT.replace("owns:\n", "owns: src/\n"),
        "owns: items go on the following",
    ),
    "unknown state": (FULL_TEXT.replace("state: active", "state: paused"), "state: 'paused'"),
    "bad time": (FULL_TEXT.replace("12:30:00Z", "12:30:00"), "updated_utc: '2030-01-01T12:30:00'"),
    "hiding character": (
        FULL_TEXT.replace("item: Step 2", f"item: Step{ZWSP} 2"),
        "item: character 5 ('\\u200b')",
    ),
    "no closing line": (FULL_TEXT.replace("---\n\nSynthetic", "\nSynthetic"), "no closing"),
}


@pytest.mark.parametrize(("text", "reason"), INVALID_CARDS.values(), ids=INVALID_CARDS.keys())
def test_invalid_card_files(text: str, reason: str) -> None:
    with pytest.raises(ProtocolError, match=re.escape(reason)):
        protocol.parse_card(text)


def test_a_card_matches_its_file_and_folder_and_is_utf8() -> None:
    with pytest.raises(ProtocolError, match="lane: 'lane-b' does not equal the file name stem"):
        protocol.parse_card_bytes(CARD_FULL, file_stem="lane-a")
    with pytest.raises(ProtocolError, match="project: 'example-project' does not match"):
        protocol.parse_card_bytes(CARD_FULL, folder_project="other-project")
    with pytest.raises(ProtocolError, match="BOM"):
        protocol.parse_card_bytes(protocol.UTF8_BOM + CARD_FULL)
    with pytest.raises(ProtocolError, match="not valid UTF-8"):
        protocol.parse_card_bytes(CARD_FULL.replace(b"Step 2", b"Step \xff"))


def test_card_file_names() -> None:
    assert protocol.card_file_name("lane-b") == "lane-b.md"
    assert protocol.parse_card_file_name("lane-b.md") == "lane-b"
    with pytest.raises(ProtocolError):
        protocol.card_file_name("../lane-b")
    for name in ("lane-b.txt", "all.md", "Lane-B.md", "nul.md", ".md"):
        with pytest.raises(ProtocolError):
            protocol.parse_card_file_name(name)


# --- Store: replace-publication -------------------------------------------------------------


def test_publish_card_replaces_the_card_and_leaves_no_other_file(
    opened: Store, store_root: Path
) -> None:
    opened.publish_card(full_card())
    folder = card_folder(store_root)
    assert (folder / "lane-b.md").read_bytes() == CARD_FULL
    second = full_card(item="Step 3 tests", state="waiting", owns=(), body="")
    opened.publish_card(second)
    assert sorted(p.name for p in folder.iterdir()) == ["lane-b.md"]
    assert opened.load_card(PROJECT, "lane-b") == second


def test_publish_card_writes_an_exclusive_temporary_file_then_replaces_once(
    opened: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A temporary name that already exists is never written to; a fresh one is drawn.
    The card changes only in the one replace, from the complete old card to the new."""
    opened.publish_card(full_card())
    folder = card_folder(store_root)
    taken = folder / "lane-b.md.0000aaaa.tmp"
    taken.write_bytes(b"another writer's file")
    hexes = iter(["0000aaaa", "0000bbbb"])
    monkeypatch.setattr(store, "default_tmp_hex", lambda: next(hexes))
    replaced: list[tuple[str, bytes, bytes]] = []
    real_replace = store.replace_file

    def watched(src: Path, dst: Path) -> None:
        replaced.append((src.name, src.read_bytes(), dst.read_bytes()))
        real_replace(src, dst)

    monkeypatch.setattr(store, "replace_file", watched)
    new = full_card(item="Step 3 tests")
    opened.publish_card(new)
    new_bytes = protocol.render_card_bytes(new)
    assert replaced == [("lane-b.md.0000bbbb.tmp", new_bytes, CARD_FULL)]
    assert taken.read_bytes() == b"another writer's file"
    assert sorted(p.name for p in folder.iterdir()) == ["lane-b.md", taken.name]
    assert (folder / "lane-b.md").read_bytes() == new_bytes


def test_publish_card_gives_up_when_every_temporary_name_is_taken(
    opened: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened.publish_card(full_card())
    monkeypatch.setattr(store, "default_tmp_hex", lambda: "0000aaaa")
    (card_folder(store_root) / "lane-b.md.0000aaaa.tmp").write_bytes(b"taken")
    before = snapshot(store_root)
    with pytest.raises(StoreError) as caught:
        opened.publish_card(full_card(item="Step 3 tests"))
    assert caught.value.code == PUBLISH_FAILED
    assert snapshot(store_root) == before


def test_an_interrupted_card_set_leaves_the_previous_card_whole(
    opened: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A set killed before its replace leaves the old card complete and a leftover .tmp,
    which scanning reports and never reads, and which never blocks the next set."""
    opened.publish_card(full_card())
    real_replace = store.replace_file

    def killed(src: Path, dst: Path) -> None:
        raise Crash

    monkeypatch.setattr(store, "replace_file", killed)
    with pytest.raises(Crash):
        opened.publish_card(full_card(item="Step 3 tests"))
    assert (card_folder(store_root) / "lane-b.md").read_bytes() == CARD_FULL
    cards, diagnostics = opened.scan_cards(PROJECT)
    assert [card.item for card in cards] == ["Step 2 tests"]
    assert [d.message for d in diagnostics] == ["leftover .tmp file; never read as a card"]
    monkeypatch.setattr(store, "replace_file", real_replace)
    opened.publish_card(full_card(item="Step 3 tests"))
    assert opened.load_card(PROJECT, "lane-b").item == "Step 3 tests"


def test_a_failed_replace_is_a_store_error_and_keeps_the_previous_card(
    opened: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened.publish_card(full_card())

    def refused(src: Path, dst: Path) -> None:
        raise PermissionError(errno.EACCES, "Access is denied")

    monkeypatch.setattr(store, "replace_file", refused)
    with pytest.raises(StoreError) as caught:
        opened.publish_card(full_card(item="Step 3 tests"))
    assert caught.value.code == STORE_UNUSABLE
    assert "cannot replace" in caught.value.message
    folder = card_folder(store_root)
    assert sorted(p.name for p in folder.iterdir()) == ["lane-b.md"]  # its .tmp is removed
    assert (folder / "lane-b.md").read_bytes() == CARD_FULL


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to replace an open file")
def test_a_reader_holding_the_card_delays_a_set_but_never_fails_it(
    opened: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows refuses to replace a file that another handle has open, such as a card
    show reading it. The replace is retried until the reader lets go."""
    opened.publish_card(full_card())
    attempts: list[str] = []
    real_replace = os.replace

    def counted(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        attempts.append(os.fspath(dst))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", counted)
    with open(card_folder(store_root) / "lane-b.md", "rb") as reader:
        letting_go = threading.Timer(0.2, reader.close)
        letting_go.start()
        opened.publish_card(full_card(item="Step 3 tests"))
        letting_go.join()
    assert len(attempts) > 1  # the first attempt met the open reader
    assert opened.load_card(PROJECT, "lane-b").item == "Step 3 tests"


# A replace and a read are tried this often before a busy (refused) attempt is reported.
# Only Windows refuses to replace an open file, or to open a file in the instant it is
# replaced; elsewhere one try is final.
REPLACE_TRIES = REPLACE_BUSY_RETRIES if os.name == "nt" else 1
READ_TRIES = READ_BUSY_RETRIES if os.name == "nt" else 1


@pytest.mark.parametrize("refusals", [REPLACE_TRIES - 1, REPLACE_TRIES])
def test_a_busy_card_replace_is_retried_up_to_its_bound(
    opened: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch, refusals: int
) -> None:
    """A set whose replace is refused as busy tries REPLACE_TRIES times, waiting between
    tries. Only a replace refused every time fails the set, as a store error that keeps
    the previous card whole and removes the set's temporary file."""
    opened.publish_card(full_card())
    real_replace = os.replace
    attempts: list[str] = []
    waits: list[float] = []

    def busy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        attempts.append(Path(dst).name)
        if len(attempts) <= refusals:
            raise PermissionError(errno.EACCES, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", busy)
    monkeypatch.setattr(time, "sleep", waits.append)
    new = full_card(item="Step 3 tests")
    if refusals < REPLACE_TRIES:
        opened.publish_card(new)
        expected = protocol.render_card_bytes(new)
    else:
        with pytest.raises(StoreError) as caught:
            opened.publish_card(new)
        assert caught.value.code == STORE_UNUSABLE
        assert caught.value.message.endswith("lane-b.md: Access is denied")
        expected = CARD_FULL
    assert attempts == ["lane-b.md"] * REPLACE_TRIES
    assert len(waits) == REPLACE_TRIES - 1
    assert snapshot(store_root) == {f"cards/{PROJECT}/lane-b.md": expected}


@pytest.mark.parametrize("refusals", [READ_TRIES - 1, READ_TRIES])
def test_a_busy_card_read_is_retried_up_to_its_bound(
    opened: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch, refusals: int
) -> None:
    """Opening a card while a set replaces it is refused for a moment. Load and scan
    try READ_TRIES times; only a card refused every time is a store error (load) or an
    unreadable diagnostic (scan), never a card that a later try reads."""
    opened.publish_card(full_card())
    real_open = open
    attempts: list[str] = []
    waits: list[float] = []

    def busy(file: Path, mode: str) -> IO[Any]:
        attempts.append(Path(file).name)
        if len(attempts) <= refusals:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(file, mode)

    monkeypatch.setattr(store, "open", busy, raising=False)
    monkeypatch.setattr(time, "sleep", waits.append)
    readable = refusals < READ_TRIES
    if readable:
        assert opened.load_card(PROJECT, "lane-b").item == "Step 2 tests"
    else:
        with pytest.raises(StoreError) as caught:
            opened.load_card(PROJECT, "lane-b")
        assert caught.value.code == STORE_UNUSABLE
        assert caught.value.message.endswith("lane-b.md: Permission denied")
    assert attempts == ["lane-b.md"] * READ_TRIES
    assert len(waits) == READ_TRIES - 1

    attempts.clear()
    cards, diagnostics = opened.scan_cards(PROJECT)
    assert attempts == ["lane-b.md"] * READ_TRIES
    if readable:
        assert ([card.item for card in cards], diagnostics) == (["Step 2 tests"], [])
    else:
        assert cards == []
        assert [str(d) for d in diagnostics] == [
            f"cards/{PROJECT}/lane-b.md: unreadable: Permission denied"
        ]
    assert snapshot(store_root) == {f"cards/{PROJECT}/lane-b.md": CARD_FULL}


def test_something_other_than_a_file_at_the_card_name(opened: Store, store_root: Path) -> None:
    """Only a regular file is a card. Set refuses to replace a folder there, and show
    reports it as invalid; nothing is written."""
    folder = card_folder(store_root)
    (folder / "lane-b.md").mkdir(parents=True)
    with pytest.raises(StoreError) as caught:
        opened.publish_card(full_card())
    assert caught.value.code == STORE_UNUSABLE
    assert "not a regular file" in caught.value.message
    with pytest.raises(CardNotFoundError) as invalid:
        opened.load_card(PROJECT, "lane-b")
    assert invalid.value.code == CARD_INVALID
    _, diagnostics = opened.scan_cards(PROJECT)
    assert [d.message for d in diagnostics] == ["ignored: not a regular file"]
    assert [p.name for p in folder.iterdir()] == ["lane-b.md"]
    assert snapshot(store_root) == {}


_CARD_CALLS: list[StoreCall] = [
    lambda s: s.scan_cards(PROJECT),
    lambda s: s.load_card(PROJECT, "lane-b"),
    lambda s: s.publish_card(full_card()),
]


@pytest.mark.parametrize("kind", LINK_KINDS)
@pytest.mark.parametrize("level", ["cards", f"cards/{PROJECT}"])
def test_card_folder_links_are_refused(
    store_root: Path, tmp_path: Path, dir_link: MakeLink, kind: str, level: str
) -> None:
    """Nothing is read or written through a linked cards folder or project folder."""
    target = tmp_path / "elsewhere"  # outside any Git tree: only the link check applies
    target.mkdir()
    link = store_root / level
    link.parent.mkdir(parents=True, exist_ok=True)
    dir_link(link, target, kind)
    opened = Store.open()
    for call in _CARD_CALLS:
        with pytest.raises(StoreError) as caught:
            call(opened)
        assert caught.value.code == STORE_LINK
    assert list(target.iterdir()) == []


# --- Store: scanning and loading -------------------------------------------------------------


def test_scan_cards_lists_valid_cards_and_reports_every_other_entry(
    opened: Store, store_root: Path
) -> None:
    place_card(store_root, CARD_MINIMAL, "lane-a.md")
    place_card(store_root, CARD_FULL, "lane-b.md")
    place_card(store_root, b"partial", "lane-c.md.0123abcd.tmp")
    place_card(store_root, b"x", "notes.txt")
    place_card(store_root, b"x", "all.md")
    place_card(store_root, b"x" * (CARD_MAX_BYTES + 1), "lane-d.md")
    place_card(store_root, b"---\nnot a card\n---\n", "lane-e.md")
    place_card(store_root, CARD_FULL, "lane-f.md")  # the card inside names lane-b
    (card_folder(store_root) / "lane-g.md").mkdir()

    cards, diagnostics = opened.scan_cards(PROJECT)

    assert [card.lane for card in cards] == ["lane-a", "lane-b"]
    assert all(d.path.startswith(f"cards/{PROJECT}/") for d in diagnostics)
    assert {d.path.rsplit("/", 1)[1]: d.message for d in diagnostics} == {
        "lane-c.md.0123abcd.tmp": "leftover .tmp file; never read as a card",
        "notes.txt": "ignored: not named <lane>.md",
        "all.md": "ignored: not named <lane>.md",
        "lane-d.md": f"oversize: {CARD_MAX_BYTES + 1} bytes (limit {CARD_MAX_BYTES})",
        "lane-e.md": "invalid: header line 2: not a 'key: value' line",
        "lane-f.md": "invalid: lane: 'lane-b' does not equal the file name stem",
        "lane-g.md": "ignored: not a regular file",
    }
    place_card(store_root, CARD_FULL, "lane-b.md", project="other-project")
    _, elsewhere = opened.scan_cards("other-project")
    assert [d.message for d in elsewhere] == [
        "invalid: project: 'example-project' does not match its folder"
    ]


def test_reading_cards_never_writes(opened: Store, store_root: Path) -> None:
    assert opened.scan_cards(PROJECT) == ([], [])
    with pytest.raises(CardNotFoundError) as caught:
        opened.load_card(PROJECT, "lane-b")
    assert caught.value.code == NOT_FOUND
    assert not store_root.exists()
    opened.publish_card(full_card())
    before = snapshot(store_root)
    opened.scan_cards(PROJECT)
    opened.load_card(PROJECT, "lane-b")
    assert snapshot(store_root) == before


# --- CLI ---------------------------------------------------------------------------------------


def test_card_set_and_show_through_the_console_script(store_root: Path, tmp_path: Path) -> None:
    """The console script and ``python -m`` set two cards; card show lists them and
    prints one. Every card set flag reaches its own field."""
    body = tmp_path / "card-body.md"
    body.write_bytes(b"Synthetic card notes.\r\nSecond line.\r\n")
    every_flag = [
        *("--window", "example-window-1", "--checkout", "C:/work/example-project"),
        *("--branch", "step-2", "--own", "src/example/", "--own", "docs/example.md"),
        *("--hold", "engine lock", "--next", "after the unit suite passes"),
        *("--body-file", str(body)),
    ]
    full_args = card_set_args("lane-b", "Step 2 tests", *every_flag, host="codex")
    full = run_cli(full_args, root=store_root)
    assert full.returncode == 0, full.stderr
    minimal = run_cli(
        card_set_args("lane-a", "Synthetic item", state="idle"),
        root=store_root,
        command=module_command(),
    )
    assert minimal.returncode == 0, minimal.stderr
    folder = card_folder(store_root)
    assert sorted(p.name for p in folder.iterdir()) == ["lane-a.md", "lane-b.md"]
    card_b = protocol.parse_card_bytes(
        (folder / "lane-b.md").read_bytes(), file_stem="lane-b", folder_project=PROJECT
    )
    assert card_b == dataclasses.replace(full_card(), updated_utc=card_b.updated_utc)
    assert full.stdout == f"card set: {PROJECT}/lane-b at {card_b.updated_utc}\n"
    card_a = protocol.parse_card_bytes((folder / "lane-a.md").read_bytes())

    listed = run_cli(["card", "show", "--project", PROJECT], root=store_root)
    assert listed.returncode == 0, listed.stderr
    assert listed.lines == [
        TAB.join(["lane-a", "claude", "idle", "Synthetic item", card_a.updated_utc]),
        TAB.join(["lane-b", "codex", "active", "Step 2 tests", card_b.updated_utc]),
        "diagnostics: 0",
    ]
    shown = run_cli(["card", "show", "--project", PROJECT, "--lane", "lane-b"], root=store_root)
    assert shown.returncode == 0, shown.stderr
    header = shown.lines[: shown.lines.index(CARD_BODY_BEGIN)]
    assert header == protocol.render_card_header_lines(card_b)
    shown_body = body_lines(shown, CARD_BODY_BEGIN, CARD_BODY_END)
    assert shown_body == ["Synthetic card notes.", "Second line."]
    assert shown.lines[-1] == CARD_BODY_END


def test_two_processes_setting_the_same_lane_leave_one_complete_card(
    store_root: Path, tmp_path: Path
) -> None:
    """Both processes write their temporary file, then replace the card at the same
    moment. Both succeed, no temporary file is left, and the card is exactly one of the
    two: complete and valid, with that writer's item, host and body throughout."""
    expected: dict[str, tuple[str, str]] = {}
    runs: list[tuple[str, list[str]]] = []
    for n, host in ((1, "claude"), (2, "codex")):
        body = f"Racer {n} notes, line after line.\n" * 120  # a few KiB: a mix would show
        path = tmp_path / f"body-{n}.md"
        path.write_bytes(body.encode("utf-8"))
        expected[f"Racer {n}"] = (host, body.strip("\n"))
        args = card_set_args("lane-b", f"Racer {n}", "--body-file", str(path), host=host)
        runs.append(("-", args))  # card set draws no note hex
    results = race(store_root, runs, held="replace_file")
    assert [r.returncode for r in results] == [0, 0], [r.stderr for r in results]
    folder = card_folder(store_root)
    assert sorted(p.name for p in folder.iterdir()) == ["lane-b.md"]
    card = protocol.parse_card_bytes(
        (folder / "lane-b.md").read_bytes(), file_stem="lane-b", folder_project=PROJECT
    )
    assert (card.host, card.body) == expected[card.item]


# A child process that calls the production ``cli.main`` with its arguments again and
# again for ``seconds``, starting once every child has started. It counts the refused
# opens of each call (on Windows a file being replaced refuses an open with
# PermissionError) and prints one JSON record per call: exit code, output, refusals.
LOOPER = """
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

from passing_notes import cli, store

barrier, children, seconds, *argv = sys.argv[1:]
real_open = open
refused = [0]


def counting_open(file, mode="r", *args, **kwargs):
    try:
        return real_open(file, mode, *args, **kwargs)
    except PermissionError:
        refused[0] += 1
        raise


store.open = counting_open
Path(barrier, str(os.getpid())).touch()
deadline = time.monotonic() + 30
while len(list(Path(barrier).iterdir())) < int(children) and time.monotonic() < deadline:
    time.sleep(0.01)
records = []
stop = time.monotonic() + float(seconds)
while time.monotonic() < stop:
    refused[0] = 0
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    records.append([code, out.getvalue(), err.getvalue(), refused[0]])
print(json.dumps(records))
"""
Looped = tuple[Result, int]  # one call: its result and its refused opens


def loop_cli(store_root: Path, runs: list[list[str]], seconds: float) -> list[list[Looped]]:
    """Run one LOOPER per argument list at once; each one's calls, in order."""
    argv = [[str(seconds), *args] for args in runs]
    children = run_together(store_root, LOOPER, argv, "its loop")
    looped = []
    for child in children:
        assert child.returncode == 0, child.stderr
        looped.append([(Result(c, o, e), n) for c, o, e, n in json.loads(child.stdout)])
    return looped


def shown_card(result: Result) -> protocol.Card:
    """The card ``card show --lane`` printed, parsed back with full validation."""
    assert result.lines[-1] == CARD_BODY_END
    header = result.lines[: result.lines.index(CARD_BODY_BEGIN)]
    body = body_lines(result, CARD_BODY_BEGIN, CARD_BODY_END)
    data = lf_file("---", *header, "---", "", *body)
    return protocol.parse_card_bytes(data, file_stem="lane-b", folder_project=PROJECT)


def test_card_show_reads_whole_cards_while_other_processes_replace_them(
    store_root: Path, tmp_path: Path, invoke: Invoke
) -> None:
    """Three processes set one lane's card again and again while three others show it,
    all through the production ``cli.main``. Every show prints one writer's complete,
    valid card. A read that meets a replace is retried, so it can fail, with a clear
    error, only after READ_TRIES refused opens. Every set succeeds."""
    expected: dict[str, tuple[str, str]] = {}
    writers: list[list[str]] = []
    for n, host in ((1, "claude"), (2, "codex"), (3, "claude")):
        body = f"Writer {n} notes, line after line.\n" * 30
        path = tmp_path / f"body-{n}.md"
        path.write_bytes(body.encode("utf-8"))
        expected[f"Writer {n}"] = (host, body.strip("\n"))
        writers.append(card_set_args("lane-b", f"Writer {n}", "--body-file", str(path), host=host))
    assert invoke(*writers[0]).returncode == 0  # the readers start with a card
    listing = ["card", "show", "--project", PROJECT]
    one = [*listing, "--lane", "lane-b"]
    *sets, shown_1, shown_2, listed = loop_cli(store_root, [*writers, one, one, listing], 1.5)

    for calls in sets:
        failed = [result.stderr for result, _ in calls if result.returncode != 0]
        assert calls and not failed, failed
    card_path = card_folder(store_root) / "lane-b.md"
    card_rel = f"cards/{PROJECT}/lane-b.md"
    refused_error = f"error: {STORE_UNUSABLE}: cannot read {card_path}: Permission denied"
    unreadable = "unreadable: Permission denied"
    seen: set[str] = set()
    for result, refused in [*shown_1, *shown_2]:
        if result.returncode == 0:
            card = shown_card(result)
            assert (card.host, card.body) == expected[card.item]
            seen.add(card.item)
        else:
            assert (result.returncode, refused) == (4, READ_TRIES), result.stderr
            assert refused_error in result.stderr

    in_flight = re.compile(  # a writer between writing its temporary file and replacing
        rf"diagnostic: {re.escape(card_rel)}\.[0-9a-f]{{{TMP_HEX_CHARS}}}\.tmp: "
        r"leftover \.tmp file; never read as a card"
    )
    for result, refused in listed:
        assert result.returncode == 0, result.stderr
        *lines, total = result.lines
        rows = [line for line in lines if not line.startswith("diagnostic: ")]
        others = [line for line in lines[len(rows) :] if not in_flight.fullmatch(line)]
        assert total == f"diagnostics: {len(lines) - len(rows)}"
        if rows:
            [(lane, host, state, item, _)] = [row.split(TAB) for row in rows]
            assert (lane, host, state, others) == ("lane-b", expected[item][0], "active", [])
            seen.add(item)
        else:
            assert (refused, others) == (READ_TRIES, [f"diagnostic: {card_rel}: {unreadable}"])
    assert len(seen) > 1  # the shows ran while the sets replaced the card


_REFUSALS = {
    "missing --item": ([a for a in card_set_args() if a not in ("--item", "Synthetic item")], None),
    "unknown --host": (card_set_args(host="copilot"), None),
    "unknown --state": (card_set_args(state="paused"), None),
    "field rule": (card_set_args("lane-b", "i" * (CARD_ITEM_MAX_CHARS + 1)), None),
    "oversize body": ([*card_set_args(), "--body-stdin"], b"x" * (CARD_MAX_BYTES + 1)),
    "body not UTF-8": ([*card_set_args(), "--body-stdin"], b"\xff\xfe not utf-8"),
    "card over the limit": (
        card_set_args(
            "lane-b",
            "Synthetic item",
            *[arg for _ in range(CARD_LIST_MAX_ITEMS) for arg in ("--own", "x" * 300)],
            *[arg for _ in range(CARD_LIST_MAX_ITEMS) for arg in ("--hold", "x" * 300)],
        ),
        None,
    ),
}
_REFUSAL_REASONS = {
    "missing --item": "the following arguments are required: --item",
    "unknown --host": "argument --host: invalid choice: 'copilot'",
    "unknown --state": "argument --state: invalid choice: 'paused'",
    "field rule": f"error: invalid-argument: item: at most {CARD_ITEM_MAX_CHARS} characters",
    "oversize body": f"error: invalid-argument: --body-stdin: larger than {CARD_MAX_BYTES} bytes",
    "body not UTF-8": "error: invalid-argument: --body-stdin: not valid UTF-8",
    "card over the limit": "error: invalid-argument: card: ",
}


@pytest.mark.parametrize("case", list(_REFUSALS), ids=list(_REFUSALS))
def test_card_set_refusals_exit_2_and_leave_the_card_unchanged(
    invoke: Invoke, store_root: Path, case: str
) -> None:
    assert invoke(*card_set_args()).returncode == 0
    before = snapshot(store_root)
    args, stdin = _REFUSALS[case]
    result = invoke(*args, stdin=stdin)
    assert result.returncode == 2, result.stdout
    assert _REFUSAL_REASONS[case] in result.stderr
    assert snapshot(store_root) == before


# card set arguments that each break one field's rule, with the field the error names.
# Host and state are argparse choices, refused before any command runs.
_BAD_FIELDS = {
    "--project": (card_set_args(project="Example"), "project"),
    "--lane": (card_set_args("../lane-b"), "lane"),
    "--item": (card_set_args("lane-b", "i" * (CARD_ITEM_MAX_CHARS + 1)), "item"),
    "--window": (card_set_args("lane-b", "Synthetic item", "--window", "has space"), "window"),
    "--checkout": (card_set_args("lane-b", "Synthetic item", "--checkout", f"{RLO}x"), "checkout"),
    "--branch": (
        card_set_args("lane-b", "Synthetic item", "--branch", "b" * (CARD_BRANCH_MAX_CHARS + 1)),
        "branch",
    ),
    "--own": (
        card_set_args("lane-b", "Synthetic item", "--own", "o" * (CARD_LINE_MAX_CHARS + 1)),
        "owns entry 1",
    ),
    "--hold": (card_set_args("lane-b", "Synthetic item", "--hold", f"lock{ZWSP}"), "holds entry 1"),
    "--next": (
        card_set_args(
            "lane-b", "Synthetic item", "--next", "n" * (CARD_NEXT_CHECKPOINT_MAX_CHARS + 1)
        ),
        "next_checkpoint",
    ),
    "card over the limit": (_REFUSALS["card over the limit"][0], "card"),
}


@pytest.mark.parametrize("source", ["--body-stdin", "--body-file"])
@pytest.mark.parametrize("case", list(_BAD_FIELDS), ids=list(_BAD_FIELDS))
def test_card_set_checks_every_other_field_before_reading_the_body(
    invoke: Invoke,
    store_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    source: str,
) -> None:
    """As post does, card set checks every field but the body before it reads the body,
    so a bad argument is the error even when the body cannot be read: standard input is
    left unread, and a body file is never opened."""
    data = b"Synthetic card notes.\n"
    stdin = io.BytesIO(data)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(stdin))
    missing = tmp_path / "missing-body.md"
    body = ["--body-stdin"] if source == "--body-stdin" else ["--body-file", str(missing)]
    args, field = _BAD_FIELDS[case]

    refused = invoke(*args, *body)

    assert refused.returncode == 2, refused.stdout
    assert refused.stderr.startswith(f"passing-notes: error: invalid-argument: {field}: ")
    assert stdin.tell() == 0
    assert not store_root.exists()
    # The probes see a read: with every field valid, the same body source is read.
    read = invoke(*card_set_args(), *body)
    if source == "--body-stdin":
        assert (read.returncode, stdin.tell()) == (0, len(data)), read.stderr
    else:
        assert read.returncode == 2
        assert f"error: {BODY_UNREADABLE}: --body-file: cannot read" in read.stderr


def test_lone_surrogates_in_card_values_through_a_real_process(store_root: Path) -> None:
    """``python -m`` passes a lone surrogate in argv through unchanged. Each free-text
    card value refuses it with exit 2 and one error line, never a traceback."""
    bad = f"bad{LONE_SURROGATE}value"
    cases = [
        (card_set_args("lane-b", bad), "item"),
        (card_set_args("lane-b", "Synthetic item", "--checkout", bad), "checkout"),
        (card_set_args("lane-b", "Synthetic item", "--branch", bad), "branch"),
        (card_set_args("lane-b", "Synthetic item", "--next", bad), "next_checkpoint"),
        (card_set_args("lane-b", "Synthetic item", "--own", bad), "owns entry 1"),
        (card_set_args("lane-b", "Synthetic item", "--hold", bad), "holds entry 1"),
    ]
    for args, name in cases:
        result = run_cli(args, root=store_root, command=module_command())
        assert result.returncode == 2, (name, result.stderr)
        assert result.stderr.startswith(
            f"passing-notes: error: invalid-argument: {name}: character 4 ('\\udc80') "
        )
        assert len(result.stderr.splitlines()) == 1
    assert not store_root.exists()


def test_card_show_frames_the_body_and_escapes_what_a_card_may_hold(
    invoke: Invoke, store_root: Path
) -> None:
    """Every body line sits behind the gutter inside the card frame, so none can pass
    for a frame line. ESC and other hiding characters print as escapes; a tab in a body
    line prints as-is. A private-use character, valid in a value, prints escaped in both
    views, and a card or file name holding a hiding character is reported escaped."""
    private = chr(0xE000)
    hostile = [CARD_BODY_END, "state: done", f"color{ESC}[31m red", f"{TAB}indented", f"{RLO}x"]
    text = FULL_TEXT.replace("item: Step 2 tests", f"item: Step 2 {private}").replace(
        "Synthetic card notes.\nSecond line.\n", "\n".join(hostile) + "\n"
    )
    place_card(store_root, text.encode("utf-8"), "lane-b.md")
    hidden = CARD_MINIMAL.replace(b"lane: lane-a", b"lane: lane-c")
    place_card(store_root, hidden.replace(b"Synthetic item", f"Hid{ZWSP}den".encode()), "lane-c.md")
    place_card(store_root, b"x", f"bad{RLO}name.md")

    shown = invoke("card", "show", "--project", PROJECT, "--lane", "lane-b")
    assert shown.returncode == 0, shown.stderr
    assert "item: Step 2 \\ue000" in shown.lines
    assert body_lines(shown, CARD_BODY_BEGIN, CARD_BODY_END) == [
        CARD_BODY_END,
        "state: done",
        "color\\x1b[31m red",
        f"{TAB}indented",
        "\\u202ex",
    ]
    assert shown.lines.count(CARD_BODY_END) == 1 and shown.lines[-1] == CARD_BODY_END

    listed = invoke("card", "show", "--project", PROJECT)
    assert listed.returncode == 0, listed.stderr
    assert listed.lines[0].split(TAB)[:4] == ["lane-b", "codex", "active", "Step 2 \\ue000"]
    folder = f"cards/{PROJECT}"
    assert f"diagnostic: {folder}/bad\\u202ename.md: ignored: not named <lane>.md" in listed.lines
    assert (
        f"diagnostic: {folder}/lane-c.md: invalid: item: character 4 ('\\u200b') is a control, "
        "format, surrogate or invisible character; a one-line value cannot hold it"
    ) in listed.lines
    assert listed.lines[-1] == "diagnostics: 2"
    assert unsafe_in(shown.stdout + listed.stdout) == []


def test_card_show_bounds_its_diagnostics_with_a_total_count(
    invoke: Invoke, store_root: Path
) -> None:
    place_card(store_root, CARD_FULL, "lane-b.md")
    total = DIAGNOSTIC_LINES + 5
    for n in range(total):
        place_card(store_root, b"broken", f"broken-{n:02d}.md")
    result = invoke("card", "show", "--project", PROJECT)
    assert result.returncode == 0, result.stderr
    assert result.lines[0].startswith(f"lane-b{TAB}")
    diagnostics = [line for line in result.lines if line.startswith("diagnostic: ")]
    assert len(diagnostics) == DIAGNOSTIC_LINES
    assert result.lines[-1] == f"diagnostics: {total} ({DIAGNOSTIC_LINES} shown)"


def test_card_show_of_a_missing_or_invalid_card_exits_3(invoke: Invoke, store_root: Path) -> None:
    missing = invoke("card", "show", "--project", PROJECT, "--lane", "lane-b")
    assert missing.returncode == 3
    assert missing.stderr == (
        f"passing-notes: error: not-found: no card for lane lane-b in project {PROJECT}\n"
    )
    assert not store_root.exists()
    place_card(store_root, CARD_FULL, "lane-a.md")  # the card inside names lane-b
    invalid = invoke("card", "show", "--project", PROJECT, "--lane", "lane-a")
    assert invalid.returncode == 3
    assert invalid.stderr.startswith(
        "passing-notes: error: card-invalid: card for lane lane-a is invalid: lane: "
    )


def test_a_linked_cards_folder_exits_4(
    invoke: Invoke, store_root: Path, tmp_path: Path, dir_link: MakeLink
) -> None:
    """A link planted at cards/<project>, here into a Git working tree, is never
    followed: card set and both forms of card show exit 4 with store-link."""
    repo = tmp_path / "fake-repo"
    (repo / ".git").mkdir(parents=True)
    planted = repo / "planted"
    planted.mkdir()
    link = card_folder(store_root)
    link.parent.mkdir(parents=True)
    dir_link(link, planted, "junction" if os.name == "nt" else "symlink")
    for args in (
        card_set_args(),
        ["card", "show", "--project", PROJECT],
        ["card", "show", "--project", PROJECT, "--lane", "lane-b"],
    ):
        result = invoke(*args)
        assert result.returncode == 4, (args, result.stderr)
        assert "error: store-link:" in result.stderr
    assert list(planted.iterdir()) == []
