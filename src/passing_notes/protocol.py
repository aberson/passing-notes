"""Protocol v1: the one source of the grammar, field set, kinds, needs and limits.

``docs/protocol.md`` is the authoritative description. ``tests/test_protocol_doc.py``
checks that this module agrees with it: the Limits table, the identifier grammar,
the field order, the kinds, the needs values and the worked examples.

Everything here is pure: no file system access. ``store.py`` does the I/O.

Naming conventions, for every function here: a ``check_*`` validator returns its
value or raises ProtocolError; a ``parse_*`` parser returns what it parsed or raises
ProtocolError, and never returns None; an ``is_*`` predicate returns a bool and never
raises.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final

# --- Limits (docs/protocol.md, "Limits"; names must match the table exactly) ---------

NOTE_MAX_BYTES: Final = 16384
ACK_MAX_BYTES: Final = 1024
CARD_MAX_BYTES: Final = 8192
ID_MAX_CHARS: Final = 32
TOKEN_MAX_CHARS: Final = 64
SUBJECT_MAX_CHARS: Final = 160
WORK_MAX_CHARS: Final = 200
REVISION_MAX_CHARS: Final = 80
REFS_MAX_ITEMS: Final = 20
REF_MAX_CHARS: Final = 300
CARD_LIST_MAX_ITEMS: Final = 20
CARD_LINE_MAX_CHARS: Final = 300
CREATED_SKEW_SECONDS: Final = 60
PUBLISH_ATTEMPTS: Final = 5
DIAGNOSTIC_LINES: Final = 20

LIMITS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "NOTE_MAX_BYTES": NOTE_MAX_BYTES,
        "ACK_MAX_BYTES": ACK_MAX_BYTES,
        "CARD_MAX_BYTES": CARD_MAX_BYTES,
        "ID_MAX_CHARS": ID_MAX_CHARS,
        "TOKEN_MAX_CHARS": TOKEN_MAX_CHARS,
        "SUBJECT_MAX_CHARS": SUBJECT_MAX_CHARS,
        "WORK_MAX_CHARS": WORK_MAX_CHARS,
        "REVISION_MAX_CHARS": REVISION_MAX_CHARS,
        "REFS_MAX_ITEMS": REFS_MAX_ITEMS,
        "REF_MAX_CHARS": REF_MAX_CHARS,
        "CARD_LIST_MAX_ITEMS": CARD_LIST_MAX_ITEMS,
        "CARD_LINE_MAX_CHARS": CARD_LINE_MAX_CHARS,
        "CREATED_SKEW_SECONDS": CREATED_SKEW_SECONDS,
        "PUBLISH_ATTEMPTS": PUBLISH_ATTEMPTS,
        "DIAGNOSTIC_LINES": DIAGNOSTIC_LINES,
    }
)

# --- Identifier grammar (docs/protocol.md, "Identifiers") -----------------------------

# The source constants of the identifier grammar are SLUG_SEPARATOR, SLUG_PATTERN,
# TOKEN_PATTERN, LANE_FORBIDDEN_SEGMENT, STAMP_FORMAT, UTC_FORMAT and HEX_CHARS. Every
# other pattern, separator and length bound below is built from them, never spelled
# again, so a change to one reaches all of its uses. tests/test_protocol.py redefines
# SLUG_PATTERN in a fresh copy of this module and checks that its uses follow.

# Joins the segments of a slug, and the parts of note ids and ack file names.
SLUG_SEPARATOR: Final = "-"
_SLUG_SEGMENT: Final = "[a-z0-9]+"
SLUG_PATTERN: Final = f"{_SLUG_SEGMENT}({SLUG_SEPARATOR}{_SLUG_SEGMENT})*"
TOKEN_PATTERN: Final = "[A-Za-z0-9._:-]+"
RECIPIENT_ALL: Final = "all"
RECIPIENT_OPERATOR: Final = "operator"
RESERVED_LANE_IDS: Final = frozenset({RECIPIENT_ALL, RECIPIENT_OPERATOR})
# The word between sender and recipient in a note id. No lane id has it as a
# segment, so "<from>-to-<to>" splits in exactly one place.
LANE_FORBIDDEN_SEGMENT: Final = "to"
# Windows reserved device names are invalid project and lane ids (docs/protocol.md,
# "Identifiers"): they cannot be folders or file stems on native Windows (``nul``
# silently discards writes), so such an id could make a note vanish. The slug grammar
# is lowercase only, so this set covers every letter case.
WINDOWS_DEVICE_NAMES: Final = frozenset(
    {"con", "prn", "aux", "nul"} | {f"com{n}" for n in range(10)} | {f"lpt{n}" for n in range(10)}
)
STAMP_FORMAT: Final = "%Y%m%dT%H%M%SZ"
UTC_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"
HEX_CHARS: Final = 4
NOTE_SUFFIX: Final = ".md"
# Two separators never occur inside a slug, so "<note id>--<reader>" is unambiguous.
ACK_SEPARATOR: Final = SLUG_SEPARATOR * 2

# The strftime directives STAMP_FORMAT and UTC_FORMAT may use, with their digit counts.
_STRFTIME_DIGITS: Final[Mapping[str, int]] = MappingProxyType(
    {"%Y": 4, "%m": 2, "%d": 2, "%H": 2, "%M": 2, "%S": 2}
)


def _strftime_pattern(fmt: str) -> tuple[str, int]:
    """The regex for the fixed-width strftime ``fmt``, and the length it matches.

    Any directive outside ``_STRFTIME_DIGITS`` fails at import (KeyError), so a
    format can never silently lose its regex.
    """
    pattern: list[str] = []
    chars = 0
    for index, part in enumerate(re.split("(%.)", fmt)):
        if index % 2:  # re.split puts the captured directives at odd indexes
            digits = _STRFTIME_DIGITS[part]
            pattern.append(f"[0-9]{{{digits}}}")
            chars += digits
        else:
            pattern.append(re.escape(part))
            chars += len(part)
    return "".join(pattern), chars


def _strftime_shape(fmt: str) -> str:
    """The fixed-width strftime ``fmt`` as docs/protocol.md shows a UTC value.

    Each directive becomes its letter once per digit, upper case: ``YYYY-MM-DD...``.
    """
    return re.sub("%.", lambda match: match[0][1] * _STRFTIME_DIGITS[match[0]], fmt).upper()


def _join_note_id(stamp: str, sender: str, recipient: str, hex_part: str) -> str:
    """The one note id layout: ``<stamp>-<from>-to-<to>-<hex>``.

    It joins real values (``build_note_id``), regex fragments (``_NOTE_ID_RE``) and
    placeholders (``NOTE_ID_SHAPE``, ``_NOTE_ID_MAX_CHARS``). The separator and the
    ``to`` word are plain characters with no meaning in a regex.
    """
    parts = (stamp, sender, LANE_FORBIDDEN_SEGMENT, recipient, hex_part)
    return SLUG_SEPARATOR.join(parts)


_STAMP_PATTERN, _STAMP_CHARS = _strftime_pattern(STAMP_FORMAT)
_UTC_PATTERN, _ = _strftime_pattern(UTC_FORMAT)  # the width is not needed
_HEX_PATTERN: Final = f"[0-9a-f]{{{HEX_CHARS}}}"

_SLUG_RE: Final = re.compile(SLUG_PATTERN)
_TOKEN_RE: Final = re.compile(TOKEN_PATTERN)
_UTC_RE: Final = re.compile(_UTC_PATTERN)
_HEX_RE: Final = re.compile(_HEX_PATTERN)
_NOTE_ID_RE: Final = re.compile(
    _join_note_id(
        f"(?P<stamp>{_STAMP_PATTERN})",
        f"(?P<sender>{SLUG_PATTERN})",
        f"(?P<recipient>{SLUG_PATTERN})",
        f"(?P<hex>{_HEX_PATTERN})",
    )
)
# The longest possible note id; longer input is refused before the regex runs.
_NOTE_ID_MAX_CHARS: Final = len(
    _join_note_id("0" * _STAMP_CHARS, "a" * ID_MAX_CHARS, "a" * ID_MAX_CHARS, "0" * HEX_CHARS)
)
# The note id layout as docs/protocol.md and error messages show it.
NOTE_ID_SHAPE: Final = _join_note_id("<yyyymmddTHHMMSSZ>", "<from>", "<to>", f"<{HEX_CHARS} hex>")
# The UTC value layout (UTC_FORMAT) as docs/protocol.md and error messages show it.
UTC_SHAPE: Final = _strftime_shape(UTC_FORMAT)
# The ack file name layout, as messages show it.
ACK_NAME_SHAPE: Final = f"<note id>{ACK_SEPARATOR}<lane>{NOTE_SUFFIX}"

# --- Note fields (docs/protocol.md, "Note format") ------------------------------------

# The order writers emit. Readers accept any order and ignore unknown keys.
NOTE_FIELDS: Final = (
    "id",
    "created_utc",
    "project",
    "from_project",
    "from",
    "to",
    "subject",
    "kind",
    "needs",
    "work",
    "revision",
    "supersedes",
    "reply_to",
    "idempotency_key",
    "refs",
)
NOTE_OPTIONAL_FIELDS: Final = frozenset(
    {"from_project", "kind", "work", "revision", "supersedes", "idempotency_key"}
)
NOTE_REQUIRED_FIELDS: Final = tuple(f for f in NOTE_FIELDS if f not in NOTE_OPTIONAL_FIELDS)
NOTE_LIST_FIELD: Final = "refs"
HEADER_DELIMITER: Final = "---"
REPLY_TO_NONE: Final = "none"

KINDS: Final = (
    "handoff",
    "change-notice",
    "question",
    "answer",
    "blocker",
    "commit-request",
    "reservation",
    "operator-relay",
    "fyi",
)
NEEDS: Final = ("reply", "ack", "fyi", "operator-decision")

# "Must not start with a quote or bracket" (docs/protocol.md, Note format table).
SUBJECT_FORBIDDEN_FIRST: Final = "\"'`([{<)]}>"

# --- Ack format (docs/protocol.md, "Inbox, read, acknowledge") ------------------------

ACK_FIELDS: Final = ("reader", "read_utc", "session")
ACK_REQUIRED_FIELDS: Final = ("reader", "read_utc")
ACK_RECEIVED: Final = "received"

UTF8_BOM: Final = b"\xef\xbb\xbf"

# --- Header lines (docs/protocol.md, "Note format": a restricted subset of YAML) -----

# The sources of the header-line grammar that notes and acks share. Writers emit
# "<key>: <value>" and "  - <item>" lines; readers allow any HEADER_BLANKS around the
# parts. The regexes, the writers, the shapes that error messages show and every strip
# below are built from these. tests/test_protocol.py narrows HEADER_BLANKS in a fresh
# copy of this module and checks that every strip follows.
HEADER_KEY_PATTERN: Final = "[A-Za-z0-9_][A-Za-z0-9_.-]*"
KEY_VALUE_SEPARATOR: Final = ":"
LIST_ITEM_MARKER: Final = "-"
HEADER_BLANKS: Final = " \t"
_BLANK_CLASS: Final = f"[{re.escape(HEADER_BLANKS)}]"
_KEY_RE: Final = re.compile(f"({HEADER_KEY_PATTERN}){re.escape(KEY_VALUE_SEPARATOR)}(.*)")
_ITEM_RE: Final = re.compile(
    f"{_BLANK_CLASS}*{re.escape(LIST_ITEM_MARKER)}(?:{_BLANK_CLASS}+(.*))?"
)
_LIST_ITEM_PREFIX: Final = f"  {LIST_ITEM_MARKER} "


def _field_line(key: str, value: str) -> str:
    """The one ``key: value`` layout that writers emit."""
    return f"{key}{KEY_VALUE_SEPARATOR} {value}"


# The header line shapes as error messages show them.
_FIELD_LINE_SHAPE: Final = _field_line("key", "value")
_LIST_ITEM_SHAPE: Final = f"{_LIST_ITEM_PREFIX}<text>"

# --- Text that can hide or reorder (docs/protocol.md, "Note format" and "Inbox") ----

# Unicode general categories a one-line value may not hold: controls (Cc: CR, LF,
# tab, ESC), format characters (Cf: zero-width, bidirectional and tag characters,
# the BOM, the soft hyphen), surrogates (Cs: a lone surrogate is not text and has no
# UTF-8 form), and line and paragraph separators (Zl, Zp).
LINE_FORBIDDEN_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
# Printing also escapes private-use (Co) and unassigned (Cn) code points: they have no
# agreed appearance, and a newer terminal may treat an unassigned one as a format
# character. Values may hold them, so whether a note is valid never depends on the
# Unicode version of the reader's Python.
DISPLAY_ESCAPED_CATEGORIES: Final = LINE_FORBIDDEN_CATEGORIES | {"Co", "Cn"}
# Unicode's Default_Ignorable_Code_Point property (DerivedCoreProperties.txt), which
# unicodedata does not expose: code points that render as nothing. Most are Cf; the
# rest are the combining grapheme joiner, Hangul fillers, Khmer inherent vowels,
# Mongolian and other variation selectors, and code points reserved as ignorable.
# Each is treated like a LINE_FORBIDDEN_CATEGORIES character.
_DEFAULT_IGNORABLE_RANGES: Final = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
# Error messages quote at most this many characters of an untrusted value.
_SHOW_MAX_CHARS: Final = 80


class ProtocolError(ValueError):
    """A value, file name or file that breaks protocol v1."""


class OversizeError(ProtocolError):
    """A note or ack that is larger than its byte limit."""


# --- Small helpers --------------------------------------------------------------------


def show(value: str, limit: int = _SHOW_MAX_CHARS) -> str:
    """Quote an untrusted value for an error message: escaped, bounded."""
    text = value if len(value) <= limit else value[:limit] + "..."
    return ascii(text)


def _is_default_ignorable(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _DEFAULT_IGNORABLE_RANGES)


def is_forbidden_in_line(ch: str) -> bool:
    """True for a character a one-line value may not hold.

    Such a character can hide, reorder or break text: LINE_FORBIDDEN_CATEGORIES, or
    any other default-ignorable code point.
    """
    if " " <= ch <= "~":  # printable ASCII, the common case
        return False
    return unicodedata.category(ch) in LINE_FORBIDDEN_CATEGORIES or _is_default_ignorable(ch)


def is_unsafe_char(ch: str) -> bool:
    """True for a character that is only ever printed as an escape.

    Every character ``is_forbidden_in_line`` refuses, plus private-use and unassigned
    code points (DISPLAY_ESCAPED_CATEGORIES).
    """
    if " " <= ch <= "~":
        return False
    return unicodedata.category(ch) in DISPLAY_ESCAPED_CATEGORIES or _is_default_ignorable(ch)


def escape_unsafe(text: str, keep: str = "") -> str:
    """``text`` with each unsafe character (except those in ``keep``) shown as an escape.

    The one rule for printing untrusted text: the CLI applies it to every line it
    writes, to standard output and standard error, and ``store`` to the file names
    in diagnostics. Escapes such as ``\\u200b`` are printable ASCII, so the result
    holds no unsafe character and escaping it again changes nothing.
    """
    return "".join(ascii(ch)[1:-1] if ch not in keep and is_unsafe_char(ch) else ch for ch in text)


def encode_text(text: str, what: str) -> bytes:
    """``text`` as UTF-8. A lone surrogate, which has no UTF-8 form, is a ProtocolError."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        bad = show(text[exc.start])
        raise ProtocolError(f"{what}: character {exc.start + 1} ({bad}) is not text") from None


def format_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(UTC_FORMAT)


def format_stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(STAMP_FORMAT)


def split_lines(text: str) -> list[str]:
    """Split on LF only and drop one trailing CR per line (CRLF tolerance).

    ``str.splitlines`` is deliberately not used: it also splits on form feeds,
    vertical tabs, U+0085 and U+2028, which would let a value smuggle in a line.
    """
    return [line.removesuffix("\r") for line in text.split("\n")]


def decode_text(data: bytes, what: str) -> str:
    """Decode a store file: UTF-8 without a BOM."""
    if data.startswith(UTF8_BOM):
        raise ProtocolError(f"{what}: starts with a UTF-8 BOM (the protocol requires no BOM)")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"{what}: not valid UTF-8 (byte {exc.start})") from None


# --- Field validators -----------------------------------------------------------------


def _check_slug(value: str, field: str, what: str) -> str:
    if not value:
        raise ProtocolError(f"{field}: {what} must not be empty")
    if len(value) > ID_MAX_CHARS:
        raise ProtocolError(
            f"{field}: {what} {show(value)} is longer than {ID_MAX_CHARS} characters"
        )
    if _SLUG_RE.fullmatch(value) is None:
        raise ProtocolError(f"{field}: {what} {show(value)} does not match {SLUG_PATTERN}")
    if value in WINDOWS_DEVICE_NAMES:
        raise ProtocolError(f"{field}: {what} {show(value)} is a reserved Windows device name")
    return value


def check_project_id(value: str, field: str = "project") -> str:
    return _check_slug(value, field, "project id")


def check_lane_id(value: str, field: str = "lane") -> str:
    _check_slug(value, field, "lane id")
    if LANE_FORBIDDEN_SEGMENT in value.split(SLUG_SEPARATOR):
        raise ProtocolError(
            f"{field}: lane id {show(value)} must not have a '{LANE_FORBIDDEN_SEGMENT}' segment"
        )
    if value in RESERVED_LANE_IDS:
        raise ProtocolError(f"{field}: {show(value)} is reserved and cannot be a lane id")
    return value


def check_recipient(value: str, field: str = "to") -> str:
    if value in RESERVED_LANE_IDS:
        return value
    return check_lane_id(value, field)


def check_token(value: str, field: str) -> str:
    if not value:
        raise ProtocolError(f"{field}: token must not be empty")
    if len(value) > TOKEN_MAX_CHARS:
        raise ProtocolError(f"{field}: token is longer than {TOKEN_MAX_CHARS} characters")
    if _TOKEN_RE.fullmatch(value) is None:
        raise ProtocolError(f"{field}: token {show(value)} does not match {TOKEN_PATTERN}")
    return value


def check_line(value: str, field: str, max_chars: int) -> str:
    """One line of text: 1..max_chars characters, with no surrounding spaces.

    No character may hide, reorder or break text (``is_forbidden_in_line``).
    """
    if not value:
        raise ProtocolError(f"{field}: must not be empty")
    if len(value) > max_chars:
        raise ProtocolError(f"{field}: at most {max_chars} characters (got {len(value)})")
    bad = next((i for i, ch in enumerate(value) if is_forbidden_in_line(ch)), None)
    if bad is not None:
        raise ProtocolError(
            f"{field}: character {bad + 1} ({show(value[bad])}) is a control, format, "
            "surrogate or invisible character; a one-line value cannot hold it"
        )
    if value != value.strip(" "):
        raise ProtocolError(f"{field}: must not start or end with a space")
    return value


def check_subject(value: str) -> str:
    check_line(value, "subject", SUBJECT_MAX_CHARS)
    if value[0] in SUBJECT_FORBIDDEN_FIRST:
        raise ProtocolError("subject: must not start with a quote or bracket")
    return value


def check_kind(value: str) -> str:
    if value not in KINDS:
        raise ProtocolError(f"kind: {show(value)} is not one of {', '.join(KINDS)}")
    return value


def check_needs(value: str) -> str:
    if value not in NEEDS:
        raise ProtocolError(f"needs: {show(value)} is not one of {', '.join(NEEDS)}")
    return value


def parse_utc(value: str, field: str) -> datetime:
    if _UTC_RE.fullmatch(value) is None:
        raise ProtocolError(f"{field}: {show(value)} is not {UTC_SHAPE}")
    try:
        return datetime.strptime(value, UTC_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise ProtocolError(f"{field}: {show(value)} is not a valid UTC time") from None


@dataclass(frozen=True)
class NoteId:
    """The parts of a note id. The note header, not the id, is authoritative."""

    value: str
    stamp: datetime
    sender: str
    recipient: str
    hex: str


def parse_note_id(value: str, field: str = "note id") -> NoteId:
    if len(value) > _NOTE_ID_MAX_CHARS:
        raise ProtocolError(f"{field}: {show(value)} is too long to be a note id")
    match = _NOTE_ID_RE.fullmatch(value)
    if match is None:
        raise ProtocolError(f"{field}: {show(value)} is not {NOTE_ID_SHAPE}")
    try:
        stamp = datetime.strptime(match["stamp"], STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise ProtocolError(f"{field}: {show(value)} has an invalid UTC stamp") from None
    check_lane_id(match["sender"], f"{field} sender")
    check_recipient(match["recipient"], f"{field} recipient")
    return NoteId(value, stamp, match["sender"], match["recipient"], match["hex"])


def check_note_id(value: str, field: str = "note id") -> str:
    parse_note_id(value, field)
    return value


def is_note_id(value: str) -> bool:
    try:
        parse_note_id(value)
    except ProtocolError:
        return False
    return True


def check_reply_to(value: str) -> str:
    if value == REPLY_TO_NONE:
        return value
    return check_note_id(value, "reply_to")


def build_note_id(created: datetime, sender: str, recipient: str, hex4: str) -> str:
    if _HEX_RE.fullmatch(hex4) is None:
        raise ProtocolError(f"note id: {show(hex4)} is not {HEX_CHARS} lowercase hex")
    note_id = _join_note_id(format_stamp(created), sender, recipient, hex4)
    return check_note_id(note_id)


# --- Notes ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Note:
    """A validated protocol v1 note. ``from_lane`` is the ``from`` field."""

    id: str
    created_utc: str
    project: str
    from_project: str | None
    from_lane: str
    to: str
    subject: str
    kind: str | None
    needs: str
    work: str | None
    revision: str | None
    supersedes: str | None
    reply_to: str
    idempotency_key: str | None
    refs: tuple[str, ...]
    body: str

    @property
    def sender(self) -> tuple[str, str]:
        """(project, lane) that sent the note; ``from_project`` defaults to ``project``."""
        return (self.from_project or self.project, self.from_lane)

    def header_values(self) -> dict[str, str | None]:
        """Scalar header fields keyed by protocol name (``refs`` is separate)."""
        return {
            "id": self.id,
            "created_utc": self.created_utc,
            "project": self.project,
            "from_project": self.from_project,
            "from": self.from_lane,
            "to": self.to,
            "subject": self.subject,
            "kind": self.kind,
            "needs": self.needs,
            "work": self.work,
            "revision": self.revision,
            "supersedes": self.supersedes,
            "reply_to": self.reply_to,
            "idempotency_key": self.idempotency_key,
        }


def render_header_lines(note: Note) -> list[str]:
    """Header field lines in the protocol's writer order, without the delimiters."""
    values = note.header_values()
    lines: list[str] = []
    for key in NOTE_FIELDS:
        if key == NOTE_LIST_FIELD:
            lines.append(f"{key}{KEY_VALUE_SEPARATOR}")
            lines.extend(f"{_LIST_ITEM_PREFIX}{ref}" for ref in note.refs)
            continue
        value = values[key]
        if value is not None:
            lines.append(_field_line(key, value))
    return lines


def render_note(note: Note) -> str:
    """The note file text: LF line endings, fields in writer order."""
    text = "\n".join([HEADER_DELIMITER, *render_header_lines(note), HEADER_DELIMITER]) + "\n"
    if note.body:
        text += "\n" + note.body + "\n"
    return text


def _parse_header(lines: Sequence[str]) -> tuple[dict[str, str], list[str] | None]:
    values: dict[str, str] = {}
    refs: list[str] | None = None
    current: str | None = None  # last key seen; None for an unknown key
    seen_any = False
    for offset, line in enumerate(lines):
        number = offset + 2  # line 1 is the opening delimiter
        if not line.strip(HEADER_BLANKS):
            continue
        item = _ITEM_RE.fullmatch(line)
        if item is not None:
            if seen_any and current is None:
                continue  # list item of an unknown key
            if current != NOTE_LIST_FIELD or refs is None:
                raise ProtocolError(f"header line {number}: list item outside {NOTE_LIST_FIELD}")
            refs.append((item.group(1) or "").strip(HEADER_BLANKS))
            continue
        if line[0] in HEADER_BLANKS:
            if seen_any and current is None:
                continue  # continuation of an unknown key
            raise ProtocolError(f"header line {number}: unexpected indented line")
        match = _KEY_RE.fullmatch(line)
        if match is None:
            raise ProtocolError(f"header line {number}: not a '{_FIELD_LINE_SHAPE}' line")
        seen_any = True
        key, value = match.group(1), match.group(2).strip(HEADER_BLANKS)
        if key not in NOTE_FIELDS:
            current = None
            continue
        if key in values or (key == NOTE_LIST_FIELD and refs is not None):
            raise ProtocolError(f"header line {number}: duplicate field {key}")
        current = key
        if key == NOTE_LIST_FIELD:
            if value:
                raise ProtocolError(
                    f"{NOTE_LIST_FIELD}: items go on the following '{_LIST_ITEM_SHAPE}' lines"
                )
            refs = []
        else:
            values[key] = value
    return values, refs


def parse_note(
    text: str,
    *,
    file_stem: str | None = None,
    folder_project: str | None = None,
) -> Note:
    """Parse and validate a note. Any field order; unknown keys ignored; CRLF accepted."""
    lines = split_lines(text)
    if lines[0].rstrip(HEADER_BLANKS) != HEADER_DELIMITER:
        raise ProtocolError(f"header: the file must start with a '{HEADER_DELIMITER}' line")
    close = next(
        (i for i in range(1, len(lines)) if lines[i].rstrip(HEADER_BLANKS) == HEADER_DELIMITER),
        None,
    )
    if close is None:
        raise ProtocolError(f"header: no closing '{HEADER_DELIMITER}' line")
    values, refs = _parse_header(lines[1:close])
    missing = [k for k in NOTE_REQUIRED_FIELDS if k not in values and k != NOTE_LIST_FIELD]
    if refs is None:
        missing.append(NOTE_LIST_FIELD)
    if missing or refs is None:
        raise ProtocolError(f"header: missing required field(s): {', '.join(missing)}")
    body = "\n".join(lines[close + 1 :]).strip("\n")
    return _build_note(values, refs, body, file_stem=file_stem, folder_project=folder_project)


def _build_note(
    values: Mapping[str, str],
    refs: Sequence[str],
    body: str,
    *,
    file_stem: str | None,
    folder_project: str | None,
) -> Note:
    note_id = parse_note_id(values["id"], "id")
    if file_stem is not None and note_id.value != file_stem:
        raise ProtocolError(f"id: {show(note_id.value)} does not equal the file name stem")
    created = parse_utc(values["created_utc"], "created_utc")
    skew = abs((created - note_id.stamp).total_seconds())
    if skew > CREATED_SKEW_SECONDS:
        raise ProtocolError(
            f"created_utc: {int(skew)} s from the id's stamp (limit {CREATED_SKEW_SECONDS} s)"
        )
    project = check_project_id(values["project"], "project")
    if folder_project is not None and project != folder_project:
        raise ProtocolError(f"project: {show(project)} does not match its folder")
    from_project = values.get("from_project")
    if from_project is not None:
        check_project_id(from_project, "from_project")
    from_lane = check_lane_id(values["from"], "from")
    to = check_recipient(values["to"], "to")
    if note_id.sender != from_lane or note_id.recipient != to:
        raise ProtocolError(
            "id: the sender or recipient it names does not match the from and to fields"
        )
    subject = check_subject(values["subject"])
    kind = values.get("kind")
    if kind is not None:
        check_kind(kind)
    needs = check_needs(values["needs"])
    work = values.get("work")
    if work is not None:
        check_line(work, "work", WORK_MAX_CHARS)
    revision = values.get("revision")
    if revision is not None:
        check_line(revision, "revision", REVISION_MAX_CHARS)
    supersedes = values.get("supersedes")
    if supersedes is not None:
        check_note_id(supersedes, "supersedes")
        if supersedes == note_id.value:
            raise ProtocolError("supersedes: a note cannot supersede itself")
    reply_to = check_reply_to(values["reply_to"])
    idempotency_key = values.get("idempotency_key")
    if idempotency_key is not None:
        check_token(idempotency_key, "idempotency_key")
    check_refs(refs)
    return Note(
        id=note_id.value,
        created_utc=values["created_utc"],
        project=project,
        from_project=from_project,
        from_lane=from_lane,
        to=to,
        subject=subject,
        kind=kind,
        needs=needs,
        work=work,
        revision=revision,
        supersedes=supersedes,
        reply_to=reply_to,
        idempotency_key=idempotency_key,
        refs=tuple(refs),
        body=body,
    )


def check_refs(refs: Sequence[str]) -> Sequence[str]:
    if len(refs) > REFS_MAX_ITEMS:
        raise ProtocolError(f"refs: at most {REFS_MAX_ITEMS} entries (got {len(refs)})")
    for index, ref in enumerate(refs, start=1):
        check_line(ref, f"refs entry {index}", REF_MAX_CHARS)
    return refs


def parse_note_bytes(
    data: bytes,
    *,
    file_stem: str | None = None,
    folder_project: str | None = None,
) -> Note:
    if len(data) > NOTE_MAX_BYTES:
        raise OversizeError(f"note: {len(data)} bytes (limit {NOTE_MAX_BYTES})")
    text = decode_text(data, "note")
    return parse_note(text, file_stem=file_stem, folder_project=folder_project)


@dataclass(frozen=True)
class Draft:
    """Validated input for a new note, before its id and time are chosen."""

    project: str
    from_project: str | None
    from_lane: str
    to: str
    subject: str
    kind: str
    needs: str
    work: str | None
    revision: str | None
    supersedes: str | None
    reply_to: str
    idempotency_key: str | None
    refs: tuple[str, ...]

    @property
    def sender(self) -> tuple[str, str]:
        return (self.from_project or self.project, self.from_lane)


def make_draft(
    *,
    project: str,
    from_lane: str,
    to: str,
    subject: str,
    kind: str,
    needs: str,
    from_project: str | None = None,
    work: str | None = None,
    revision: str | None = None,
    supersedes: str | None = None,
    reply_to: str = REPLY_TO_NONE,
    idempotency_key: str | None = None,
    refs: Iterable[str] = (),
) -> Draft:
    """Validate every field of a note to post. Raises ProtocolError on the first bad one.

    Each value is checked on its own before any rendering, so no value can inject a
    header line.
    """
    check_project_id(project, "project")
    if from_project is not None:
        check_project_id(from_project, "from_project")
        if from_project == project:
            from_project = None  # only cross-project notes carry from_project
    check_lane_id(from_lane, "from")
    check_recipient(to, "to")
    check_subject(subject)
    check_kind(kind)
    check_needs(needs)
    if work is not None:
        check_line(work, "work", WORK_MAX_CHARS)
    if revision is not None:
        check_line(revision, "revision", REVISION_MAX_CHARS)
    if supersedes is not None:
        check_note_id(supersedes, "supersedes")
    check_reply_to(reply_to)
    if idempotency_key is not None:
        check_token(idempotency_key, "idempotency_key")
    ref_tuple = tuple(refs)
    check_refs(ref_tuple)
    return Draft(
        project=project,
        from_project=from_project,
        from_lane=from_lane,
        to=to,
        subject=subject,
        kind=kind,
        needs=needs,
        work=work,
        revision=revision,
        supersedes=supersedes,
        reply_to=reply_to,
        idempotency_key=idempotency_key,
        refs=ref_tuple,
    )


def normalize_body(body: str) -> str:
    """LF line endings (CRLF and lone CR become LF), no leading or trailing blank lines."""
    normalized = body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if len(encode_text(normalized, "body")) > NOTE_MAX_BYTES:
        raise ProtocolError(f"body: larger than the note limit of {NOTE_MAX_BYTES} bytes")
    return normalized


def make_note(draft: Draft, body: str, created: datetime, hex4: str) -> tuple[Note, bytes]:
    """Give a draft its id and time; return the note and its exact file bytes.

    The rendered bytes are parsed back with the reader's full validation, so the
    writer can only emit files that readers accept.
    """
    created = created.astimezone(UTC).replace(microsecond=0)
    note_id = build_note_id(created, draft.from_lane, draft.to, hex4)
    if draft.supersedes == note_id:
        raise ProtocolError("supersedes: a note cannot supersede itself")
    note = Note(
        id=note_id,
        created_utc=format_utc(created),
        project=draft.project,
        from_project=draft.from_project,
        from_lane=draft.from_lane,
        to=draft.to,
        subject=draft.subject,
        kind=draft.kind,
        needs=draft.needs,
        work=draft.work,
        revision=draft.revision,
        supersedes=draft.supersedes,
        reply_to=draft.reply_to,
        idempotency_key=draft.idempotency_key,
        refs=draft.refs,
        body=normalize_body(body),
    )
    data = encode_text(render_note(note), "note")
    if len(data) > NOTE_MAX_BYTES:
        raise ProtocolError(f"note: {len(data)} bytes after rendering (limit {NOTE_MAX_BYTES})")
    reparsed = parse_note_bytes(data, file_stem=note_id, folder_project=draft.project)
    if reparsed != note:
        raise ProtocolError("note: rendering did not round-trip")  # defensive; never expected
    return note, data


# --- Addressing and supersedes (docs/protocol.md, "Inbox" and "Supersedes") -----------


def is_addressed_to(note: Note, project: str, lane: str) -> bool:
    """True when the note belongs in ``lane``'s inbox in ``project``.

    That is a note ``to`` the lane, or ``to: all`` that the lane did not send.
    """
    if note.project != project:
        return False
    if note.to == lane:
        return True
    return note.to == RECIPIENT_ALL and note.sender != (project, lane)


@dataclass(frozen=True)
class SupersedesResult:
    hidden: frozenset[str]
    ignored: tuple[tuple[str, str], ...]  # (superseding note id, reason)


def resolve_supersedes(notes: Iterable[Note]) -> SupersedesResult:
    """Apply ``supersedes`` only between notes of the same sender.

    A ``supersedes`` naming another sender's note, or a note that is missing (or
    invalid), is ignored and returned as a diagnostic.
    """
    by_id = {note.id: note for note in notes}
    hidden: set[str] = set()
    ignored: list[tuple[str, str]] = []
    for note in sorted(by_id.values(), key=lambda n: n.id):
        if note.supersedes is None:
            continue
        target = by_id.get(note.supersedes)
        if target is None:
            ignored.append(
                (note.id, f"supersedes {note.supersedes}, which is missing or invalid; ignored")
            )
        elif target.sender != note.sender:
            ignored.append(
                (note.id, f"supersedes {target.id}, a note from another sender; ignored")
            )
        else:
            hidden.add(target.id)
    return SupersedesResult(frozenset(hidden), tuple(ignored))


# --- Acks -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Ack:
    reader: str
    read_utc: str
    session: str | None = None


def ack_file_name(note_id: str, reader: str) -> str:
    check_note_id(note_id)
    check_lane_id(reader, "lane")
    return f"{note_id}{ACK_SEPARATOR}{reader}{NOTE_SUFFIX}"


def parse_ack_file_name(name: str) -> tuple[str, str]:
    """(note id, reader) of an ack file named ``<note id>--<reader>.md``.

    Like every ``parse_*`` function it raises ProtocolError for an invalid name and
    never returns None.
    """
    stem = name.removesuffix(NOTE_SUFFIX)
    parts = stem.split(ACK_SEPARATOR)
    if stem == name or len(parts) != 2:
        raise ProtocolError(f"ack file name: {show(name)} is not {ACK_NAME_SHAPE}")
    note_id, reader = parts
    check_note_id(note_id, "ack file name note id")
    check_lane_id(reader, "ack file name reader")
    return note_id, reader


def make_ack(reader: str, read: datetime, session: str | None = None) -> Ack:
    check_lane_id(reader, "lane")
    if session is not None:
        check_token(session, "session")
    return Ack(reader=reader, read_utc=format_utc(read), session=session)


def render_ack(ack: Ack) -> str:
    """The ack file text: fields in ACK_FIELDS order, then the ``received`` line."""
    values = {"reader": ack.reader, "read_utc": ack.read_utc, "session": ack.session}
    lines = [_field_line(key, value) for key in ACK_FIELDS if (value := values[key]) is not None]
    lines.append(ACK_RECEIVED)
    text = "\n".join(lines) + "\n"
    if len(encode_text(text, "ack")) > ACK_MAX_BYTES:
        raise ProtocolError(f"ack: larger than {ACK_MAX_BYTES} bytes")  # cannot happen
    return text


def parse_ack(text: str, *, file_reader: str | None = None) -> Ack:
    """Parse an ack. Blank lines and CRLF are tolerated; unknown keys are ignored."""
    lines = [line for line in split_lines(text) if line.strip(HEADER_BLANKS)]
    if not lines or lines[-1].strip(HEADER_BLANKS) != ACK_RECEIVED:
        raise ProtocolError(f"ack: the last line must be '{ACK_RECEIVED}'")
    values: dict[str, str] = {}
    for line in lines[:-1]:
        match = _KEY_RE.fullmatch(line)
        if match is None:
            raise ProtocolError(f"ack: {show(line)} is not a '{_FIELD_LINE_SHAPE}' line")
        key, value = match.group(1), match.group(2).strip(HEADER_BLANKS)
        if key not in ACK_FIELDS:
            continue
        if key in values:
            raise ProtocolError(f"ack: duplicate field {key}")
        values[key] = value
    missing = [k for k in ACK_REQUIRED_FIELDS if k not in values]
    if missing:
        raise ProtocolError(f"ack: missing required field(s): {', '.join(missing)}")
    reader = check_lane_id(values["reader"], "reader")
    if file_reader is not None and reader != file_reader:
        raise ProtocolError("ack: reader does not match the file name")
    parse_utc(values["read_utc"], "read_utc")
    session = values.get("session")
    if session is not None:
        check_token(session, "session")
    return Ack(reader=reader, read_utc=values["read_utc"], session=session)


def parse_ack_bytes(data: bytes, *, file_reader: str | None = None) -> Ack:
    if len(data) > ACK_MAX_BYTES:
        raise OversizeError(f"ack: {len(data)} bytes (limit {ACK_MAX_BYTES})")
    return parse_ack(decode_text(data, "ack"), file_reader=file_reader)
