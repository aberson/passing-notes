"""Strict, read-only plan progress projection used by the lane-status skill.

Step 25 (D72) made the scan block-first; Step 30 (D90-D93) moved it to schema
``passing-notes.plan-progress/2`` and format ``markdown-step-status-v2``: tolerant step
ids, the status marker anywhere in a step, more status words, a ``held`` count,
warnings instead of whole-file refusal for heading shape, and a frontier for "next".

The review repros of the superseded candidate ``b812a49`` (findings F1-F3, 2026-11-22)
run through the script's CLI. The same F1/F2 cases also run against a frozen copy of
that candidate (``known_bad_plan_progress_b812a49.py``) and must fail there: the
red-on-garbage anchor that proves these tests can catch the known-bad masking.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/lane-status/scripts/plan_progress.py"
KNOWN_BAD = Path(__file__).resolve().with_name("known_bad_plan_progress_b812a49.py")
REAL_PLAN = "docs/plans/master_plan.md"

SCHEMA = "passing-notes.plan-progress/2"
FORMAT = "markdown-step-status-v2"
COUNT_KEYS = ("completed", "in_progress", "remaining", "held", "unknown")
WARNING_KEYS = ("skipped_headings", "duplicate_ids", "extra_markers")
TOP_KEYS = (
    "schema",
    "source",
    "format",
    *COUNT_KEYS,
    "in_progress_steps",
    "next_steps",
    "older_open",
    "older_open_steps",
    "warnings",
)
STEP_KEYS = ("id", "title", "status", "classification")
# Which classifications each step list may hold (D92).
LIST_CLASSES = {
    "in_progress_steps": {"in_progress"},
    "next_steps": {"in_progress", "remaining"},
    "older_open_steps": {"remaining"},
}


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lane_status_plan_progress", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plan_progress = load_module()


@dataclass(frozen=True)
class Outcome:
    code: int
    result: dict[str, object] | None
    stderr: str


def run_cli(
    script: Path, plan: Path, source: str, root: Path | None = None, cwd: Path | None = None
) -> Outcome:
    """Run a plan-progress script as the skill does: a separate isolated process."""
    argv = [sys.executable, "-I", str(script), "--plan", str(plan), "--source", source]
    if root is not None:
        argv += ["--root", str(root)]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60, cwd=cwd, check=False)
    result = json.loads(done.stdout) if done.returncode == 0 else None
    return Outcome(done.returncode, result, done.stderr)


def run_text(script: Path, text: str, tmp_path: Path) -> Outcome:
    """Write a synthetic plan byte-exact under a root and read it with --plan/--source/--root."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "plan.md").write_bytes(text.encode("utf-8"))
    return run_cli(script, Path("docs/plan.md"), "docs/plan.md", root=repo, cwd=tmp_path)


@dataclass(frozen=True)
class Repro:
    """One synthetic plan and the counts, step lists and warnings it must give."""

    name: str
    text: str
    counts: tuple[int, int, int, int, int]  # completed, in_progress, remaining, held, unknown
    next_steps: tuple[str, ...] = ()
    in_progress_steps: tuple[str, ...] = ()
    older_open_steps: tuple[str, ...] = ()
    older_open: int | None = None  # defaults to len(older_open_steps)
    warnings: tuple[int, int, int] = (0, 0, 0)  # skipped_headings, duplicate_ids, extra_markers
    # What b812a49's known-bad masking was observed to give: exit code and its v1 counts
    # (completed, in_progress, remaining, unknown), or exit code and its stderr reason.
    # Only the F1/F2 anchor cases set it.
    known_bad: tuple[int, tuple[int, int, int, int] | str] | None = None


def v1_counts_of(outcome: Outcome) -> tuple[int, int, int, int]:
    """The known-bad v1 script's own four counts."""
    result = outcome.result
    assert result is not None, outcome.stderr
    completed, in_progress, remaining, unknown = (
        int(str(result[key])) for key in ("completed", "in_progress", "remaining", "unknown")
    )
    return completed, in_progress, remaining, unknown


def assert_expected(outcome: Outcome, repro: Repro) -> None:
    """Every v2 key, count, step list and warning, compared with ``.get`` so that a v1
    result (the known-bad anchor) fails with an AssertionError, never a KeyError."""
    assert outcome.code == 0, outcome.stderr
    result = outcome.result
    assert result is not None
    counts = tuple(result.get(key) for key in COUNT_KEYS)
    assert counts == repro.counts, (repro.name, result)
    for key, ids in (
        ("next_steps", repro.next_steps),
        ("in_progress_steps", repro.in_progress_steps),
        ("older_open_steps", repro.older_open_steps),
    ):
        rows = result.get(key)
        assert isinstance(rows, list), (repro.name, key, result)
        assert tuple(row.get("id") for row in rows) == ids, (repro.name, key, result)
        for row in rows:
            assert tuple(row) == STEP_KEYS, (repro.name, key, row)
            assert row["classification"] in LIST_CLASSES[key], (repro.name, key, row)
    older_open = len(repro.older_open_steps) if repro.older_open is None else repro.older_open
    assert result.get("older_open") == older_open, (repro.name, result)
    assert result.get("warnings") == dict(zip(WARNING_KEYS, repro.warnings, strict=True)), (
        repro.name,
        result,
    )
    assert tuple(result) == TOP_KEYS, (repro.name, result)
    assert result["source"] == "docs/plan.md"
    assert result["format"] == FORMAT
    assert result["schema"] == SCHEMA


# The review's repros, verbatim (b812a49-review.md, F1 A-D and F2).
F1_F2 = (
    Repro(
        "F1-A stray backtick in prose two paragraphs apart",
        "Use a ` tick\n\n<!--\n### Step 1: fake\n- **Status:** DONE\n-->\n"
        "### Step 2: real\n- **Status:** PENDING\n\nend ` tick\n",
        (0, 0, 1, 0, 0),
        next_steps=("2",),
        known_bad=(0, (1, 0, 1, 0)),
    ),
    Repro(
        "F1-B backtick inside a tilde fence",
        "### Step 1: a\n- **Status:** PENDING\n~~~\nx `\n~~~\n<!-- hidden\n"
        "### Step 9: fake\n- **Status:** DONE\n-->\nfoo ` \n",
        (0, 0, 1, 0, 0),
        next_steps=("1",),
        known_bad=(0, (1, 0, 1, 0)),
    ),
    Repro(
        "F1-C single tick in a fenced sample, then a comment",
        "~~~\n`\n~~~\n<!--\n### Step 1: fake\n-->\n### Step 2: real\n- **Status:** PENDING\n`\n",
        (0, 0, 1, 0, 0),
        next_steps=("2",),
        known_bad=(0, (0, 0, 1, 1)),
    ),
    Repro(
        "F1-D span crossing a line-initial comment block",
        "text `a\n<!--\n### Step 1: fake\n- **Status:** DONE\n-->\nb` \n"
        "### Step 2: r\n- **Status:** DONE\n",
        (1, 0, 0, 0, 0),
        known_bad=(0, (2, 0, 0, 0)),
    ),
    Repro(
        "F2 backtick inside a real comment, later span",
        "<!-- ` -->\n### Step 1: a\n- **Status:** DONE\n\nlater `x`\n",
        (1, 0, 0, 0, 0),
        known_bad=(3, "no Step headings"),
    ),
    Repro(
        "F2 backtick inside two real comments",
        "<!-- ` -->\n### Step 1: a\n- **Status:** DONE\n<!-- ` -->\n",
        (1, 0, 0, 0, 0),
        known_bad=(3, "no Step headings"),
    ),
)

OTHER_REPROS = (
    Repro(
        "F3 comment line between a heading and its status",
        "### Step 1: a\n<!-- c -->\n- **Status:** DONE\n",
        (1, 0, 0, 0, 0),
    ),
    Repro(
        "F3 multi-line comment between a heading and its status",
        "### Step 1: a\n<!--\nnote ` with a tick\n-->\n- **Status:** IN PROGRESS\n",
        (0, 1, 0, 0, 0),
        next_steps=("1",),
        in_progress_steps=("1",),
    ),
    Repro(
        "a status inside a comment under a real heading is not evidence",
        "### Step 1: a\n<!--\n- **Status:** DONE\n-->\n",
        (0, 0, 0, 0, 1),
    ),
    Repro(
        "CRLF plan with a stray tick and a commented fake step",
        "Use a ` tick\r\n\r\n<!--\r\n### Step 1: fake\r\n- **Status:** DONE\r\n-->\r\n"
        "### Step 2: real\r\n- **Status:** PENDING\r\n",
        (0, 0, 1, 0, 0),
        next_steps=("2",),
    ),
    Repro(
        "a code span may cross lines inside one paragraph run",
        "Show `the literal <!--\nmarker` here.\n### Step 1: real\n- **Status:** DONE\n",
        (1, 0, 0, 0, 0),
    ),
    Repro(
        "a span never pairs across a list item boundary",
        "- one ` tick\n- <!-- `\n### Step 9: fake\n- **Status:** DONE\n-->\n"
        "### Step 1: real\n- **Status:** PENDING\n",
        (0, 0, 1, 0, 0),
        next_steps=("1",),
    ),
)


ROUND_TWO = (
    Repro(
        "a byte-order mark before the first heading",
        "\ufeff### Step 1: a\n- **Status:** DONE\n### Step 2: b\n- **Status:** PENDING\n",
        (1, 0, 1, 0, 0),
        next_steps=("2",),
    ),
    Repro(
        "a byte-order mark before a fence",
        "\ufeff```\n### Step 1: fake\n- **Status:** DONE\n```\n"
        "### Step 2: real\n- **Status:** PENDING\n",
        (0, 0, 1, 0, 0),
        next_steps=("2",),
    ),
    Repro(
        "an unclosed inline comment is literal text",
        "### Step 1: a\n- **Status:** DONE\n\nProse with a stray <!-- marker.\n"
        "### Step 2: b\n- **Status:** PENDING\n### Step 3: c\n- **Status:** PENDING\n",
        (1, 0, 2, 0, 0),
        next_steps=("2", "3"),
    ),
    Repro(
        "a 4-space-indented comment opener is literal",
        "### Step 1: a\n- **Status:** DONE\n\n    <!-- indented code\n\n"
        "### Step 2: b\n- **Status:** PENDING\n",
        (1, 0, 1, 0, 0),
        next_steps=("2",),
    ),
    Repro(
        "<!--> is a complete comment",
        "### Step 1: a\n- **Status:** DONE\n<!-->\n### Step 2: b\n- **Status:** PENDING\n"
        "Inline <!--> too.\n### Step 3: c\n- **Status:** PENDING\n",
        (1, 0, 2, 0, 0),
        next_steps=("2", "3"),
    ),
    Repro(
        "<!---> is a complete comment",
        "### Step 1: a\n- **Status:** DONE\n<!--->\n### Step 2: b\n- **Status:** PENDING\n",
        (1, 0, 1, 0, 0),
        next_steps=("2",),
    ),
    Repro(
        "a 4-space-indented fence is not a fence",
        "    ```\n### Step 1: real\n- **Status:** PENDING\n    ```\n",
        (0, 0, 1, 0, 0),
        next_steps=("1",),
    ),
    Repro(
        "a shorter marker does not close a fence",
        "````\n```\n### Step 9: fake\n- **Status:** DONE\n````\n"
        "### Step 1: real\n- **Status:** PENDING\n",
        (0, 0, 1, 0, 0),
        next_steps=("1",),
    ),
)


# Step 30 (D90-D93): the plans the workspace actually writes.
V2_REPROS = (
    Repro(
        "D90 a letter id counts and a malformed heading is skipped, not fatal",
        "### Step 1: a\n- **Status:** DONE\n\n### Step M1: b\n- **Status:** PENDING\n\n"
        "### Step 2 - draft\n- **Status:** DONE\n",
        (1, 0, 1, 0, 0),
        next_steps=("M1",),
        warnings=(1, 0, 0),
    ),
    Repro(
        "D90 the id shapes the workspace writes",
        "### Step 24: a\n- **Status:** PENDING\n### Step M7: b\n- **Status:** PENDING\n"
        "### Step EI.3: c\n- **Status:** PENDING\n### Step V2.1: d\n- **Status:** PENDING\n"
        "### Step 1.2.3: e\n- **Status:** PENDING\n### Step ABCD.123456: f\n",
        (0, 0, 5, 0, 1),
        next_steps=("24", "M7", "EI.3"),
    ),
    Repro(
        "D90 ids outside the shape are skipped and close the open step",
        "### Step 1: a\n### Step ABCDE1: five letters\n- **Status:** DONE\n"
        "### Step 1.2.3.4: four numbers\n- **Status:** DONE\n"
        "### Step 1234567: seven digits\n- **Status:** DONE\n"
        "### Step 2:no space\n- **Status:** DONE\n### Step 3:\n- **Status:** DONE\n"
        "### Step 4: commented <!-- note -->\n- **Status:** DONE\n",
        (0, 0, 0, 0, 1),
        warnings=(6, 0, 0),
    ),
    Repro(
        "D90 a duplicate id keeps the first and ignores the duplicate's marker",
        "### Step 3: first\n- **Status:** PENDING\n### Step 3: second\n- **Status:** DONE\n",
        (0, 0, 1, 0, 0),
        next_steps=("3",),
        warnings=(0, 1, 0),
    ),
    Repro(
        "D90 a duplicate Step 3 counts one duplicate",
        "### Step 1: a\n- **Status:** DONE\n### Step 3: b\n- **Status:** DONE\n"
        "### Step 3: again\n- **Status:** PENDING\n",
        (2, 0, 0, 0, 0),
        warnings=(0, 1, 0),
    ),
    Repro(
        "D91 a PLANNED marker as the step's last bullet counts as remaining",
        "### Step 1: a\n- **Problem:** p\n- **Type:** code\n- **Done when:**\n  - one\n"
        "  - two\n- **Depends on:** none\n- **Status:** PLANNED\n",
        (0, 0, 1, 0, 0),
        next_steps=("1",),
    ),
    Repro(
        "D91 a bare **Status:** marker after prose and a fence counts",
        "### Step 1: a\nProse first.\n\n```text\n- **Status:** DONE\n```\n\n**Status:** TODO\n",
        (0, 0, 1, 0, 0),
        next_steps=("1",),
    ),
    Repro(
        "D91 later markers count in extra_markers and never change the class",
        "### Step 1: a\n- **Status:** DONE\n- **Status:** PENDING\n\n**Status:** TODO\n"
        "### Step 2: b\n- **Status:** PENDING\n",
        (1, 0, 1, 0, 0),
        next_steps=("2",),
        warnings=(0, 0, 2),
    ),
    Repro(
        "D91 any ATX heading ends the step's marker search",
        "### Step 1: a\n#### Notes\n- **Status:** DONE\n### Step 2: b\n## Part\n**Status:** DONE\n",
        (0, 0, 0, 0, 2),
    ),
    Repro(
        "D91 a marker inside a code span crossing lines never counts",
        "### Step 1: a\nSee `the sample\n**Status:** DONE` here.\n",
        (0, 0, 0, 0, 1),
    ),
    Repro(
        "D91 HELD counts as held, never remaining or next",
        "### Step 1: a\n- **Status:** DONE\n### Step 2: b\n- **Status:** HELD (main)\n"
        "### Step 3: c\n- **Status:** PENDING\n",
        (1, 0, 1, 1, 0),
        next_steps=("3",),
    ),
    Repro(
        "D91 PASS with a date counts as completed",
        "### Step 1: a\n- **Status:** PASS (2026-11-02)\n### Step 2: b\n- **Status:** TODO\n",
        (1, 0, 1, 0, 0),
        next_steps=("2",),
    ),
    Repro(
        "D91 the leading word decides: an em dash and a semicolon",
        "### Step 1: a\n- **Status:** DONE \u2014 merged\n"
        "### Step 2: b\n- **Status:** PLANNED; draft\n",
        (1, 0, 1, 0, 0),
        next_steps=("2",),
    ),
    Repro(
        "D92 the frontier follows the latest-dated DONE, not plan order",
        "### Step 3: c\n- **Status:** PENDING\n### Step 5: e\n- **Status:** DONE (2026-11-22)\n"
        "### Step 4: d\n- **Status:** PENDING\n",
        (1, 0, 2, 0, 0),
        next_steps=("4",),
        older_open_steps=("3",),
    ),
    Repro(
        "D92 two DONE steps with the same date anchor on the later one",
        "### Step 1: a\n- **Status:** PENDING\n### Step 2: b\n- **Status:** DONE (2026-11-22)\n"
        "### Step 3: c\n- **Status:** PENDING\n### Step 4: d\n- **Status:** DONE (2026-11-22)\n"
        "### Step 5: e\n- **Status:** PENDING\n",
        (2, 0, 3, 0, 0),
        next_steps=("5",),
        older_open_steps=("1", "3"),
    ),
    Repro(
        "D92 a later date anchors even when it comes earlier in the plan",
        "### Step 1: a\n- **Status:** DONE (2026-11-20)\n### Step 2: b\n- **Status:** PENDING\n"
        "### Step 3: c\n- **Status:** DONE (2026-11-23)\n### Step 4: d\n- **Status:** PENDING\n"
        "### Step 5: e\n- **Status:** DONE (2026-11-21)\n### Step 6: f\n- **Status:** PENDING\n",
        (3, 0, 3, 0, 0),
        next_steps=("4", "6"),
        older_open_steps=("2",),
    ),
    Repro(
        "D92 any dated DONE beats an undated later DONE; an impossible date is no date",
        "### Step 1: a\n- **Status:** DONE (2026-11-01)\n### Step 2: b\n- **Status:** PENDING\n"
        "### Step 3: c\n- **Status:** DONE (2026-13-45)\n### Step 4: d\n- **Status:** PENDING\n",
        (2, 0, 2, 0, 0),
        next_steps=("2", "4"),
    ),
    Repro(
        "D92 without a date the anchor is the last DONE in plan order",
        "### Step 1: a\n- **Status:** PENDING\n### Step 2: b\n- **Status:** DONE\n"
        "### Step 3: c\n- **Status:** PENDING\n### Step 4: d\n- **Status:** DONE\n"
        "### Step 5: e\n- **Status:** IN PROGRESS\n",
        (2, 1, 2, 0, 0),
        next_steps=("5",),
        in_progress_steps=("5",),
        older_open_steps=("1", "3"),
    ),
    Repro(
        "D92 without a DONE the anchor is the plan start",
        "### Step 1: a\n- **Status:** BLOCKED\n### Step 2: b\n- **Status:** PENDING\n"
        "### Step 3: c\n- **Status:** IN PROGRESS\n### Step 4: d\n- **Status:** PENDING\n"
        "### Step 5: e\n- **Status:** TODO\n",
        (0, 1, 3, 1, 0),
        next_steps=("2", "3", "4"),
        in_progress_steps=("3",),
    ),
    Repro(
        "D92 older open counts every remaining step before the anchor, lists three",
        "### Step 1: a\n- **Status:** PENDING\n### Step 2: b\n- **Status:** IN PROGRESS\n"
        "### Step 3: c\n- **Status:** PENDING\n### Step 4: d\n- **Status:** WAITING\n"
        "### Step 5: e\n- **Status:** PENDING\n### Step 6: f\n- **Status:** PENDING\n"
        "### Step 7: g\n- **Status:** DONE (2026-11-22)\n",
        (1, 1, 4, 1, 0),
        in_progress_steps=("2",),
        older_open_steps=("1", "3", "5"),
        older_open=4,
    ),
)


@pytest.mark.parametrize(
    "text, reason",
    [
        pytest.param(
            "### Step 1: a\n- **Status:** DONE\n```\n### Step 2: b\n- **Status:** DONE\n"
            "### Step 3: c\n- **Status:** PENDING\n",
            "unterminated-fence",
            id="unterminated-backtick-fence",
        ),
        pytest.param(
            "### Step 1: a\n- **Status:** DONE\n~~~~\n~~~\n### Step 2: b\n",
            "unterminated-fence",
            id="unterminated-tilde-fence-shorter-close",
        ),
        pytest.param(
            "### Step 1: a\n- **Status:** DONE\n```\n~~~\n### Step 2: b\n",
            "unterminated-fence",
            id="unterminated-fence-other-character-close",
        ),
        pytest.param(
            "### Step 1: a\n- **Status:** DONE\n<!--\n### Step 2: b\n- **Status:** DONE\n"
            "### Step 3: c\n- **Status:** PENDING\n",
            "unterminated-comment",
            id="unterminated-comment",
        ),
        pytest.param(
            "### Step 1: a\n<!-- note\n<!-- still open\n",
            "unterminated-comment",
            id="unterminated-comment-with-nested-opener",
        ),
        pytest.param(
            "### Step 1: a\n- **Status:** DONE\n<!-- one --> <!-- reopened\n"
            "### Step 2: b\n-->\n### Step 3: c\n- **Status:** PENDING\n",
            "ambiguous-comment",
            id="comment-reopened-past-its-block-line",
        ),
        pytest.param(
            "<!--\n### Step 9: fake\nend --> <!-- reopened\n-->\n### Step 1: a\n",
            "ambiguous-comment",
            id="comment-reopened-on-its-closing-line",
        ),
        pytest.param(
            "### Step X: no digits\n- **Status:** DONE\n### Step 1 - draft\n",
            "no Step headings",
            id="every-heading-skipped",
        ),
    ],
)
def test_open_block_at_eof_or_ambiguous_comment_is_unknown_not_partial(
    text: str, reason: str, tmp_path: Path
) -> None:
    """A fence or comment block still open at the end, a comment reopened past its
    block's line, or a plan whose every heading is skipped gives no counts at all (the
    skill shows unknown)."""
    outcome = run_text(SCRIPT, text, tmp_path)
    assert (outcome.code, outcome.result) == (3, None)
    assert outcome.stderr == f"plan-progress-unavailable: {reason}\n"


@pytest.mark.parametrize(
    "repro", F1_F2 + OTHER_REPROS + ROUND_TWO + V2_REPROS, ids=lambda r: r.name
)
def test_review_repros_through_the_cli(repro: Repro, tmp_path: Path) -> None:
    assert_expected(run_text(SCRIPT, repro.text, tmp_path), repro)


@pytest.mark.parametrize("repro", F1_F2, ids=lambda r: r.name)
def test_red_on_garbage_the_known_bad_masking_fails_the_same_cases(
    repro: Repro, tmp_path: Path
) -> None:
    """The anchor: b812a49's document-level masking gets every F1/F2 case wrong, in the
    specific way the review observed (a commented-out step counted, or a real step lost)."""
    outcome = run_text(KNOWN_BAD, repro.text, tmp_path)
    assert repro.known_bad is not None
    code, observed = repro.known_bad
    assert outcome.code == code, outcome.stderr
    if isinstance(observed, str):
        assert outcome.stderr == f"plan-progress-unavailable: {observed}\n"
    else:
        assert v1_counts_of(outcome) == observed
        # The known-bad counts are wrong in themselves, not only in their v1 keys.
        expected = repro.counts
        assert observed != (expected[0], expected[1], expected[2], expected[4])
    with pytest.raises(AssertionError):
        assert_expected(outcome, repro)


def test_known_bad_reference_is_the_document_level_masking() -> None:
    text = KNOWN_BAD.read_text(encoding="utf-8")
    assert text.startswith("# KNOWN-BAD REFERENCE")
    assert '"""Mask closed CommonMark backtick spans across the bounded document."""' in text
    assert "comment_lines = _markdown_lines(_without_inline_code(text))" in text
    assert "_without_inline_code" not in SCRIPT.read_text(encoding="utf-8")


FAKE = "### Step 9: fake\n- **Status:** DONE\n"


@pytest.mark.parametrize(
    "wrapped",
    [
        pytest.param(f"```text\n{FAKE}```\n", id="backtick-fenced"),
        pytest.param(f"~~~\n{FAKE}~~~\n", id="tilde-fenced"),
        pytest.param(f"````md\n```\n{FAKE}```\n````\n", id="nested-backtick-fence"),
        pytest.param(f"<!--\n{FAKE}-->\n", id="commented"),
        pytest.param(
            f"<!-- one line --> <!-- closed again -->\n<!--\n{FAKE}-->\n", id="two-comments"
        ),
        pytest.param(f"- <!-- comment in a list item\n{FAKE}-->\n", id="list-item-comment"),
        pytest.param(f"```\n-->\n{FAKE}```\n", id="close-marker-inside-fence"),
        pytest.param(f"```\n~~~\n{FAKE}```\n", id="other-character-does-not-close"),
        pytest.param(
            "Use `### Step 9: fake` here.\n`### Step 8: fake`\n`- **Status:** DONE`\n",
            id="inline-code",
        ),
    ],
)
def test_fenced_commented_or_inline_code_step_never_counts(wrapped: str, tmp_path: Path) -> None:
    text = wrapped + "\n### Step 1: real\n- **Status:** PENDING\n"
    repro = Repro("wrapped", text, (0, 0, 1, 0, 0), next_steps=("1",))
    assert_expected(run_text(SCRIPT, text, tmp_path), repro)


@pytest.mark.parametrize(
    "status, classification",
    [
        ("DONE", "completed"),
        ("done (2026-11-01)", "completed"),
        ("DONE-ish", "completed"),
        ("DONE: merged", "completed"),
        ("DONE, merged", "completed"),
        ("DONE merged", "completed"),
        ("PASS", "completed"),
        ("IN PROGRESS (lane x)", "in_progress"),
        ("PENDING", "remaining"),
        ("PLANNED", "remaining"),
        ("TODO: later", "remaining"),
        ("BLOCKED", "held"),
        ("WAITING (main)", "held"),
        ("HELD", "held"),
        ("PARKED", "held"),
        ("DEFERRED - next phase", "held"),
        ("DONEish", "unknown"),
        ("DONE.", "unknown"),
        ("DONE/merged", "unknown"),
        ("DONE\tmerged", "unknown"),
        ("IN-PROGRESS", "unknown"),
        ("CANCELLED", "unknown"),
        ("**DONE**", "unknown"),
        ("", "unknown"),
    ],
)
def test_the_leading_status_word_decides_the_class(status: str, classification: str) -> None:
    result = plan_progress.parse_plan(f"### Step 1: a\n- **Status:** {status}\n", "synthetic.md")
    counts = {key: result[key] for key in COUNT_KEYS}
    assert counts == {key: int(key == classification) for key in COUNT_KEYS}, status


def _first_marker_class(text: str) -> str:
    """The class of a status text by its leading word, written apart from the script."""
    words = (
        ("IN PROGRESS", "in_progress"),
        ("DONE", "completed"),
        ("PASS", "completed"),
        ("PENDING", "remaining"),
        ("PLANNED", "remaining"),
        ("TODO", "remaining"),
        ("BLOCKED", "held"),
        ("WAITING", "held"),
        ("HELD", "held"),
        ("PARKED", "held"),
        ("DEFERRED", "held"),
    )
    upper = text.upper()
    for word, kind in words:
        follower = upper[len(word) : len(word) + 1]
        if upper.startswith(word) and (not follower or follower in " (-\u2014:;,"):
            return kind
    return "unknown"


def _independent_counts(text: str) -> dict[str, int]:
    """Count steps by class under the v2 rules, line by line, without the script's code.

    This is the plan's own line-by-line count (Step 25, criterion 3; Step 30 moved it to
    D90/D91). It shares no code with the scanner but is deliberately cruder: it ignores
    inline code, mid-line comments and paragraph runs. Agreement therefore proves the
    script reads this repository's real plan as a plain line count does; it does not prove
    the scanner correct on shapes the real plan lacks. The synthetic repros above pin those.
    """
    step_id = r"[A-Za-z]{0,4}\.?[0-9]{1,6}(?:\.[0-9]{1,6}){0,2}"
    counts = dict.fromkeys(COUNT_KEYS, 0)
    counts.update(dict.fromkeys(WARNING_KEYS, 0))
    seen: set[str] = set()
    current: str | None = None  # the open step's class, "" until its first marker
    fence: tuple[str, int] | None = None
    comment = False

    def close() -> None:
        nonlocal current
        if current is not None:
            counts[current or "unknown"] += 1
        current = None

    for line in text.splitlines():
        stripped = line.lstrip(" ")
        if fence is not None:
            marker = stripped[: len(stripped) - len(stripped.lstrip(fence[0]))]
            if len(marker) >= fence[1] and not stripped[len(marker) :].strip():
                fence = None
            continue
        if comment:
            comment = "-->" not in line
            continue
        opener = re.match(r"(`{3,}|~{3,})", stripped)
        if opener and len(line) - len(stripped) <= 3:
            fence = (opener.group(1)[0], len(opener.group(1)))
            continue
        if stripped.startswith("<!--"):
            comment = "-->" not in stripped[4:]
            continue
        if re.match(r" {0,3}#{1,6}(?:[ \t]|$)", line):
            close()
            heading = re.fullmatch(rf"### Step ({step_id}):[ \t]+(.+)", line)
            if heading and heading.group(1) not in seen:
                seen.add(heading.group(1))
                current = ""
            elif heading:
                counts["duplicate_ids"] += 1
            elif line.startswith("### Step "):
                counts["skipped_headings"] += 1
            continue
        for prefix in ("- **Status:**", "**Status:**"):
            if current is not None and line.startswith(prefix):
                if current:
                    counts["extra_markers"] += 1
                else:
                    current = _first_marker_class(line[len(prefix) :].strip(" \t"))
                break
    close()
    return counts


def test_real_plan_counts_match_an_independent_line_count() -> None:
    plan = (ROOT / REAL_PLAN).read_text(encoding="utf-8")
    expected = _independent_counts(plan)
    assert sum(expected[key] for key in COUNT_KEYS) > 0 and expected["completed"] > 0
    outcome = run_cli(SCRIPT, Path(REAL_PLAN), REAL_PLAN, root=ROOT, cwd=ROOT)
    assert outcome.code == 0, outcome.stderr
    result = outcome.result
    assert result is not None and result["source"] == REAL_PLAN
    assert result["schema"] == SCHEMA and result["format"] == FORMAT
    observed = {key: result[key] for key in COUNT_KEYS}
    warnings = result["warnings"]
    assert isinstance(warnings, dict)
    observed.update(warnings)
    assert observed == expected


def test_counts_only_explicit_recognized_step_statuses() -> None:
    result = plan_progress.parse_plan(
        """# Synthetic plan

### Step 1: Shipped slice
- **Status:** DONE (2030-01-01)

### Step 2: Active slice
- **Status:** IN PROGRESS

### Step 3: Queued slice
- **Status:** PENDING

### Step 4: Missing status is not completion

### Step 5: Explicit but unsupported status
- **Status:** CANCELLED

### Step M6: Parked slice
- **Problem:** waits on main
- **Status:** PARKED (main)
""",
        "docs/plans/synthetic.md",
    )
    assert result == {
        "schema": "passing-notes.plan-progress/2",
        "source": "docs/plans/synthetic.md",
        "format": "markdown-step-status-v2",
        "completed": 1,
        "in_progress": 1,
        "remaining": 1,
        "held": 1,
        "unknown": 2,
        "in_progress_steps": [
            {
                "id": "2",
                "title": "Active slice",
                "status": "IN PROGRESS",
                "classification": "in_progress",
            }
        ],
        "next_steps": [
            {
                "id": "2",
                "title": "Active slice",
                "status": "IN PROGRESS",
                "classification": "in_progress",
            },
            {
                "id": "3",
                "title": "Queued slice",
                "status": "PENDING",
                "classification": "remaining",
            },
        ],
        "older_open": 0,
        "older_open_steps": [],
        "warnings": {"skipped_headings": 0, "duplicate_ids": 0, "extra_markers": 0},
    }


def test_held_variants_are_held_and_output_is_bounded_ascii() -> None:
    steps = []
    for number in range(1, 7):
        steps.append(
            f"### Step {number}: Synthetic | `title` \u4e2d {number}\n"
            f"- **Status:** {'BLOCKED (synthetic)' if number == 1 else 'WAITING'}"
        )
    steps.append("### Step 7: Last \u4e2d\n- **Status:** PENDING \u2014 \u4e2d")
    result = plan_progress.parse_plan("\n\n".join(steps), "synthetic/plan.md")
    assert (result["held"], result["remaining"]) == (6, 1)
    (row,) = result["next_steps"]
    assert result["source"] == "synthetic/plan.md"
    assert row["id"] == "7" and row["title"].isascii() and row["status"].isascii()
    assert row["status"] == "PENDING \\u2014 \\u4e2d"


def test_a_duplicate_keeps_the_first_title() -> None:
    result = plan_progress.parse_plan(
        "### Step 3: First\n- **Status:** PENDING\n### Step 3: Second\n- **Status:** DONE\n",
        "synthetic.md",
    )
    assert [(row["id"], row["title"]) for row in result["next_steps"]] == [("3", "First")]
    assert result["warnings"] == {"skipped_headings": 0, "duplicate_ids": 1, "extra_markers": 0}


@pytest.mark.parametrize(
    "text, reason",
    [
        ("# no steps\n", "no Step headings"),
        ("### Step 1234567: Too many digits\n", "no Step headings"),
    ],
)
def test_plans_without_a_readable_step_are_rejected(text: str, reason: str) -> None:
    with pytest.raises(plan_progress.PlanError, match=reason):
        plan_progress.parse_plan(text, "synthetic.md")


def test_fenced_or_unrelated_markers_never_supply_completion() -> None:
    result = plan_progress.parse_plan(
        """### Step 1: Missing marker

```text
```still-inside-the-fence
- **Status:** DONE
### Step 88: Sample only
- **Status:** DONE
```

### Step 2: Also missing
## Example statuses
- **Status:** DONE

<!--
### Step 89: Commented sample
- **Status:** DONE
<!-- an opener inside a comment does not nest
### Step 91: Still commented
- **Status:** DONE
-->

### Step 3: Unicode separators are not Markdown lines
prose\u2028### Step 90: Not a physical line\u2028- **Status:** DONE

### Step 4: Unknown leading words are unsupported
- **Status:** DONEish
""",
        "synthetic.md",
    )
    assert (result["completed"], result["remaining"], result["unknown"]) == (0, 0, 4)
    assert result["warnings"] == {"skipped_headings": 0, "duplicate_ids": 0, "extra_markers": 0}


def test_comment_tokens_inside_fence_do_not_hide_later_real_step() -> None:
    result = plan_progress.parse_plan(
        """```text
<!-- an unclosed comment token inside code
### Step 90: Fenced sample
- **Status:** DONE
```
### Step 1: Real step
- **Status:** PENDING
""",
        "synthetic.md",
    )
    assert (result["completed"], result["remaining"], result["unknown"]) == (0, 1, 0)
    assert result["next_steps"][0]["id"] == "1"


@pytest.mark.parametrize(
    "prefix",
    [
        "```inline code```\n```text\n### Step 90: Fenced sample\n- **Status:** DONE\n```",
        "Prose uses `<!--` as literal inline code.",
        r"Escape a tick \` and show `<!--` literally.",
        "Show `the literal <!--\nmarker` here.",
        "Prose uses `<!--` then <!-- a real closed comment -->",
        "~~~text\n`<!--` remains code inside a tilde fence\n~~~",
        "````text\n```inline code```\n### Step 91: Still fenced\n- **Status:** DONE\n````",
        "A comment with a tick <!-- ` --> and a later `span`.",
        "Odd ticks ` in one paragraph `` stay literal `",
    ],
)
def test_adjacent_inline_fence_and_comment_boundaries_preserve_real_step(prefix: str) -> None:
    result = plan_progress.parse_plan(
        prefix + "\n### Step 1: Real step\n- **Status:** PENDING\n", "synthetic.md"
    )
    assert (result["completed"], result["remaining"], result["unknown"]) == (0, 1, 0)
    assert result["next_steps"][0]["id"] == "1"


@pytest.mark.parametrize("status", ["DONE\x1f", "PEND\u0130NG", "WAIT\u0130NG", "D\u0130NE"])
def test_unicode_status_variants_are_not_explicit_evidence(status: str) -> None:
    result = plan_progress.parse_plan(
        f"### Step 1: Unsupported status\n- **Status:** {status}\n", "synthetic.md"
    )
    assert (result["completed"], result["remaining"], result["held"], result["unknown"]) == (
        0,
        0,
        0,
        1,
    )


@pytest.mark.parametrize(
    "label",
    [
        "../private.md",
        "C:/private.md",
        "docs/plan\\x.md",
        "docs//plan.md",
        "docs/./plan.md",
        "docs/",
    ],
)
def test_public_source_label_must_be_safe_and_repository_relative(label: str) -> None:
    with pytest.raises(plan_progress.PlanError, match="source-label-invalid"):
        plan_progress.parse_plan("### Step 1: One\n", label)


def test_cli_reads_without_writing_and_reports_machine_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "plan.md"
    source.write_text("### Step 7: Next proof\n- **Status:** PENDING\n", encoding="utf-8")
    before = source.read_bytes()
    argv = ["--plan", str(source), "--source", "plan.md", "--root", str(tmp_path)]
    assert plan_progress.main(argv) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["next_steps"][0]["id"] == "7"
    assert tuple(result) == TOP_KEYS
    assert captured.err == ""
    assert source.read_bytes() == before


def test_cli_rejects_sources_outside_root_without_disclosing_path(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = tmp_path / "private-plan.md"
    source.write_text("### Step 1: Private\n- **Status:** DONE\n", encoding="utf-8")
    outcome = run_cli(SCRIPT, source, "docs/plan.md", root=root)
    assert (outcome.code, outcome.result) == (3, None)
    assert outcome.stderr == "plan-progress-unavailable: source-outside-root\n"
    assert str(source) not in outcome.stderr


def test_opened_handle_must_still_resolve_inside_repository_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "plan.md"
    source.write_text("### Step 1: One\n- **Status:** DONE\n", encoding="utf-8")
    monkeypatch.setattr(plan_progress, "_final_path", lambda descriptor: tmp_path / "outside.md")
    with pytest.raises(plan_progress.PlanError, match="source-outside-root"):
        plan_progress.read_plan(source, "plan.md", root)


@pytest.mark.parametrize("limit_name", ["bytes", "steps"])
def test_cli_limits_fail_closed_with_empty_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
) -> None:
    source = tmp_path / "plan.md"
    if limit_name == "bytes":
        monkeypatch.setattr(plan_progress, "MAX_BYTES", 8)
        source.write_text("### Step 1: Too large\n", encoding="utf-8")
        reason = "source-too-large"
    else:
        monkeypatch.setattr(plan_progress, "MAX_STEPS", 1)
        source.write_text("### Step 1: One\n\n### Step M2: Two\n", encoding="utf-8")
        reason = "more than 1 steps"
    assert plan_progress.main(["--plan", str(source), "--source", "plan.md"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert reason in captured.err


def test_duplicates_and_skipped_headings_do_not_count_toward_the_step_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plan_progress, "MAX_STEPS", 1)
    result = plan_progress.parse_plan(
        "### Step 1: One\n### Step 1: Again\n### Step 1 - draft\n", "synthetic.md"
    )
    assert result["unknown"] == 1
    assert result["warnings"] == {"skipped_headings": 1, "duplicate_ids": 1, "extra_markers": 0}


def test_cli_rejects_non_regular_source(tmp_path: Path) -> None:
    outcome = run_cli(SCRIPT, tmp_path, "plan.md")
    assert (outcome.code, outcome.result) == (3, None)
    assert outcome.stderr == "plan-progress-unavailable: source-not-regular\n"
