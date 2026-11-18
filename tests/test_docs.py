"""README, lane rules and diagrams: generated files current, links and values real."""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest

from passing_notes import cli, protocol, status, wake

ROOT = Path(__file__).resolve().parents[1]
DOCS = ("README.md", "docs/cli.md", "docs/lane-rules.md")


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


diagrams = _load("diagrams")


def test_committed_diagrams_match_their_definitions() -> None:
    for path, text in diagrams.outputs().items():
        assert path.exists(), f"run: uv run python tools/diagrams.py ({path.name} missing)"
        on_disk = path.read_text(encoding="utf-8")
        assert on_disk == text, f"run: uv run python tools/diagrams.py ({path.name} stale)"
        root = ET.fromstring(on_disk)
        assert root.get("role") == "img" and root.get("aria-label")


def test_light_and_dark_come_from_one_definition() -> None:
    for d in diagrams.DIAGRAMS:
        light = diagrams.render(d, "light")
        dark = diagrams.render(d, "dark")
        strip = re.compile(r"#[0-9A-Fa-f]{6}|rgba\([^)]*\)|-(light|dark)\b")
        assert strip.sub("", light) == strip.sub("", dark)


@pytest.mark.parametrize("doc", DOCS)
def test_relative_links_and_images_resolve(doc: str) -> None:
    path = ROOT / doc
    text = path.read_text(encoding="utf-8")
    targets = re.findall(r"\]\(([^)\s]+)\)", text) + re.findall(r'(?:src|srcset)="([^"]+)"', text)
    for target in targets:
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        resolved = (path.parent / target.split("#")[0]).resolve()
        assert resolved.exists(), f"{doc}: broken link {target}"


def test_every_readme_picture_has_both_themes() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    pictures = re.findall(r"<picture>(.*?)</picture>", text, re.DOTALL)
    assert len(pictures) == len(diagrams.DIAGRAMS)
    for block in pictures:
        assert re.search(r'srcset="docs/diagrams/[a-z-]+-dark\.svg"', block)
        assert re.search(r'src="docs/diagrams/[a-z-]+-light\.svg"', block)
        assert re.search(r'alt="[^"]{40,}"', block)


def test_lane_rules_use_real_protocol_values() -> None:
    text = (ROOT / "docs" / "lane-rules.md").read_text(encoding="utf-8")
    found = 0
    for flag, allowed in (
        ("--kind", protocol.KINDS),
        ("--needs", protocol.NEEDS),
        ("--state", protocol.CARD_STATES),
    ):
        for value in re.findall(rf"{flag} ([a-z-]+)", text):
            assert value in allowed, f"lane-rules: {flag} {value}"
            found += 1
    assert found >= 4  # the rules really do use these flags


def test_cli_doc_quotes_the_inbox_summary_format_exactly() -> None:
    """A host hook parses the summary line, so docs/cli.md pins its exact format, and
    documents every read-only view flag."""
    text = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    documented = "inbox-summary: count=<n> oldest_age_min=<m|-> stale=<k|->"
    assert f"`{documented}`" in text
    placeholders = {"count": "<n>", "oldest_age_min": "<m|->", "stale": "<k|->"}
    assert cli.INBOX_SUMMARY_FORMAT.format(**placeholders) == documented
    for flag in ("--summary", "--stale-after MINUTES", "--overlaps"):
        assert f"`{flag}`" in text, flag
    for usage in ("--summary --stale-after 30", "card show --project example-project --overlaps"):
        assert usage in text, usage


def test_cli_doc_quotes_the_doorbell_exactly() -> None:
    """docs/cli.md quotes wake.doorbell_text verbatim; lane rules carry the same rule."""
    text = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert f"`{wake.doorbell_text('<id>')}`" in flat
    assert "not the deliverable" in wake.doorbell_text("<id>")
    rules = " ".join((ROOT / "docs" / "lane-rules.md").read_text(encoding="utf-8").split())
    assert "Acknowledging a doorbell is not the deliverable." in rules
    assert "A finished item always posts its own handoff note" in rules


def test_session_start_and_wake_docs_name_the_new_views_and_flags() -> None:
    """Session start names --overlaps; the CLI doc and lane rules describe the post --wake
    exit 2 refusal and --wake-if-needed with its not-rung line."""
    for name in ("CLAUDE.md", "AGENTS.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        start = next(line for line in text.splitlines() if line.startswith("- **Session start:**"))
        assert "--overlaps" in start, name
    cli_doc = " ".join((ROOT / "docs" / "cli.md").read_text(encoding="utf-8").split())
    rules = " ".join((ROOT / "docs" / "lane-rules.md").read_text(encoding="utf-8").split())
    for text in (cli_doc, rules):
        assert "--wake-if-needed" in text and "exit 2" in text and "wake: not rung" in text
    assert "card set" in cli_doc and "involves this lane" in cli_doc
    assert "without regard to case" in cli_doc and "comparison is case-sensitive" not in cli_doc


# The labels docs/cli.md gives each D54 object's key list, keyed as status.JSON_KEYS.
STATUS_JSON_LABELS = {
    "status": "top level:",
    "scope": "`scope`:",
    "project": "each of `projects[]`:",
    "lane": "each of `lanes[]`:",
    "diagnostics": "`diagnostics`:",
}


def _status_doc_section() -> str:
    text = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    start = text.index("## Lane status (read-only)")
    return text[start : text.index("\n## ", start + 1)]


def _status_flags() -> list[str]:
    parser = cli.build_parser()
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    status_parser = commands.choices["status"]
    return sorted(
        option
        for action in status_parser._actions
        for option in action.option_strings
        if option != "--help" and option.startswith("--")
    )


def test_cli_doc_pins_the_status_flags_code_and_json_keys() -> None:
    """docs/cli.md documents every status flag the parser has, the out-of-scope code with
    the topology reference, the columns, and the D54 key lists exactly as the one
    constant status.JSON_KEYS defines them."""
    section = _status_doc_section()
    flags = _status_flags()
    assert flags == ["--all-projects", "--json", "--live", "--mine", "--project", "--stale-after"]
    for flag in flags:
        assert re.search(rf"^- `{flag}[ `]", section, re.MULTILINE), flag
    assert f"`{status.STATUS_OUT_OF_SCOPE}`" in section
    assert f"`{status.TOPOLOGY_REF}`" in section and "(protocol.md#topology)" in section
    assert f"`{' '.join(status.COLUMNS)}`" in section
    assert f'`"schema": "{status.SCHEMA}"`' in section
    assert set(STATUS_JSON_LABELS) == set(status.JSON_KEYS)
    bullets = [" ".join(b.split("\n\n")[0].split()) for b in re.split(r"\n- ", section)]
    for kind, label in STATUS_JSON_LABELS.items():
        (bullet,) = [b for b in bullets if b.startswith(label)]
        keys = re.findall(r"`([a-z_]+)`", re.sub(r"\([^)]*\)", "", bullet[len(label) :]))
        assert tuple(keys) == status.JSON_KEYS[kind], kind


LANE_STATUS_CORE = ROOT / "skills" / "lane-status" / "core.md"
# The verbs Step 17 forbids the lane-status skill to run.
LANE_STATUS_FORBIDDEN = ("card set", "wake bind", "ack", "post", "read")
# The status.JSON_KEYS fields the skill's rendering reads.
LANE_STATUS_KEYS_USED = (
    "schema",
    "generated_utc",
    "caller",
    "lanes",
    "operator_decisions",
    "item",
    "age_minutes",
    "open",
    "open_unblocking",
    "flags",
    "shown",
    "total",
)


def _core_section(text: str, heading: str) -> str:
    start = text.index(f"\n{heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def _cli_verbs() -> set[str]:
    parser = cli.build_parser()
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return set(commands.choices)


def test_lane_status_core_runs_only_status_json() -> None:
    """Step 17: the core's allowed-command text is ``passing-notes status ... --json`` and
    nothing else; no forbidden verb appears in it, and outside "What NOT to do" the core
    names no other CLI verb after ``passing-notes``."""
    text = LANE_STATUS_CORE.read_text(encoding="utf-8")
    allowed = _core_section(text, "## The only command")
    (block,) = re.findall(r"```text\n(.*?)```", allowed, re.DOTALL)
    lines = [line for line in block.splitlines() if line.strip()]
    assert sorted(line.split()[2] for line in lines) == ["--all-projects", "--mine", "--project"]
    for line in lines:
        assert line.startswith("passing-notes status --") and " --json" in line, line
    assert "only command" in allowed
    for verb in LANE_STATUS_FORBIDDEN:
        assert not re.search(rf"\b{verb}\b", allowed), verb
    verbs = _cli_verbs()
    assert {"status", "card", "wake", "ack", "post", "read"} <= verbs
    before_donts = text[: text.index("\n## What NOT to do\n")]
    assert set(re.findall(r"passing-notes ([a-z]+)", before_donts)) & verbs == {"status"}
    donts = " ".join(_core_section(text, "## What NOT to do").split())
    for verb in LANE_STATUS_FORBIDDEN:
        assert f"`{verb}`" in donts, verb


def test_status_docs_cover_several_projects_mine_and_live() -> None:
    """docs/cli.md and the lane-status core document repeated --project, --mine and the
    --live rule (done rows hidden, done_count true, one ``done: N hidden`` line)."""
    section = " ".join(_status_doc_section().split())
    for phrase in (
        "in the order given, duplicates dropped",
        "the first refused project fails the whole command",
        "--mine needs a window bound to at least one lane",
        "Exactly one of `--project` (one or more), `--all-projects` and `--mine`",
        "`done: N hidden`",
        "`lanes` omits the done rows and `done_count` stays the real count",
    ):
        assert phrase in section, phrase
    core = " ".join(LANE_STATUS_CORE.read_text(encoding="utf-8").split())
    for phrase in (
        "Exactly one of `--project <id>` (one or more), `--all-projects` and `--mine`",
        "`--mine --live` when the window knows it holds lanes in more than one project",
        "Done lanes are listed unless `--live` was passed",
        "The `Scope:` line names every scope flag the command ran with",
        "`<project>: done: <done_count> hidden`",
    ):
        assert phrase in core, phrase


def test_lane_status_core_keeps_untrusted_card_text_inert() -> None:
    """A card item is rendered in a Markdown table: a control character, a link or a `|`
    must not break the row or render. WINDOW follows D52 (a record, not liveness)."""
    flat = " ".join(LANE_STATUS_CORE.read_text(encoding="utf-8").split())
    assert "**Untrusted text stays inert.**" in flat
    for step in (
        r"a line feed as `\n`, a carriage return as `\r`, a tab as `\t`",
        "Put the result in a code span, so no link, image, HTML or emphasis in it renders",
        "one backtick longer than the longest run of backticks in the text",
        r"Escape each `|` as `\|`. GitHub tables need this even inside a code span",
    ):
        assert step in flat, step
    assert "not that its window is live (plan D52)" in flat


def test_lane_status_core_renders_the_cli_json_and_is_not_a_lane() -> None:
    """Step 17: a Markdown table with the CLI's columns, the flags, the operator-decision
    counts and one "look here first" line; status-out-of-scope passes through verbatim;
    invoking the skill does not make the window a lane; every JSON field it reads is a
    status.JSON_KEYS key."""
    text = LANE_STATUS_CORE.read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert "| " + " | ".join(status.COLUMNS) + " |" in text
    assert "Markdown table" in flat
    for label in ("Scope:", "Operator decisions:", "Flags:", "Look here first:"):
        assert re.search(rf"^{label} ", text, re.MULTILINE), label
    assert f"`{status.SCHEMA}`" in flat
    assert f"`{status.STATUS_OUT_OF_SCOPE}`" in flat and "**verbatim**" in flat
    assert "do not retry with another scope" in flat
    assert "**Invoking it does not make the window a lane.**" in flat
    assert "sets no card, binds no doorbell" in flat
    assert "Default when the operator gives no scope:" in flat
    assert "Otherwise `--all-projects`" in flat
    assert f"cut to {status.ITEM_WIDTH} characters" in flat
    assert f"`{status.ELLIPSIS}`" in flat
    known = {key for keys in status.JSON_KEYS.values() for key in keys}
    for key in LANE_STATUS_KEYS_USED:
        assert key in known, key
        assert re.search(rf"[`<.]{key}[`>]", text) or f".{key} " in text, key
