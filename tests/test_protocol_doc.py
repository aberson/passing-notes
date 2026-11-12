"""docs/protocol.md and protocol.py must agree (decision D11).

Parses the example note, ack and lane card in the document, and checks the Limits
table, the identifier grammar, the note and card field orders and rules, the kinds,
the needs values, the card hosts and states against the constants of the same names
in ``passing_notes.protocol``.
"""

from __future__ import annotations

import re
import textwrap

import pytest
from conftest import REPO_ROOT

from passing_notes import protocol

DOC = REPO_ROOT / "docs" / "protocol.md"
_FENCE = re.compile(r"^( *)```(\w*)\n(.*?)^\1```", re.MULTILINE | re.DOTALL)
_TABLE_ROW = re.compile(r"^\|(.+)\|\s*$", re.MULTILINE)
_CODE = re.compile(r"`([^`]+)`")
_NAME_RANGE = re.compile(r"`([a-z]+)([0-9])` to `\1([0-9])`")


def doc_text() -> str:
    return DOC.read_text(encoding="utf-8").replace("\r\n", "\n")


def section(title: str) -> str:
    """The text under ``## <title>`` up to the next level-2 heading."""
    text = doc_text()
    start = text.index(f"\n## {title}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def fenced(block_section: str, language: str) -> list[str]:
    """Fenced blocks of ``language``, dedented (list-item blocks are indented)."""
    return [
        textwrap.dedent(match.group(3))
        for match in _FENCE.finditer(block_section)
        if match.group(2) == language
    ]


def table_rows(block_section: str) -> list[list[str]]:
    rows = []
    for match in _TABLE_ROW.finditer(block_section):
        cells = [cell.strip() for cell in match.group(1).split("|")]
        if set("".join(cells)) <= set("-: "):
            continue  # the separator row
        rows.append(cells)
    return rows[1:]  # drop the header row


def code_cell(cell: str) -> str:
    """The text of the one code span that makes up the whole table ``cell``."""
    match = _CODE.fullmatch(cell)
    assert match is not None, f"not a single code span: {cell!r}"
    return match.group(1)


def first_code(text: str) -> str:
    """The text of the first code span in ``text``."""
    match = _CODE.search(text)
    assert match is not None, f"no code span in {text!r}"
    return match.group(1)


# --- Limits -------------------------------------------------------------------------------


def test_limits_table_matches_the_constants() -> None:
    rows = table_rows(section("Limits"))
    table = {code_cell(name): int(value) for name, value in rows}
    assert table == dict(protocol.LIMITS)
    for name, value in table.items():
        assert getattr(protocol, name) == value, name


# --- Examples -----------------------------------------------------------------------------


def test_example_note_parses_and_renders_identically() -> None:
    blocks = fenced(section("Note format"), "markdown")
    assert blocks, "no markdown example under 'Note format'"
    example = blocks[0]
    note = protocol.parse_note(
        example,
        file_stem="20300101T120000Z-lane-a-to-lane-b-1a2b",
        folder_project="example-project",
    )
    assert (note.from_lane, note.to, note.kind, note.needs) == (
        "lane-a",
        "lane-b",
        "question",
        "reply",
    )
    assert note.refs == ("commit 0000000", "docs/example.md")
    assert note.body.startswith("What I need:")
    assert protocol.render_note(note) == example  # writers emit the documented order


def test_example_ack_parses_and_renders_identically() -> None:
    blocks = fenced(section("Inbox, read, acknowledge"), "text")
    assert blocks, "no ack example under 'Inbox, read, acknowledge'"
    example = blocks[0]
    ack = protocol.parse_ack(example, file_reader="lane-b")
    assert ack == protocol.Ack("lane-b", "2030-01-01T12:05:00Z", "example-window-1")
    assert protocol.render_ack(ack) == example
    assert len(example.encode("utf-8")) <= protocol.ACK_MAX_BYTES


def test_example_card_parses_and_renders_identically() -> None:
    blocks = fenced(section("Lane cards"), "markdown")
    assert blocks, "no markdown example under 'Lane cards'"
    example = blocks[0]
    card = protocol.parse_card(example, file_stem="lane-b", folder_project="example-project")
    assert (card.host, card.state, card.item) == ("codex", "active", "Step 2 tests")
    assert (card.owns, card.holds) == (("src/example/",), ("none",))
    assert card.body.startswith("Optional short notes")
    assert protocol.render_card(card) == example  # writers emit the documented order
    assert len(example.encode("utf-8")) <= protocol.CARD_MAX_BYTES


# --- Grammar and field set ----------------------------------------------------------------


def test_identifier_grammar_matches() -> None:
    rows = {row[0]: row for row in table_rows(section("Identifiers"))}
    project = rows["project id"]
    assert first_code(project[1]) == protocol.SLUG_PATTERN
    assert f"at most {protocol.ID_MAX_CHARS} chars" in project[1]
    assert f"at most {protocol.ID_MAX_CHARS} chars" in rows["lane id"][1]
    for reserved in protocol.RESERVED_LANE_IDS:
        assert f"`{reserved}`" in rows["lane id"][2]
    token = rows["token"]
    assert first_code(token[1]) == protocol.TOKEN_PATTERN
    assert f"at most {protocol.TOKEN_MAX_CHARS} chars" in token[1]
    assert f"`{protocol.NOTE_ID_SHAPE}`" in rows["note id"][1]


def test_windows_device_names_match() -> None:
    """The Identifiers rule lists exactly the names protocol.py rejects."""
    paragraphs = section("Identifiers").split("\n\n")
    rule = " ".join(next(p for p in paragraphs if "device names" in p).split())
    assert "are invalid project and lane ids, in any letter case:" in rule
    names = {
        f"{stem}{n}"
        for stem, first, last in _NAME_RANGE.findall(rule)
        for n in range(int(first), int(last) + 1)
    }
    names |= set(_CODE.findall(_NAME_RANGE.sub("", rule)))
    assert names == protocol.WINDOWS_DEVICE_NAMES
    for name in names:
        for check in (protocol.check_project_id, protocol.check_lane_id):
            with pytest.raises(protocol.ProtocolError, match="device name"):
                check(name)


def _field_rows() -> list[list[str]]:
    rows = table_rows(section("Note format").split("**Kinds:**")[0])
    return [row for row in rows if _CODE.fullmatch(row[0])]


def _fields_by_name() -> dict[str, list[str]]:
    return {code_cell(row[0]): row for row in _field_rows()}


def test_note_fields_match_in_writer_order() -> None:
    rows = _field_rows()
    names = [code_cell(row[0]) for row in rows]
    assert tuple(names) == protocol.NOTE_FIELDS
    optional = {code_cell(row[0]) for row in rows if "optional" in row[1]}
    assert optional == protocol.NOTE_OPTIONAL_FIELDS


def test_utc_format_is_the_shape_errors_show() -> None:
    rows = _fields_by_name()
    assert rows["created_utc"][1] == f"`{protocol.UTC_SHAPE}`"
    with pytest.raises(protocol.ProtocolError, match=f"is not {protocol.UTC_SHAPE}$"):
        protocol.parse_utc("2030-01-01 12:00:00", "created_utc")


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("created_utc", f"Within {protocol.CREATED_SKEW_SECONDS} s"),
        ("subject", f"1-{protocol.SUBJECT_MAX_CHARS} chars"),
        ("work", f"At most {protocol.WORK_MAX_CHARS} chars"),
        ("revision", f"At most {protocol.REVISION_MAX_CHARS} chars"),
        ("refs", f"At most {protocol.REFS_MAX_ITEMS} entries of at most {protocol.REF_MAX_CHARS}"),
    ],
)
def test_field_rules_quote_the_limits(field: str, expected: str) -> None:
    assert expected in _fields_by_name()[field][2]


def test_kinds_and_needs_match() -> None:
    note_section = section("Note format")
    kinds_list = note_section.split("**Kinds:**")[1].split("\n\n")[0]
    assert tuple(_CODE.findall(kinds_list)) == protocol.KINDS
    needs_row = _fields_by_name()["needs"]
    assert tuple(_CODE.findall(needs_row[1])) == protocol.NEEDS


_CATEGORY = re.compile(r"`([A-Z][a-z])`")


def test_character_rules_name_the_categories_the_code_uses() -> None:
    """The one-line rule lists LINE_FORBIDDEN_CATEGORIES; the printing rule adds the
    rest of DISPLAY_ESCAPED_CATEGORIES."""
    paragraphs = section("Note format").split("\n\n")
    one_line = next(p for p in paragraphs if p.startswith("A one-line value"))
    assert set(_CATEGORY.findall(one_line)) == protocol.LINE_FORBIDDEN_CATEGORIES
    assert "default-ignorable" in one_line
    inbox = section("Inbox, read, acknowledge")
    printing = next(item for item in inbox.split("\n- ") if item.startswith("**Printing"))
    extra = protocol.DISPLAY_ESCAPED_CATEGORIES - protocol.LINE_FORBIDDEN_CATEGORIES
    assert set(_CATEGORY.findall(printing)) == extra
    assert "`\\u200b`" in printing and protocol.escape_unsafe("\u200b") == "\\u200b"


def test_publication_and_sizes_quote_the_limits() -> None:
    posting = section("Posting")
    assert f"up to {protocol.PUBLISH_ATTEMPTS} attempts" in posting
    inbox = section("Inbox, read, acknowledge")
    assert f"An ack is at most {protocol.ACK_MAX_BYTES // 1024} KiB" in inbox
    cards = section("Lane cards")
    assert f"A card is at most {protocol.CARD_MAX_BYTES // 1024} KiB" in cards


# --- Lane cards -----------------------------------------------------------------------------


def _card_rows() -> dict[str, list[str]]:
    rows = table_rows(section("Lane cards"))
    return {code_cell(row[0]): row for row in rows if _CODE.fullmatch(row[0])}


def test_card_fields_match_in_writer_order() -> None:
    """The field table lists CARD_FIELDS in order; "optional" marks exactly
    CARD_OPTIONAL_FIELDS, and the item-line fields are exactly CARD_LIST_FIELDS."""
    rows = _card_rows()
    assert tuple(rows) == protocol.CARD_FIELDS
    optional = {name for name, row in rows.items() if "optional" in row[1]}
    assert optional == protocol.CARD_OPTIONAL_FIELDS
    lists = {name for name, row in rows.items() if row[1].startswith("`  - <text>` lines")}
    assert lists == set(protocol.CARD_LIST_FIELDS)
    assert tuple(_CODE.findall(rows["host"][1])) == protocol.HOSTS
    assert tuple(_CODE.findall(rows["state"][1])) == protocol.CARD_STATES
    assert rows["updated_utc"][1] == f"`{protocol.UTC_SHAPE}`"
    assert rows["window"][1] == "token, optional"
    assert (rows["lane"][1], rows["project"][1]) == ("lane id", "project id")


_CARD_LIST_RULE = (
    f"At most {protocol.CARD_LIST_MAX_ITEMS} entries of at most {protocol.CARD_LINE_MAX_CHARS}"
)


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        *[(name, f"At most {limit} chars") for name, limit in protocol.CARD_LINE_LIMITS.items()],
        *[(name, _CARD_LIST_RULE) for name in protocol.CARD_LIST_FIELDS],
    ],
)
def test_card_field_rules_quote_the_limits(field: str, expected: str) -> None:
    assert _card_rows()[field][2].startswith(expected)
