"""Read a bounded Markdown plan and project explicit step-status evidence as JSON.

This helper is intentionally independent of Passing Notes runtime storage.  It never
infers completion: a step is complete only when its own status marker says ``DONE`` or
``PASS``.  The counts are the plan's own declarations, not verified completion.

Format ``markdown-step-status-v2`` (Step 30, D90-D93):

* A step heading is ``### Step <id>:`` and a title, where ``<id>`` is up to four
  letters, an optional dot and one to three dot-separated numbers (``24``, ``M7``,
  ``EI.3``, ``V2.1``).  Any other ``### Step `` heading is skipped and counted in
  ``warnings.skipped_headings``; a duplicate id keeps the first step and is counted in
  ``warnings.duplicate_ids``.  Either one closes the open step.
* A step's marker is the first visible line starting with ``- **Status:**`` or
  ``**Status:**`` before the next ATX heading; later ones count in
  ``warnings.extra_markers``.  Its leading word decides the class (``STATUS_WORDS``).
* ``next_steps`` is the frontier: in-progress or remaining steps after the anchor, the
  completed step with the latest ``YYYY-MM-DD`` date in its status text (the later one
  on a tie), else the last completed step, else the plan start.  ``older_open`` counts
  remaining steps before the anchor.  ``in_progress_steps`` lists in-progress steps
  anywhere in the plan.  Every list holds at most three steps, in plan order.

The scan is block-first (D72), line by line, with no Markdown parser: backtick and
tilde fences and HTML comments are found before any inline-code masking, and inline
code is masked only inside one paragraph line run, never across a blank line or a
block boundary.  Only visible text is evidence: a heading or marker inside a fence, a
comment or a code span never counts.  A fence or comment block still open at the end
of the plan, or a comment reopened past its block's closing line, makes the whole
result unavailable (the skill reports ``unknown``) rather than a partial count.
"""

from __future__ import annotations

import argparse
import bisect
import ctypes
import json
import os
import re
import stat
import string
import sys
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

MAX_BYTES = 1024 * 1024
MAX_STEPS = 256
MAX_LABEL = 240
MAX_TITLE = 120

MAX_LISTED = 3

FORMAT = "markdown-step-status-v2"
SCHEMA = "passing-notes.plan-progress/2"

STEP_ID = r"[A-Za-z]{0,4}\.?[0-9]{1,6}(?:\.[0-9]{1,6}){0,2}"
STEP = re.compile(rf"^### Step ({STEP_ID}):[ \t]+(.+)$")
STEP_PREFIX = "### Step "
STATUS_PREFIXES = ("- **Status:**", "**Status:**")
# The leading word of a status marker and its class (D91).
STATUS_WORDS = {
    "DONE": "completed",
    "PASS": "completed",
    "IN PROGRESS": "in_progress",
    "PENDING": "remaining",
    "PLANNED": "remaining",
    "TODO": "remaining",
    "BLOCKED": "held",
    "WAITING": "held",
    "HELD": "held",
    "PARKED": "held",
    "DEFERRED": "held",
}
CLASSES = ("completed", "in_progress", "remaining", "held", "unknown")
WARNINGS = ("skipped_headings", "duplicate_ids", "extra_markers")
# The word ends the text or is followed by a space, "(", "-", an em dash, ":", ";" or ",".
STATUS = re.compile(
    "(" + "|".join(re.escape(word) for word in STATUS_WORDS) + ")(?=\\Z|[ (\\-\u2014:;,])",
    re.IGNORECASE | re.ASCII,
)
DATE = re.compile(r"(?<![0-9])([0-9]{4})-([0-9]{2})-([0-9]{2})(?![0-9])")
BACKTICK_FENCE = re.compile(r"^ {0,3}(`{3,})[^`]*$")
TILDE_FENCE = re.compile(r"^ {0,3}(~{3,}).*$")
SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/ -]{0,239}$")
BOM = "\ufeff"

# Lines that start a new block, so they end a paragraph line run (CommonMark lets each
# of these interrupt a paragraph).  A ``<!--`` at the start of a line, or at the start
# of a list item's or block quote's content, is an HTML comment block.
COMMENT_BLOCK = re.compile(r"^(?: {0,3}(?:>[ \t]?|(?:[-+*]|[0-9]{1,9}[.)])[ \t]+))* {0,3}<!--")
ATX_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
LIST_ITEM = re.compile(r"^ {0,3}(?:[-+*]|[0-9]{1,9}[.)])(?:[ \t]|$)")
BLOCK_QUOTE = re.compile(r"^ {0,3}>")
THEMATIC_BREAK = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
BACKTICK_RUN = re.compile(r"`+")
ESCAPABLE = frozenset(string.punctuation)
COMMENT_OPEN = "<!--"
COMMENT_CLOSE = "-->"
# The close is searched from two characters after the opener, so ``<!-->`` and
# ``<!--->`` are complete comments, as in CommonMark.
CLOSE_OFFSET = 2

VISIBLE = 0
CODE = 1
COMMENT = 2


class PlanError(Exception):
    """The source cannot provide reliable bounded plan evidence."""


@dataclass(frozen=True)
class Step:
    """One ``### Step <id>:`` heading and the raw text of its first status marker."""

    id: str
    title: str
    status_raw: str | None = None


def _ascii(value: str, limit: int) -> str:
    out: list[str] = []
    used = 0
    for char in value:
        code = ord(char)
        if char == "\n":
            token = r"\n"
        elif char == "\r":
            token = r"\r"
        elif char == "\t":
            token = r"\t"
        elif code < 0x20 or code == 0x7F:
            token = rf"\x{code:02x}"
        elif code > 0x7E:
            token = rf"\u{code:04x}" if code <= 0xFFFF else rf"\U{code:08x}"
        else:
            token = char
        if used + len(token) > limit:
            break
        out.append(token)
        used += len(token)
    return "".join(out)


def _classification(status: str | None) -> str:
    """The class of a status text by its leading word (D91); anything else is unknown."""
    if status is None:
        return "unknown"
    match = STATUS.match(status)
    if match is None:
        return "unknown"
    return STATUS_WORDS[match.group(1).upper()]


def _latest_date(status: str | None) -> str | None:
    """The latest valid ``YYYY-MM-DD`` date in a status text, as an ISO string."""
    latest: str | None = None
    for match in DATE.finditer(status or ""):
        year, month, day = (int(part) for part in match.groups())
        try:
            value = date(year, month, day).isoformat()
        except ValueError:
            continue
        if latest is None or value > latest:
            latest = value
    return latest


def safe_label(value: str) -> str:
    """Return one inert repository-relative provenance label, unchanged."""
    if not SAFE_LABEL.fullmatch(value) or "\\" in value or ":" in value:
        raise PlanError("source-label-invalid")
    parts = value.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise PlanError("source-label-invalid")
    return value


def _markdown_lines(text: str) -> list[str]:
    """Markdown physical lines: only CR, LF and CRLF are separators."""
    return re.split(r"\r\n|\n|\r", text)


def _closes_fence(line: str, fenced: tuple[str, int]) -> bool:
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3 or not stripped.startswith(fenced[0] * fenced[1]):
        return False
    marker_length = len(stripped) - len(stripped.lstrip(fenced[0]))
    return marker_length >= fenced[1] and not stripped[marker_length:].strip(" \t")


def _opens_fence(line: str) -> tuple[str, int] | None:
    match = BACKTICK_FENCE.fullmatch(line) or TILDE_FENCE.fullmatch(line)
    if match is None:
        return None
    marker = match.group(1)
    return marker[0], len(marker)


def _starts_block(line: str) -> bool:
    """Whether a nonblank line starts a block and so ends any paragraph line run."""
    return bool(
        _opens_fence(line) is not None
        or COMMENT_BLOCK.match(line)
        or ATX_HEADING.match(line)
        or LIST_ITEM.match(line)
        or BLOCK_QUOTE.match(line)
        or THEMATIC_BREAK.match(line)
    )


def _block_comment_line(line: str, commented: bool) -> tuple[bool, bool]:
    """Scan one line of an HTML comment block raw, with no inline-code masking.

    Used inside a comment and on a line-initial comment block, where backticks have no
    meaning.  Returns the comment state after the line and whether any text outside a
    comment was present.  A comment that closes on this line and then reopens without
    closing again cannot be read safely, so it raises ``ambiguous-comment``.
    """
    position = 0
    visible = False
    closed = False
    while True:
        if commented:
            close = line.find(COMMENT_CLOSE, position)
            if close < 0:
                if closed:
                    raise PlanError("ambiguous-comment")
                return True, visible
            commented = False
            closed = True
            position = close + len(COMMENT_CLOSE)
            continue
        opening = line.find(COMMENT_OPEN, position)
        chunk = line[position:] if opening < 0 else line[position:opening]
        if chunk.strip():
            visible = True
        if opening < 0:
            return False, visible
        commented = True
        position = opening + CLOSE_OFFSET


def _inline_kinds(text: str) -> list[int]:
    """Classify each character of one paragraph line run as visible, code or comment.

    Left to right, as CommonMark reads inline content: a backslash escapes the next
    ASCII punctuation character; a backtick run opens a code span only when a run of
    exactly the same length closes it later in this run (otherwise it is literal);
    ``<!--`` opens a comment only when a ``-->`` closes it later in this run (otherwise
    it is literal text).  Nothing carries past the end of the run.
    """
    kinds = [VISIBLE] * len(text)
    runs = [(match.start(), match.end() - match.start()) for match in BACKTICK_RUN.finditer(text)]
    starts_by_length: dict[int, list[int]] = {}
    for start, length in runs:
        starts_by_length.setdefault(length, []).append(start)
    position = 0
    while position < len(text):
        char = text[position]
        if char == "\\" and position + 1 < len(text) and text[position + 1] in ESCAPABLE:
            position += 2
            continue
        if char == "`":
            end = position
            while end < len(text) and text[end] == "`":
                end += 1
            length = end - position
            candidates = starts_by_length.get(length, [])
            index = bisect.bisect_left(candidates, end)
            if index < len(candidates):
                closing = candidates[index] + length
                for offset in range(position, closing):
                    kinds[offset] = CODE
                position = closing
            else:
                position = end
            continue
        if text.startswith(COMMENT_OPEN, position):
            close = text.find(COMMENT_CLOSE, position + CLOSE_OFFSET)
            if close < 0:
                position += len(COMMENT_OPEN)
                continue
            stop = close + len(COMMENT_CLOSE)
            for offset in range(position, stop):
                kinds[offset] = COMMENT
            position = stop
            continue
        position += 1
    return kinds


@dataclass(frozen=True)
class _LineView:
    """How one line of a paragraph run reads once code spans and comments are known."""

    touched: bool  # a comment covers part of the line
    visible: bool  # the line has text outside comments
    lead: int  # length of the line's leading run of plain (not code, not comment) text


def _run_line_views(lines: list[str]) -> list[_LineView]:
    """Per line of one paragraph run: comment contact, visible text and plain lead."""
    kinds = _inline_kinds("\n".join(lines))
    views: list[_LineView] = []
    offset = 0
    for line in lines:
        touched = False
        visible = False
        lead = 0
        plain = True
        for index, char in enumerate(line):
            kind = kinds[offset + index]
            if kind == COMMENT:
                touched = True
            elif not char.isspace():
                visible = True
            if kind != VISIBLE:
                plain = False
            elif plain:
                lead += 1
        views.append(_LineView(touched, visible, lead))
        offset += len(line) + 1
    return views


class _Steps:
    """Collect ``### Step <id>:`` headings and each step's first status marker (D90/D91)."""

    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.seen: set[str] = set()
        self.open = False  # whether the last step still reads markers (until a heading)
        self.warnings = dict.fromkeys(WARNINGS, 0)

    def heading(self, line: str, readable: bool) -> None:
        """Read one visible ATX heading line; ``readable`` is false when a comment
        touches it, so it may close a step but never open one."""
        self.open = False
        match = STEP.match(line) if readable else None
        if match is None:
            if line.startswith(STEP_PREFIX):
                self.warnings["skipped_headings"] += 1
            return
        step_id = match.group(1)
        if step_id in self.seen:
            self.warnings["duplicate_ids"] += 1
            return
        if len(self.steps) >= MAX_STEPS:
            raise PlanError(f"more than {MAX_STEPS} steps")
        self.seen.add(step_id)
        self.steps.append(Step(step_id, _ascii(match.group(2), MAX_TITLE)))
        self.open = True

    def read(self, line: str, lead: int) -> None:
        """Read one visible non-heading line; only its first ``lead`` characters are
        plain text, so a marker that starts inside a code span never counts."""
        if not self.open:
            return
        for prefix in STATUS_PREFIXES:
            if line.startswith(prefix) and lead >= len(prefix):
                step = self.steps[-1]
                if step.status_raw is not None:
                    self.warnings["extra_markers"] += 1
                    return
                raw = line[len(prefix) :].strip(" \t")
                self.steps[-1] = replace(step, status_raw=raw)
                return


def _scan(text: str) -> _Steps:
    """Block-first scan: fences and comments first, then inline code per paragraph run.

    Fences and comment blocks hide their lines but do not end a step: only a visible
    ATX heading does (D91), so a marker after a fenced sample still belongs to its step.
    """
    collected = _Steps()
    lines = _markdown_lines(text)
    fenced: tuple[str, int] | None = None
    commented = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if fenced is not None:
            if _closes_fence(line, fenced):
                fenced = None
            index += 1
            continue
        if commented:
            commented, _ = _block_comment_line(line, True)
            index += 1
            continue
        fence = _opens_fence(line)
        if fence is not None:
            fenced = fence
            index += 1
            continue
        if COMMENT_BLOCK.match(line):
            commented, _ = _block_comment_line(line, False)
            index += 1
            continue
        if not line.strip():
            index += 1
            continue
        end = index + 1
        if not ATX_HEADING.match(line):
            while end < len(lines) and lines[end].strip() and not _starts_block(lines[end]):
                end += 1
        run = lines[index:end]
        for run_line, view in zip(run, _run_line_views(run), strict=True):
            if ATX_HEADING.match(run_line):
                collected.heading(run_line, readable=not view.touched)
            elif not view.touched:
                collected.read(run_line, view.lead)
        index = end
    if fenced is not None:
        raise PlanError("unterminated-fence")
    if commented:
        raise PlanError("unterminated-comment")
    return collected


def _anchor(steps: list[Step], classes: list[str]) -> int:
    """The frontier anchor's index (D92), or -1 for the plan start.

    The completed step with the latest ``YYYY-MM-DD`` date in its status text, the
    later one in plan order on a tie; else the last completed step; else the start.
    """
    anchor = -1
    anchor_date: str | None = None
    for index, (step, classification) in enumerate(zip(steps, classes, strict=True)):
        if classification != "completed":
            continue
        latest = _latest_date(step.status_raw)
        if latest is not None and (anchor_date is None or latest >= anchor_date):
            anchor, anchor_date = index, latest
        elif anchor_date is None:
            anchor = index
    return anchor


def parse_plan(text: str, source: str) -> dict[str, object]:
    label = safe_label(source)
    collected = _scan(text.removeprefix(BOM))  # one leading byte-order mark, as utf-8-sig
    steps = collected.steps
    if not steps:
        raise PlanError("no Step headings")

    counts = dict.fromkeys(CLASSES, 0)
    classes: list[str] = []
    projected: list[dict[str, object]] = []
    for step in steps:
        classification = _classification(step.status_raw)
        counts[classification] += 1
        classes.append(classification)
        projected.append(
            {
                "id": step.id,
                "title": step.title,
                "status": None if step.status_raw is None else _ascii(step.status_raw, MAX_TITLE),
                "classification": classification,
            }
        )

    anchor = _anchor(steps, classes)
    in_progress = [row for row in projected if row["classification"] == "in_progress"]
    after = [
        row
        for row in projected[anchor + 1 :]
        if row["classification"] in ("in_progress", "remaining")
    ]
    older = [row for row in projected[: max(anchor, 0)] if row["classification"] == "remaining"]
    return {
        "schema": SCHEMA,
        "source": label,
        "format": FORMAT,
        **counts,
        "in_progress_steps": in_progress[:MAX_LISTED],
        "next_steps": after[:MAX_LISTED],
        "older_open": len(older),
        "older_open_steps": older[:MAX_LISTED],
        "warnings": dict(collected.warnings),
    }


def _within(path: Path, root: Path) -> bool:
    try:
        normalized_root = os.path.normcase(root)
        return os.path.commonpath((os.path.normcase(path), normalized_root)) == normalized_root
    except ValueError:
        return False


def _resolved_plan(path: Path, root: Path | None) -> tuple[Path, Path | None]:
    try:
        resolved_root = root.resolve(strict=True) if root is not None else None
        candidate = path if path.is_absolute() or resolved_root is None else resolved_root / path
        lexical = Path(os.path.abspath(candidate))
        if resolved_root is not None and not _within(lexical, resolved_root):
            raise PlanError("source-outside-root")
        if resolved_root is not None:
            lexical_relative = lexical.relative_to(resolved_root)
            if any(":" in part for part in lexical_relative.parts):
                raise PlanError("source-outside-root")
        resolved = candidate.resolve(strict=True)
        # An early, friendly refusal only; _bounded_bytes re-checks the opened handle.
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise PlanError("source-not-regular")
        if resolved_root is not None:
            if not _within(resolved, resolved_root):
                raise PlanError("source-outside-root")
            relative = resolved.relative_to(resolved_root)
            if any(":" in part for part in relative.parts):
                raise PlanError("source-outside-root")
    except PlanError:
        raise
    except (OSError, ValueError):
        raise PlanError("source-unavailable") from None
    return resolved, resolved_root


def _final_path(descriptor: int) -> Path:
    """Resolve the object actually opened, not only its pre-open pathname."""
    if os.name == "nt":
        import msvcrt

        handle = msvcrt.get_osfhandle(descriptor)
        size = 32768
        buffer = ctypes.create_unicode_buffer(size)
        length = ctypes.windll.kernel32.GetFinalPathNameByHandleW(handle, buffer, size, 0)
        if not length or length >= size:
            raise PlanError("source-handle-unverified")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)
    for folder in ("/proc/self/fd", "/dev/fd"):
        try:
            return Path(os.readlink(Path(folder) / str(descriptor)))
        except OSError:
            continue
    raise PlanError("source-handle-unverified")


# O_NONBLOCK keeps a FIFO swapped in after the path check from blocking the open; the
# descriptor is then checked with fstat before any read.  Windows has no such flag.
OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)


def _bounded_bytes(path: Path, root: Path | None) -> bytes:
    try:
        descriptor = os.open(path, OPEN_FLAGS)
    except OSError:
        raise PlanError("source-unavailable") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PlanError("source-not-regular")
        opened = _final_path(descriptor)
        if root is not None and not _within(opened, root):
            raise PlanError("source-outside-root")
        chunks: list[bytes] = []
        remaining = MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    except OSError:
        raise PlanError("source-unavailable") from None
    finally:
        os.close(descriptor)
    if len(data) > MAX_BYTES:
        raise PlanError("source-too-large")
    return data


def read_plan(path: Path, source: str, root: Path | None = None) -> dict[str, object]:
    label = safe_label(source)
    resolved, resolved_root = _resolved_plan(path, root)
    try:
        # parse_plan drops one leading byte-order mark (Windows editors write one).
        text = _bounded_bytes(resolved, resolved_root).decode("utf-8")
    except UnicodeDecodeError:
        raise PlanError("source-not-utf8") from None
    return parse_plan(text, label)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args(argv)
    try:
        result = read_plan(args.plan, args.source, args.root)
    except PlanError as exc:
        print(f"plan-progress-unavailable: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
