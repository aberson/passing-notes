# KNOWN-BAD REFERENCE - do not use, do not fix.
# A frozen copy of `git show b812a49:skills/lane-status/scripts/plan_progress.py`, the
# superseded candidate whose document-level masking (`_without_inline_code`) pairs
# backticks across blocks (review findings F1/F2, 2026-11-22). It exists only as the
# red-on-garbage anchor in tests/test_plan_progress.py: the review repros must FAIL
# against it. Nothing imports it from production code.

"""Read a bounded Markdown plan and project explicit step-status evidence as JSON.

This helper is intentionally independent of Passing Notes runtime storage.  It never
infers completion: a step is complete only when its own status marker says ``DONE``.
"""

from __future__ import annotations

import argparse
import bisect
import ctypes
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Final

MAX_BYTES: Final = 1024 * 1024
MAX_STEPS: Final = 256
MAX_LABEL: Final = 240
MAX_TITLE: Final = 120

STEP = re.compile(r"^### Step ([1-9][0-9]{0,5}):[ \t]+(.+)$")
STATUS_PREFIX: Final = "- **Status:**"
STATUS = re.compile(
    r"^(DONE|IN PROGRESS|PENDING|BLOCKED|WAITING)(?: \([^()\r\n]{1,200}\))?$",
    re.IGNORECASE | re.ASCII,
)
BACKTICK_FENCE = re.compile(r"^ {0,3}(`{3,})[^`]*$")
TILDE_FENCE = re.compile(r"^ {0,3}(~{3,}).*$")
SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/ -]{0,239}$")


class PlanError(Exception):
    """The source cannot provide reliable bounded plan evidence."""


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
    if status is None:
        return "unknown"
    match = STATUS.fullmatch(status)
    if match is None:
        return "unknown"
    token = match.group(1).upper()
    if token == "DONE":
        return "completed"
    if token == "IN PROGRESS":
        return "in_progress"
    return "remaining"


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


def _without_inline_code(text: str) -> str:
    """Mask closed CommonMark backtick spans across the bounded document."""
    masked = list(text)
    runs: list[tuple[int, int]] = []
    by_length: dict[int, list[int]] = {}
    position = 0
    while position < len(text):
        if text[position] != "`":
            position += 1
            continue
        backslashes = 0
        before = position - 1
        while before >= 0 and text[before] == "\\":
            backslashes += 1
            before -= 1
        if backslashes % 2:
            position += 1
            continue
        end = position
        while end < len(text) and text[end] == "`":
            end += 1
        runs.append((position, end - position))
        by_length.setdefault(end - position, []).append(len(runs) - 1)
        position = end
    index = 0
    while index + 1 < len(runs):
        opening, length = runs[index]
        same_length = by_length[length]
        next_position = bisect.bisect_right(same_length, index)
        if next_position >= len(same_length):
            index += 1
            continue
        closing_index = same_length[next_position]
        closing, _ = runs[closing_index]
        for offset in range(opening, closing + length):
            if masked[offset] not in ("\r", "\n"):
                masked[offset] = " "
        index = closing_index + 1
    return "".join(masked)


def _comment_state(line: str, commented: bool) -> tuple[bool, bool]:
    """Return comment state after the line and whether any comment text was present."""
    position = 0
    touched = commented
    while position < len(line):
        if commented:
            close = line.find("-->", position)
            if close < 0:
                return True, True
            commented = False
            position = close + 3
            continue
        opening = line.find("<!--", position)
        if opening < 0:
            break
        touched = True
        commented = True
        position = opening + 4
    return commented, touched


def parse_plan(text: str, source: str) -> dict[str, object]:
    steps: list[dict[str, object]] = []
    seen: set[int] = set()
    current: dict[str, object] | None = None
    fenced: tuple[str, int] | None = None
    commented = False

    lines = _markdown_lines(text)
    comment_lines = _markdown_lines(_without_inline_code(text))
    for line, comment_line in zip(lines, comment_lines, strict=True):
        if fenced is not None:
            if _closes_fence(line, fenced):
                fenced = None
            continue
        if commented:
            commented, _ = _comment_state(line, commented)
            current = None
            continue
        fence = _opens_fence(line)
        if fence is not None:
            current = None
            fenced = fence
            continue
        commented, comment_text = _comment_state(comment_line, commented)
        if comment_text:
            current = None
            continue
        heading = STEP.match(line)
        if heading:
            number = int(heading.group(1))
            if number in seen:
                raise PlanError(f"duplicate Step {number}")
            if len(steps) >= MAX_STEPS:
                raise PlanError(f"more than {MAX_STEPS} steps")
            seen.add(number)
            current = {
                "number": number,
                "title": _ascii(heading.group(2), MAX_TITLE),
                "status": None,
            }
            steps.append(current)
            continue
        if line.startswith("### Step "):
            raise PlanError("malformed Step heading")
        if current is None or not line.strip():
            continue
        if line.startswith(STATUS_PREFIX):
            if current["status"] is not None:
                raise PlanError(f"Step {current['number']} has more than one status marker")
            raw = line[len(STATUS_PREFIX) :].strip(" \t")
            current["status_raw"] = raw
            current["status"] = _ascii(raw, MAX_TITLE)
            continue
        current = None

    if not steps:
        raise PlanError("no Step headings")

    counts = {"completed": 0, "in_progress": 0, "remaining": 0, "unknown": 0}
    projected: list[dict[str, object]] = []
    for step in steps:
        status_value = step.pop("status_raw", None)
        classification = _classification(status_value if isinstance(status_value, str) else None)
        counts[classification] += 1
        projected.append({**step, "classification": classification})

    def selected(kind: str, limit: int) -> list[dict[str, object]]:
        return [step for step in projected if step["classification"] == kind][:limit]

    return {
        "schema": "passing-notes.plan-progress/1",
        "source": safe_label(source),
        "format": "markdown-step-status-v1",
        **counts,
        "in_progress_steps": selected("in_progress", 3),
        "next_steps": selected("remaining", 3),
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


def _bounded_bytes(path: Path, root: Path | None) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
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
        text = _bounded_bytes(resolved, resolved_root).decode("utf-8")
    except UnicodeDecodeError:
        raise PlanError("source-not-utf8") from None
    return parse_plan(text, label)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--plan", required=True, type=Path)
    result.add_argument("--source", required=True)
    result.add_argument("--root", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = read_plan(args.plan, args.source, args.root)
    except PlanError as exc:
        print(f"plan-progress-unavailable: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
