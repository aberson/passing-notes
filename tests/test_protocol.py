"""Unit tests for protocol.py: grammar, field rules, limits, parsing and rendering."""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import sys
import tempfile
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import CREATED, FIXTURES, PROJECT, V1_NOTE_FULL, note_id, note_text

from passing_notes import protocol
from passing_notes.protocol import (
    ACK_MAX_BYTES,
    CREATED_SKEW_SECONDS,
    ID_MAX_CHARS,
    NOTE_MAX_BYTES,
    REF_MAX_CHARS,
    REFS_MAX_ITEMS,
    REVISION_MAX_CHARS,
    SUBJECT_MAX_CHARS,
    TOKEN_MAX_CHARS,
    WORK_MAX_CHARS,
    OversizeError,
    ProtocolError,
)

BOM = chr(0xFEFF)


def draft(**overrides: object) -> protocol.Draft:
    fields: dict[str, object] = {
        "project": PROJECT,
        "from_lane": "lane-a",
        "to": "lane-b",
        "subject": "Synthetic subject",
        "kind": "question",
        "needs": "reply",
    }
    fields.update(overrides)
    return protocol.make_draft(**fields)  # type: ignore[arg-type]


# --- Identifiers ----------------------------------------------------------------------


@pytest.mark.parametrize("value", ["a", "example-project", "x1-2y", "a" * ID_MAX_CHARS, "dev"])
def test_valid_project_ids(value: str) -> None:
    assert protocol.check_project_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a" * (ID_MAX_CHARS + 1),
        "Example",
        "a--b",
        "-a",
        "a-",
        "a_b",
        "a.b",
        "..",
        "../x",
        "..\\x",
        "a/b",
        "C:\\x",
        "/abs",
        "a b",
        "a\nb",
        "nul",
        "con",
        "com1",
        "lpt9",
    ],
)
def test_invalid_project_ids(value: str) -> None:
    with pytest.raises(ProtocolError):
        protocol.check_project_id(value)


@pytest.mark.parametrize("value", ["lane-a", "toast", "auto-lane", "a" * ID_MAX_CHARS])
def test_valid_lane_ids(value: str) -> None:
    assert protocol.check_lane_id(value) == value


@pytest.mark.parametrize(
    "value",
    ["go-to-x", "to", "to-x", "x-to", "all", "operator", "a" * 33, "Lane", "..", "nul"],
)
def test_invalid_lane_ids(value: str) -> None:
    with pytest.raises(ProtocolError):
        protocol.check_lane_id(value)


def test_recipient_allows_all_and_operator_but_not_bad_lanes() -> None:
    assert protocol.check_recipient("all") == "all"
    assert protocol.check_recipient("operator") == "operator"
    assert protocol.check_recipient("lane-b") == "lane-b"
    with pytest.raises(ProtocolError):
        protocol.check_recipient("go-to-x")


@pytest.mark.parametrize("value", ["a", "example-window-1", "A.b_c:d-e", "x" * TOKEN_MAX_CHARS])
def test_valid_tokens(value: str) -> None:
    assert protocol.check_token(value, "token") == value


@pytest.mark.parametrize("value", ["", "x" * (TOKEN_MAX_CHARS + 1), "a b", "a/b", "a\\b", "é"])
def test_invalid_tokens(value: str) -> None:
    with pytest.raises(ProtocolError):
        protocol.check_token(value, "token")


def test_note_id_parts() -> None:
    parts = protocol.parse_note_id("20300101T120000Z-lane-a-to-lane-b-1a2b")
    assert parts.stamp == CREATED
    assert (parts.sender, parts.recipient, parts.hex) == ("lane-a", "lane-b", "1a2b")
    assert protocol.parse_note_id("20300101T120000Z-lane-a-to-all-00ff").recipient == "all"
    assert protocol.parse_note_id("20300101T120000Z-a-to-operator-abcd").recipient == "operator"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "20300101T120000Z-lane-a-to-lane-b-1A2B",  # uppercase hex
        "20300101T120000Z-lane-a-to-lane-b-1a2",  # 3 hex
        "20300101T120000Z-lane-a-to-lane-b-1a2b.md",
        "20300101T120000Z-lane-a-to-lane-b-1a2b/../../x",
        "../../20300101T120000Z-lane-a-to-lane-b-1a2b",
        "20301301T120000Z-lane-a-to-lane-b-1a2b",  # month 13
        "20300101T250000Z-lane-a-to-lane-b-1a2b",  # hour 25
        "20300101t120000z-lane-a-to-lane-b-1a2b",  # lowercase stamp
        "20300101T120000Z-all-to-lane-b-1a2b",  # reserved sender
        "20300101T120000Z-operator-to-lane-b-1a2b",
        "20300101T120000Z-a-to-b-to-c-1a2b",  # a 'to' segment either way
        "20300101T120000Z-lane--a-to-lane-b-1a2b",
        "20300101T120000Z-" + "a" * 33 + "-to-lane-b-1a2b",
        "x" * 500,
    ],
)
def test_invalid_note_ids(value: str) -> None:
    assert not protocol.is_note_id(value)
    with pytest.raises(ProtocolError):
        protocol.check_note_id(value)


def test_build_note_id_rejects_bad_hex() -> None:
    with pytest.raises(ProtocolError):
        protocol.build_note_id(CREATED, "lane-a", "lane-b", "XYZ1")


def test_note_id_length_bound_admits_the_longest_valid_id() -> None:
    """The bound is exact: the longest valid id passes (build_note_id validates it), and
    one character more is refused by the length check itself, before the regex runs."""
    longest = protocol.build_note_id(CREATED, "a" * ID_MAX_CHARS, "b" * ID_MAX_CHARS, "1a2b")
    with pytest.raises(ProtocolError, match="too long to be a note id"):
        protocol.parse_note_id(longest + "0")


# --- Derived patterns track their sources ------------------------------------------------
#
# protocol.py defines each grammar source once and builds every other pattern, separator
# and bound from it. Two representative sources are redefined here in a fresh copy of
# the module: SLUG_PATTERN (ids, note ids and ack file names) and HEADER_BLANKS (every
# blank that the note and ack parsers skip or strip). A pattern that spelled its source
# out again would keep the old shape and fail, even though its string equals the
# derived one today.


def protocol_with(folder: Path, name: str, expression: str) -> types.ModuleType:
    """A fresh copy of protocol.py with the value assigned to ``name`` replaced, imported.

    The assignment is found with ``ast``, so its layout (one line or wrapped by the
    formatter) does not matter. The copy is written under ``folder`` (a temporary path,
    in its own new folder so no cached bytecode is ever reused) and imported under
    another name; the real module is untouched.
    """
    source = Path(protocol.__file__).read_bytes()
    assignments = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == name
    ]
    assert len(assignments) == 1, f"{name} must be assigned once at module level"
    value = assignments[0].value
    assert value is not None and value.end_lineno is not None
    assert value.end_col_offset is not None
    lines = source.splitlines(keepends=True)  # ast offsets count UTF-8 bytes
    start = sum(map(len, lines[: value.lineno - 1])) + value.col_offset
    end = sum(map(len, lines[: value.end_lineno - 1])) + value.end_col_offset
    copy = Path(tempfile.mkdtemp(dir=folder)) / "protocol_variant.py"
    copy.write_bytes(source[:start] + expression.encode("utf-8") + source[end:])
    spec = importlib.util.spec_from_file_location("protocol_variant", copy)
    assert spec is not None and spec.loader is not None
    variant = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = variant  # dataclasses look their module up while defining
    try:
        spec.loader.exec_module(variant)
    finally:
        del sys.modules[spec.name]
    assert getattr(variant, name) != getattr(protocol, name)
    return variant


def test_ids_and_ack_names_track_slug_pattern(tmp_path: Path) -> None:
    """Widen the slug grammar to allow "_": the note id regex must accept it too."""
    variant = protocol_with(tmp_path, "SLUG_PATTERN", '"[a-z0-9_]+(-[a-z0-9_]+)*"')
    wide = "20300101T120000Z-lane_a-to-lane_b-1a2b"
    assert not protocol.is_note_id(wide)
    parts = variant.parse_note_id(wide)
    assert (parts.sender, parts.recipient) == ("lane_a", "lane_b")
    assert variant.build_note_id(CREATED, "lane_a", "lane_b", "1a2b") == wide
    assert variant.parse_ack_file_name(variant.ack_file_name(wide, "lane_c")) == (wide, "lane_c")
    assert variant.check_project_id("project_x") == "project_x"


@pytest.fixture(scope="module")
def space_only_blanks(tmp_path_factory: pytest.TempPathFactory) -> types.ModuleType:
    """protocol.py with HEADER_BLANKS narrowed to a space, so a tab is not a blank."""
    return protocol_with(tmp_path_factory.mktemp("variant"), "HEADER_BLANKS", '" "')


ACK_TEXT = "reader: lane-b\nread_utc: 2030-01-01T12:05:00Z\nreceived\n"
_TAB = chr(9)
# One case per place a reader skips or strips blanks. The real grammar accepts each;
# with HEADER_BLANKS narrowed to a space, each tab is no longer a blank and breaks.
_TABBED_NOTE = note_text(refs=["r"], extra=[("x-future", "1")])
BLANK_SITES = {
    "blank header line": _TABBED_NOTE.replace("\nsubject:", f"\n{_TAB}\nsubject:"),
    "before a list item": _TABBED_NOTE.replace("  - r", f"{_TAB}- r"),
    "after a list item": _TABBED_NOTE.replace("  - r", f"  - r{_TAB}"),
    "indented unknown-key line": _TABBED_NOTE.replace("x-future: 1", f"x-future: 1\n{_TAB}more"),
    "after a value": _TABBED_NOTE.replace(
        "subject: Synthetic subject", f"subject: Synthetic subject{_TAB}"
    ),
    "after the opening ---": _TABBED_NOTE.replace("---\n", f"---{_TAB}\n", 1),
    "after the closing ---": _TABBED_NOTE.replace("\n---\n", f"\n---{_TAB}\n"),
    "ack blank line": ACK_TEXT.replace("received", f"{_TAB}\nreceived"),
    "ack after received": ACK_TEXT.replace("received", f"received{_TAB}"),
    "ack after a value": ACK_TEXT.replace("lane-b\n", f"lane-b{_TAB}\n"),
}


@pytest.mark.parametrize("text", BLANK_SITES.values(), ids=BLANK_SITES.keys())
def test_header_blanks_track_header_blanks(space_only_blanks: types.ModuleType, text: str) -> None:
    is_ack = text.startswith("reader")
    real = protocol.parse_ack if is_ack else protocol.parse_note
    narrowed = space_only_blanks.parse_ack if is_ack else space_only_blanks.parse_note
    assert real(text)
    with pytest.raises(space_only_blanks.ProtocolError):
        narrowed(text)


# --- One-line fields ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "limit"),
    [("work", WORK_MAX_CHARS), ("revision", REVISION_MAX_CHARS)],
)
def test_one_line_limits(field: str, limit: int) -> None:
    assert protocol.check_line("x" * limit, field, limit)
    with pytest.raises(ProtocolError, match=f"at most {limit}"):
        protocol.check_line("x" * (limit + 1), field, limit)


@pytest.mark.parametrize("value", ["", "a\nb", "a\rb", "a\tb", " a", "a "])
def test_one_line_rejects_breaks_and_surrounding_blanks(value: str) -> None:
    """Empty, broken or padded values; HIDING_CHARACTERS below covers the rest."""
    with pytest.raises(ProtocolError):
        protocol.check_line(value, "work", WORK_MAX_CHARS)


# Characters that can hide, reorder or break text, one or more per class: a one-line
# value refuses each, and printing shows each as an escape.
HIDING_CHARACTERS = {
    "C0 control (ESC)": "\x1b",
    "C1 control (NEL)": "\x85",
    "zero-width space (Cf)": "\u200b",
    "zero-width joiner (Cf)": "\u200d",
    "word joiner (Cf)": "\u2060",
    "zero-width no-break space (Cf)": "\ufeff",
    "soft hyphen (Cf)": "\xad",
    "right-to-left override (Cf)": "\u202e",
    "left-to-right isolate (Cf)": "\u2066",
    "Arabic letter mark (Cf)": "\u061c",
    "tag letter (Cf)": "\U000e0041",
    "cancel tag (Cf)": "\U000e007f",
    "lone high surrogate (Cs)": "\ud800",
    "lone low surrogate (Cs)": "\udc80",
    "line separator (Zl)": "\u2028",
    "paragraph separator (Zp)": "\u2029",
    "variation selector 16 (ignorable Mn)": "\ufe0f",
    "variation selector 17 (ignorable Mn)": "\U000e0100",
    "combining grapheme joiner (ignorable Mn)": "\u034f",
    "Hangul filler (ignorable Lo)": "\u3164",
    "halfwidth Hangul filler (ignorable Lo)": "\uffa0",
    "reserved ignorable (Cn)": "\u2065",
    "reserved tag (Cn)": "\U000e0080",
}


@pytest.mark.parametrize("char", HIDING_CHARACTERS.values(), ids=HIDING_CHARACTERS.keys())
def test_one_line_values_refuse_every_hiding_character(char: str) -> None:
    with pytest.raises(ProtocolError, match=r"character 2 \(.*\) is a control, format"):
        protocol.check_line(f"a{char}b", "work", WORK_MAX_CHARS)
    with pytest.raises(ProtocolError):
        draft(subject=f"Synthetic{char}subject")
    assert protocol.is_forbidden_in_line(char) and protocol.is_unsafe_char(char)
    escaped = protocol.escape_unsafe(f"a{char}b")
    assert escaped == f"a{ascii(char)[1:-1]}b" and escaped.isascii()


def test_one_line_values_keep_visible_text() -> None:
    """Visible text is valid and prints unchanged. Private-use and unassigned code points
    are valid too (validity must not depend on the Unicode version) but print escaped."""
    visible = "Caf\u00e9 \u65e5\u672c a\u00a0b \u2014 \u2713 \u0415"
    assert protocol.check_line(visible, "work", WORK_MAX_CHARS) == visible
    assert protocol.escape_unsafe(visible) == visible
    for char in ("\ue000", "\U000f0000", "\u0378"):  # private use (Co) and unassigned (Cn)
        assert protocol.check_line(f"a{char}b", "work", WORK_MAX_CHARS)
        assert not protocol.is_forbidden_in_line(char) and protocol.is_unsafe_char(char)
        assert protocol.escape_unsafe(char) == ascii(char)[1:-1]
    assert protocol.LINE_FORBIDDEN_CATEGORIES < protocol.DISPLAY_ESCAPED_CATEGORIES


def test_escape_unsafe_keeps_what_it_is_told_and_is_idempotent() -> None:
    text = "".join(HIDING_CHARACTERS.values()) + "\tplain"
    once = protocol.escape_unsafe(text, keep="\t")
    assert once.endswith("\tplain") and "\\t" not in once
    assert protocol.escape_unsafe(once, keep="\t") == once
    assert [ch for ch in once if ch != "\t" and protocol.is_unsafe_char(ch)] == []


def test_lone_surrogates_are_protocol_errors_not_crashes() -> None:
    """A lone surrogate has no UTF-8 form. Every path that encodes raises ProtocolError
    (exit 2 in the CLI), never UnicodeEncodeError (a traceback)."""
    for field in ("subject", "work", "revision"):
        with pytest.raises(ProtocolError, match=r"\('\\udc80'\)"):
            draft(**{field: "bad\udc80value"})
    with pytest.raises(ProtocolError, match="refs entry 2"):
        draft(refs=["ok", "bad\ud800value"])
    with pytest.raises(ProtocolError, match=r"body: character 2 \('\\udc80'\) is not text"):
        protocol.normalize_body("x\udc80")
    with pytest.raises(ProtocolError, match="body"):
        protocol.make_note(draft(), "x\ud800", CREATED, "1a2b")
    # A Draft built without make_draft still cannot crash the writer.
    unchecked = dataclasses.replace(draft(), subject="bad\udc80value")
    with pytest.raises(ProtocolError, match="note: character"):
        protocol.make_note(unchecked, "Body.", CREATED, "1a2b")
    with pytest.raises(ProtocolError, match="ack: character 1"):
        protocol.encode_text("\ud800", "ack")
    assert protocol.encode_text("caf\u00e9", "ack") == b"caf\xc3\xa9"


def test_subject_bounds_and_first_character() -> None:
    assert protocol.check_subject("x" * SUBJECT_MAX_CHARS)
    assert protocol.check_subject("Unicode is fine: café")
    with pytest.raises(ProtocolError):
        protocol.check_subject("x" * (SUBJECT_MAX_CHARS + 1))
    with pytest.raises(ProtocolError):
        protocol.check_subject("")
    for first in "\"'`([{<":
        with pytest.raises(ProtocolError, match="quote or bracket"):
            protocol.check_subject(f"{first}quoted")


def test_kinds_and_needs_are_closed_sets() -> None:
    with pytest.raises(ProtocolError):
        protocol.check_kind("question-and-answer")
    with pytest.raises(ProtocolError):
        protocol.check_needs("approval")


def test_refs_limits() -> None:
    at_limit = ["r"] * REFS_MAX_ITEMS
    assert protocol.check_refs(at_limit) is at_limit  # a check_* returns its value
    assert protocol.check_refs(["r" * REF_MAX_CHARS])
    with pytest.raises(ProtocolError):
        protocol.check_refs(["r"] * (REFS_MAX_ITEMS + 1))
    with pytest.raises(ProtocolError):
        protocol.check_refs(["r" * (REF_MAX_CHARS + 1)])
    with pytest.raises(ProtocolError):
        protocol.check_refs([""])


def test_parse_utc_returns_the_moment() -> None:
    moment = protocol.parse_utc("2030-01-01T12:00:00Z", "created_utc")
    assert moment == datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "value", ["2030-01-01T12:00:00", "2030-1-01T12:00:00Z", "2030-02-30T12:00:00Z", "now"]
)
def test_invalid_utc(value: str) -> None:
    with pytest.raises(ProtocolError):
        protocol.parse_utc(value, "created_utc")


# --- Parsing notes ----------------------------------------------------------------------


def test_parse_valid_note_any_order_unknown_keys_crlf() -> None:
    data = (FIXTURES / "v0-note-crlf.md").read_bytes()
    assert b"\r\n" in data  # the fixture must stay CRLF (see .gitattributes)
    note = protocol.parse_note_bytes(
        data, file_stem="20300101T120000Z-lane-a-to-lane-b-0a0a", folder_project=PROJECT
    )
    assert note.kind is None  # v0 notes may omit kind
    assert note.subject == "Synthetic v0 note written by hand"
    assert note.refs == ("commit 0000000", "docs/example.md")
    assert note.body.startswith("What I need: a synthetic answer.")
    assert "\r" not in note.body


def test_fixture_files_end_every_line_in_crlf() -> None:
    """Committed fixtures keep their bytes under any checkout settings.

    Git's autocrlf never rewrites a file once its lines end in CRLF, but turns an
    LF-only file into CRLF whenever .gitattributes is not yet in effect. So every file
    under tests/fixtures/ is CRLF throughout; LF-exact data is built by conftest.lf_file.
    """
    files = sorted(path for path in FIXTURES.iterdir() if path.is_file())
    assert files
    for path in files:
        data = path.read_bytes()
        assert b"\r\n" in data, f"{path.name} has no CRLF line ending"
        assert data.count(b"\n") == data.count(b"\r\n"), f"{path.name} has a bare LF"


def test_parse_full_v1_note_round_trips() -> None:
    data = V1_NOTE_FULL
    note = protocol.parse_note_bytes(data, folder_project=PROJECT)
    assert note.from_project == "other-project"
    assert note.sender == ("other-project", "lane-b")
    assert note.supersedes == "20300101T120900Z-lane-b-to-lane-a-0d0d"
    assert note.idempotency_key == "example-key.1"
    assert (note.work, note.revision, note.kind) == ("example-project Step 2", "0000000", "answer")
    assert protocol.render_note(note).encode("utf-8") == data


def test_parse_note_with_no_refs_and_empty_body() -> None:
    text = note_text(body="")
    note = protocol.parse_note(text)
    assert note.refs == ()
    assert note.body == ""


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (note_text().replace("---\n", "", 1), "start with a '---'"),
        (note_text().replace("\n---\n\n", "\n\n"), "no closing"),
        (note_text().replace("needs: ack\n", ""), "missing required"),
        (note_text().replace("refs:\n", ""), "missing required"),
        (note_text().replace("reply_to: none\n", ""), "missing required"),
        (note_text(extra=[("needs", "fyi")]), "duplicate"),
        (note_text(needs="approve"), "needs"),
        (note_text(kind="chat"), "kind"),
        (note_text(subject="[bracketed]"), "quote or bracket"),
        (note_text(subject="x" * (SUBJECT_MAX_CHARS + 1)), "subject"),
        (note_text(extra=[("work", "w" * (WORK_MAX_CHARS + 1))]), "work"),
        (note_text(extra=[("revision", "r" * (REVISION_MAX_CHARS + 1))]), "revision"),
        (note_text(extra=[("work", "")]), "work"),
        (note_text(extra=[("idempotency_key", "bad key")]), "idempotency_key"),
        (note_text(extra=[("from_project", "Bad")]), "from_project"),
        (note_text(extra=[("supersedes", "../x")]), "supersedes"),
        (
            note_text(
                extra=[("supersedes", note_id("20300101T120000Z", "lane-a", "lane-b", "1a2b"))]
            ),
            "itself",
        ),
        (note_text(refs=["r"] * (REFS_MAX_ITEMS + 1)), "refs"),
        (note_text(refs=["r" * (REF_MAX_CHARS + 1)]), "refs entry"),
        (note_text(created_utc="2030-01-01T12:01:01Z"), "created_utc"),
        (note_text(project="Bad_Project"), "project"),
        (note_text().replace("from: lane-a", "from: lane-c"), "does not match the from"),
        (note_text().replace("to: lane-b", "to: lane-c"), "does not match the from"),
        (note_text().replace("from: lane-a", "from: operator"), "reserved"),
        (note_text().replace("  - ", "").replace("refs:\n", "refs: inline\n"), "refs"),
        (note_text().replace("subject:", "  - stray item\nsubject:"), "list item outside refs"),
        (note_text().replace("subject:", " indented: x\nsubject:"), "indented"),
        (note_text().replace("subject:", "no colon here\nsubject:"), "key: value"),
    ],
)
def test_invalid_notes(text: str, reason: str) -> None:
    with pytest.raises(ProtocolError, match=reason):
        protocol.parse_note(text)


def test_created_skew_boundary() -> None:
    at_limit = f"2030-01-01T12:0{CREATED_SKEW_SECONDS // 60}:00Z"
    assert protocol.parse_note(note_text(created_utc=at_limit))
    before = "2030-01-01T11:59:00Z"  # 60 s before the stamp is also within the skew
    assert protocol.parse_note(note_text(created_utc=before))


def test_file_stem_and_folder_must_match() -> None:
    text = note_text()
    with pytest.raises(ProtocolError, match="file name stem"):
        protocol.parse_note(text, file_stem="20300101T120000Z-lane-a-to-lane-b-ffff")
    with pytest.raises(ProtocolError, match="folder"):
        protocol.parse_note(text, folder_project="other-project")


def test_note_bytes_limits_bom_and_encoding() -> None:
    small = note_text().encode("utf-8")
    padding = NOTE_MAX_BYTES - len(small)
    exact = note_text(body="Synthetic body." + "x" * padding).encode("utf-8")
    assert len(exact) == NOTE_MAX_BYTES
    assert protocol.parse_note_bytes(exact)
    with pytest.raises(OversizeError):
        protocol.parse_note_bytes(exact + b"x")
    with pytest.raises(ProtocolError, match="BOM"):
        protocol.parse_note_bytes(BOM.encode("utf-8") + small)
    with pytest.raises(ProtocolError, match="UTF-8"):
        protocol.parse_note_bytes(small.replace(b"Synthetic body.", b"\xff\xfe"))


def test_unknown_keys_may_carry_list_items_and_indented_lines() -> None:
    text = note_text(extra=[("x-future", "")]).replace(
        "x-future: \n", "x-future:\n  - item\n  nested: value\n"
    )
    assert protocol.parse_note(text).subject == "Synthetic subject"


# --- Writing notes ----------------------------------------------------------------------


def test_make_note_renders_fields_in_protocol_order() -> None:
    note, data = protocol.make_note(
        draft(
            from_project="other-project",
            work="example-project Step 2",
            revision="0000000",
            supersedes="20300101T110000Z-lane-a-to-lane-b-0000",
            reply_to="20300101T100000Z-lane-b-to-lane-a-0001",
            idempotency_key="key-1",
            refs=["commit 0000000", "docs/example.md"],
        ),
        "Line one.\r\nLine two.\r\n",
        CREATED,
        "1a2b",
    )
    text = data.decode("utf-8")
    keys = [line.split(":", 1)[0] for line in text.split("---\n")[1].splitlines()]
    keys = [key for key in keys if not key.startswith("  -")]
    assert keys == [k for k in protocol.NOTE_FIELDS]
    assert note.id == "20300101T120000Z-lane-a-to-lane-b-1a2b"
    assert note.created_utc == "2030-01-01T12:00:00Z"
    assert "\r" not in text and not data.startswith(protocol.UTF8_BOM)
    assert text.endswith("\n\nLine one.\nLine two.\n")


def test_make_note_omits_absent_optional_fields_and_same_project_sender() -> None:
    note, data = protocol.make_note(draft(from_project=PROJECT), "Body.", CREATED, "00aa")
    text = data.decode("utf-8")
    assert note.from_project is None
    for absent in ("from_project", "work", "revision", "supersedes", "idempotency_key"):
        assert f"\n{absent}:" not in text
    assert "\nreply_to: none\n" in text and "\nrefs:\n---\n" in text


@pytest.mark.parametrize(
    "overrides",
    [
        {"subject": "Injected\nneeds: fyi"},
        {"work": "x\nsupersedes: 20300101T110000Z-lane-c-to-lane-b-0000"},
        {"revision": "r\rkind: fyi"},
        {"refs": ["ok", "bad\n---"]},
        {"to": "../lane-b"},
        {"from_lane": "lane-a/../x"},
        {"project": ".."},
        {"reply_to": "../x"},
        {"idempotency_key": "has space"},
    ],
)
def test_make_draft_rejects_injection_and_traversal(overrides: dict[str, object]) -> None:
    with pytest.raises(ProtocolError):
        draft(**overrides)


def test_make_note_rejects_oversize_body() -> None:
    """A body at the byte limit leaves no room for the header."""
    with pytest.raises(ProtocolError, match="after rendering"):
        protocol.make_note(draft(), "x" * NOTE_MAX_BYTES, CREATED, "1a2b")


def test_body_normalization() -> None:
    assert protocol.normalize_body("\r\n\na\r\nb\rc\n\n") == "a\nb\nc"


# --- Addressing and supersedes ---------------------------------------------------------


def _note(sender: str, recipient: str, hex4: str, **extra: str) -> protocol.Note:
    pairs = [(key, value) for key, value in extra.items()]
    return protocol.parse_note(
        note_text(sender=sender, recipient=recipient, hex4=hex4, extra=pairs)
    )


def test_addressing_rules() -> None:
    to_b = _note("lane-a", "lane-b", "0001")
    to_all = _note("lane-a", "all", "0002")
    to_operator = _note("lane-a", "operator", "0003")
    assert protocol.is_addressed_to(to_b, PROJECT, "lane-b")
    assert not protocol.is_addressed_to(to_b, PROJECT, "lane-c")
    assert protocol.is_addressed_to(to_all, PROJECT, "lane-b")
    assert not protocol.is_addressed_to(to_all, PROJECT, "lane-a")  # the sender
    assert not protocol.is_addressed_to(to_operator, PROJECT, "lane-b")
    assert not protocol.is_addressed_to(to_b, "other-project", "lane-b")
    cross = _note("lane-a", "all", "0004", from_project="other-project")
    assert protocol.is_addressed_to(cross, PROJECT, "lane-a")  # a different sender


def test_resolve_supersedes_same_sender_only() -> None:
    old = _note("lane-a", "lane-b", "0001")
    newer = _note("lane-a", "lane-b", "0002", supersedes=old.id)
    other_old = _note("lane-c", "lane-b", "0003")
    hijack = _note("lane-a", "lane-b", "0004", supersedes=other_old.id)
    missing = _note(
        "lane-a",
        "lane-b",
        "0005",
        supersedes=note_id("20300101T110000Z", "lane-a", "lane-b", "9999"),
    )
    cross_project = _note(
        "lane-a", "lane-b", "0006", from_project="other-project", supersedes=old.id
    )
    result = protocol.resolve_supersedes([old, newer, other_old, hijack, missing, cross_project])
    assert result.hidden == {old.id}
    reasons = dict(result.ignored)
    assert "another sender" in reasons[hijack.id]
    assert "missing" in reasons[missing.id]
    assert "another sender" in reasons[cross_project.id]


# --- Acks -----------------------------------------------------------------------------------


def test_ack_render_and_parse() -> None:
    ack = protocol.make_ack("lane-b", datetime(2030, 1, 1, 12, 5, tzinfo=UTC), "example-window-1")
    text = protocol.render_ack(ack)
    assert (
        text
        == "reader: lane-b\nread_utc: 2030-01-01T12:05:00Z\nsession: example-window-1\nreceived\n"
    )
    assert protocol.parse_ack(text, file_reader="lane-b") == ack
    no_session = protocol.make_ack("lane-b", datetime(2030, 1, 1, tzinfo=UTC))
    assert "session" not in protocol.render_ack(no_session)


def test_ack_fixture_with_blank_lines_and_crlf() -> None:
    data = (FIXTURES / "v0-ack-blank-lines.md").read_bytes()
    assert b"\r\n\r\n" in data
    ack = protocol.parse_ack_bytes(data, file_reader="lane-b")
    assert (ack.reader, ack.read_utc, ack.session) == ("lane-b", "2030-01-01T12:05:00Z", None)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("reader: lane-b\nread_utc: 2030-01-01T12:05:00Z\n", "last line"),
        ("reader: lane-b\nreceived\n", "missing"),
        ("reader: lane-c\nread_utc: 2030-01-01T12:05:00Z\nreceived\n", "file name"),
        ("reader: lane-b\nread_utc: soon\nreceived\n", "read_utc"),
        ("reader: lane-b\nread_utc: 2030-01-01T12:05:00Z\nsession: a b\nreceived\n", "session"),
        ("reader: lane-b\nreader: lane-b\nread_utc: 2030-01-01T12:05:00Z\nreceived\n", "duplicate"),
        ("reader: lane-b\nsome text\nread_utc: 2030-01-01T12:05:00Z\nreceived\n", "key: value"),
    ],
)
def test_invalid_acks(text: str, reason: str) -> None:
    with pytest.raises(ProtocolError, match=reason):
        protocol.parse_ack(text, file_reader="lane-b")


def test_ack_size_limit() -> None:
    base = b"reader: lane-b\nread_utc: 2030-01-01T12:05:00Z\nreceived\n"
    padded = b"x-pad: " + b"p" * (ACK_MAX_BYTES - len(base) - 8) + b"\n" + base
    assert len(padded) == ACK_MAX_BYTES
    assert protocol.parse_ack_bytes(padded, file_reader="lane-b")
    with pytest.raises(OversizeError):
        protocol.parse_ack_bytes(padded + b"\n", file_reader="lane-b")


def test_ack_file_names() -> None:
    name = protocol.ack_file_name("20300101T120000Z-lane-a-to-lane-b-1a2b", "lane-b")
    assert name == "20300101T120000Z-lane-a-to-lane-b-1a2b--lane-b.md"
    assert protocol.parse_ack_file_name(name) == (
        "20300101T120000Z-lane-a-to-lane-b-1a2b",
        "lane-b",
    )
    for bad, reason in [
        ("x--lane-b.md", "note id"),
        ("20300101T120000Z-lane-a-to-lane-b-1a2b--all.md", "reserved"),
        ("a--b--c.md", "is not <note id>--<lane>.md"),
        ("x.txt", "is not <note id>--<lane>.md"),
        (f"{name}.txt", "is not <note id>--<lane>.md"),
    ]:
        # Like every parse_* function: ProtocolError for invalid input, never None.
        with pytest.raises(ProtocolError, match=reason):
            protocol.parse_ack_file_name(bad)
    with pytest.raises(ProtocolError):
        protocol.ack_file_name("20300101T120000Z-lane-a-to-lane-b-1a2b", "../x")
