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

from passing_notes import away, cli, protocol, status, sweep, wake

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


# The texts Step 22 changed: every protocol value and CLI flag they name must be real.
COMMAND_TEXTS = (
    "docs/lane-rules.md",
    "skills/lane-close/core.md",
    "skills/lane-handoff/core.md",
    "README.md",
    "docs/cli.md",
    "skills/lane-status/core.md",
)


@pytest.mark.parametrize("doc", COMMAND_TEXTS)
def test_changed_texts_use_real_protocol_values(doc: str) -> None:
    text = (ROOT / doc).read_text(encoding="utf-8")
    found = 0
    for flag, allowed in (
        ("--kind", protocol.KINDS),
        ("--needs", protocol.NEEDS),
        ("--state", protocol.CARD_STATES),
    ):
        for value in re.findall(rf"{flag} ([a-z-]+)", text):
            assert value in allowed, f"{doc}: {flag} {value}"
            found += 1
    if doc == "docs/lane-rules.md":
        assert found >= 4  # the rules really do use these flags


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _named_commands(text: str) -> list[str]:
    """Each code span that runs the CLI: `passing-notes <verb> ...`, or a bare `<verb> ...`
    span that names a flag (for example `card set --project <P> ...`)."""
    verbs = set(_subcommands(cli.build_parser()))
    fences = re.findall(r"```[a-z]*\n(.*?)```", text, re.DOTALL)
    spans = [line for block in fences for line in block.splitlines()]
    spans += re.findall(r"`([^`]+)`", re.sub(r"```.*?```", "", text, flags=re.DOTALL))
    commands = []
    for span in spans:
        words = span.split()
        if words and words[0] == "passing-notes" and len(words) > 1:
            commands.append(" ".join(words[1:]))
        elif words and words[0] in verbs and any(w.startswith("--") for w in words):
            commands.append(" ".join(words))
    return commands


@pytest.mark.parametrize("doc", COMMAND_TEXTS)
def test_every_cli_flag_the_changed_texts_name_exists(doc: str) -> None:
    """Every flag in a `passing-notes ...` command these texts name is one the CLI parser
    has for that very verb (C2: the routine check named a real but wrong flag)."""
    text = (ROOT / doc).read_text(encoding="utf-8")
    commands = _named_commands(text)
    assert commands, doc
    checked = 0
    for command in commands:
        words = command.split()
        parser = cli.build_parser()
        verb = words[0]
        subs = _subcommands(parser)
        if verb.startswith("<"):
            continue  # a placeholder such as `passing-notes <command> --help`
        assert verb in subs, f"{doc}: unknown verb in `{command}`"
        parser = subs[verb]
        rest = words[1:]
        nested = _subcommands(parser)
        if nested and rest and rest[0] in nested:
            parser = nested[rest[0]]
        known = {o for action in parser._actions for o in action.option_strings}
        for word in rest:
            flag = word.split("=")[0].lstrip("[(").rstrip(",;.)]")
            if flag.startswith("--"):
                assert flag in known, f"{doc}: `{command}` names {flag}"
                checked += 1
    assert checked, doc


def _core_section(text: str, heading: str) -> str:
    start = text.index(f"\n{heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


LANE_RULES = ROOT / "docs" / "lane-rules.md"
LANE_CLOSE_CORE = ROOT / "skills" / "lane-close" / "core.md"
LANE_HANDOFF_CORE = ROOT / "skills" / "lane-handoff" / "core.md"
# D69's cadence, owned once by lane-rules section 3.
SWEEP_CADENCE = (
    "at session start, after each operator message, after each batch of doorbells, and "
    "before ending any turn longer than 10 minutes"
)
SWEEP_POINTER = 'passing-notes `docs/lane-rules.md`, section 3, "Sweep duty"'
# The lane-rules section headings these tests slice with _core_section.
RULES_3 = "## 3. Notes, acknowledgements and doorbells"
RULES_6 = "## 6. Opening, closing and handing off lanes"
RULES_8 = "## 8. What a project names locally"


def test_routine_check_is_the_coordinator_view_with_the_sweep() -> None:
    """C2: the routine check names --coordinator-attention and --sweep and no longer
    pairs --needs-attention (a lane-row filter) with the coordinator."""
    rules = LANE_RULES.read_text(encoding="utf-8")
    section = _core_section(rules, RULES_3)
    flat = " ".join(section.split())
    check = "`passing-notes status --mine --live --coordinator-attention --sweep`"
    assert f"Main's routine check is {check}" in flat
    assert "`lane-status --mine --live --coordinator-attention --sweep`" in flat
    assert "--needs-attention" not in rules
    assert "--coordinator-attention" in _status_flags() and "--sweep" in _status_flags()


def test_sweep_cadence_is_owned_once_and_the_lane_skills_point_to_it() -> None:
    """D69: one paragraph in lane-rules section 3 owns the cadence; lane-close and
    lane-handoff point to it by name instead of restating it."""
    rules = LANE_RULES.read_text(encoding="utf-8")
    section = " ".join(_core_section(rules, RULES_3).split())
    assert "**Sweep duty.**" in section and SWEEP_CADENCE in section
    assert "This paragraph is the one owner of the cadence; other files point here." in section
    texts = [ROOT / name for name in (*DOCS, "AGENTS.md", "CLAUDE.md")]
    texts += sorted((ROOT / "skills").glob("*/core.md"))
    texts += sorted((ROOT / "skills").glob("*/providers/*.md"))
    counts = {
        path: " ".join(path.read_text(encoding="utf-8").split()).count(SWEEP_CADENCE)
        for path in texts
    }
    assert sum(counts.values()) == 1 and counts[LANE_RULES] == 1, counts
    for core in (LANE_CLOSE_CORE, LANE_HANDOFF_CORE):
        flat = " ".join(core.read_text(encoding="utf-8").split())
        assert SWEEP_POINTER in flat, core
        for restated in ("10 minutes", "batch of doorbells", "after each operator message"):
            assert restated not in flat, (core, restated)


def test_stand_in_limit_is_three_and_matches_the_sweep() -> None:
    """P45, verbatim, in section 8; the sweep's stand-ins-over-limit row uses the same 3."""
    section = " ".join(_core_section(LANE_RULES.read_text(encoding="utf-8"), RULES_8).split())
    assert sweep.STAND_IN_LIMIT == 3
    assert (
        f'(operator, P45: "yes: at most {sweep.STAND_IN_LIMIT} stand-in mains per Claude '
        'dev/main window.")'
    ) in section
    assert "Before standing in for a fourth project" in section
    assert "`stand-ins-over-limit` row (`open-project-main`)" in section
    assert sweep.SWEEP_ACTIONS["stand-ins-over-limit"] == "open-project-main"


def test_standing_closes_follow_d2_and_d5_literally() -> None:
    """D67: D5 covers lane-open Claude windows; D2 covers any standard-closeable window
    below the floor; a Codex window above it still needs explicit authorization."""
    section = " ".join(_core_section(LANE_RULES.read_text(encoding="utf-8"), RULES_6).split())
    assert "**Standing closes.**" in section
    assert (
        'P46: "yes: auto-close done lane-open Claude windows once proven safe; '
        'operator-opened windows stay manual."'
    ) in section
    floor = f"below {sweep.MEMORY_FLOOR_DEFAULT_GIB} GiB of free memory"
    assert f"{floor}, any `standard-closeable` window, Claude or Codex" in section
    assert "a lane-open Codex window still needs the operator's explicit authorization" in section
    assert "no tool closes it at any memory level" in section
    assert "Claude and unverified windows remain for the operator" not in section
    close = " ".join(LANE_CLOSE_CORE.read_text(encoding="utf-8").split())
    assert "operator-authorized Codex launcher only" not in close
    assert "lane-open-launched Claude or Codex window only through" in close
    assert floor in close


@pytest.mark.parametrize(
    "provider", sorted((ROOT / "skills" / "lane-close" / "providers").glob("*.md"))
)
def test_lane_close_providers_do_not_narrow_the_cleanup_to_codex(provider: Path) -> None:
    """D66 and section 6 let the Done-window cleanup close a lane-open Claude window too.
    A provider overlay loads with the core, so it must not restrict the cleanup to Codex
    launchers."""
    flat = " ".join(provider.read_text(encoding="utf-8").split())
    assert "Codex launcher" not in flat, provider.name
    if provider.name == "claude.md":
        assert "Never close this window." in flat
        assert (
            "the core's Done-window cleanup may close a lane-open window, Claude or Codex, "
            "only when the helper proves it closeable and one of the core's listed "
            "authorizations covers it"
        ) in flat


def test_windowless_route_uses_only_real_verbs_and_writes_no_other_card() -> None:
    """D70: a main stands in and self-closes; a work lane is reopened under the same id
    with a close-only brief. Only `card set`, `lane-open` and `lane-close` are named."""
    text = LANE_CLOSE_CORE.read_text(encoding="utf-8")
    start = text.index("\n### Windowless lane (REQUEST mode only)\n")
    route = text[start : text.index("\n### ", start + 1)]
    flat = " ".join(route.split())
    assert "Only a lane writes its own card" in flat
    assert set(re.findall(r"passing-notes ([a-z]+ [a-z]+)", route)) == {"card set"}
    assert "`lane-open " in route and "`lane-close` SELF mode" in route
    skills = set(re.findall(r"\blane-[a-z]+", route)) - {"lane-rules"}
    assert skills == {"lane-open", "lane-close"}
    # The set check above sees only `passing-notes` spans; a bare `wake unbind` (proposed
    # in W3/R7, absent from the CLI) would slip past it.
    assert "unbind" not in route
    assert "reopens the **same lane id** with `lane-open" in flat
    assert "Never retire an orphan on your own." in flat
    assert "Do not print the SAFE TO CLOSE banner" in flat
    request = " ".join(_core_section(text, "## REQUEST mode").split())
    assert "Use the **Windowless lane** route below instead of steps 2-4." in request
    action = sweep.SWEEP_ACTIONS["windowless-lane"]
    assert f"`windowless-lane` row (action `{action}`)" in request
    rules = " ".join(_core_section(LANE_RULES.read_text(encoding="utf-8"), RULES_6).split())
    assert "**Only a lane writes its own card.**" in rules
    assert "The coordinator never writes the windowless lane's card." in rules


def test_windowless_reopen_is_only_for_idle_and_the_rest_go_to_the_operator() -> None:
    """lane-open reopens only a missing, done or idle card, so the windowless route
    reopens an idle work lane and sends an active, waiting or blocked one to the
    operator (wait for its window, or orphan retirement with window-gone proof)."""
    lane_open = " ".join((ROOT / "skills/lane-open/core.md").read_text(encoding="utf-8").split())
    assert (
        "shows `state: done` or `idle`. Any other state means "
        "`lane <lane> already has an active card`"
    ) in lane_open
    text = LANE_CLOSE_CORE.read_text(encoding="utf-8")
    start = text.index("\n### Windowless lane (REQUEST mode only)\n")
    route = " ".join(text[start : text.index("\n### ", start + 1)].split())
    idle = "**The target is a work lane with an `idle` card.** This is the only reopen case"
    assert idle in route
    other = "**The target is a work lane whose card is `active`, `waiting` or `blocked`.**"
    assert f"{other} Do not reopen it" in route
    assert "Report the row to the operator as needing a decision" in route
    assert "wait for that lane's own window to come back and close itself" in route
    assert "orphan retirement with proof that the window is gone" in route
    assert "which needs the operator's word" in route
    # The stand-in writes only its own card, and only with no live main.
    assert "so its `card set --lane main` is that lane writing its own card" in route
    assert STAND_IN_TRIGGER in route and STALE_MAIN_PROOF in route
    rules = LANE_RULES.read_text(encoding="utf-8")
    six = " ".join(_core_section(rules, RULES_6).split())
    assert "For a work lane with an `idle` card, the coordinator" in six
    assert "a windowless lane that is `active`, `waiting` or `blocked` goes to the operator" in six
    assert "so its `card set` is that lane writing its own card" in six
    three = " ".join(_core_section(rules, RULES_3).split())
    assert "a close-only reopen for an `idle` work lane" in three
    assert "any other windowless lane goes in the table as an operator decision" in three
    eight = " ".join(_core_section(rules, RULES_8).split())
    assert STAND_IN_TRIGGER in eight and STALE_MAIN_PROOF in eight
    assert "`card set` is `main` writing its own card" in eight


# The protocol's stand-in trigger (no live main card); a stale live-state main card needs
# the orphan-retirement proof that its window is gone.
STAND_IN_TRIGGER = "no live `main` card"
STALE_MAIN_PROOF = (
    "A stale `active`, `waiting` or `blocked` `main` card qualifies only with proof that "
    "the window is gone, which needs the operator's word, as for orphan retirement"
)


def test_stand_in_trigger_matches_the_protocol_and_needs_window_gone_proof() -> None:
    """protocol.md: a stand-in needs no live main card (none, done or idle). A stale
    active main card is not enough on its own: it needs section 6's orphan-retirement
    proof, so dev main never overwrites a main whose window may still be live."""
    protocol_text = " ".join((ROOT / "docs" / "protocol.md").read_text(encoding="utf-8").split())
    assert "A project with no live `main` card may have the workspace main stand in." in (
        protocol_text
    )
    rules = LANE_RULES.read_text(encoding="utf-8")
    six = " ".join(_core_section(rules, RULES_6).split())
    orphan = "orphan retirement with proof that the window is gone, which needs the operator's word"
    assert orphan in six
    eight = " ".join(_core_section(rules, RULES_8).split())
    trigger = (
        "With no live `main` card (no `main` card, or a `done` or `idle` one), the "
        "operator's window (dev main) may stand in"
    )
    assert trigger in eight and STALE_MAIN_PROOF in eight
    close = " ".join(LANE_CLOSE_CORE.read_text(encoding="utf-8").split())
    assert (
        "It stands in only when the project has no live `main` card: no `main` card, or a "
        "`done` or `idle` one."
    ) in close
    for text in (eight, close):
        assert "a stale one with no live binding" not in text
        assert "no live binding)" not in text


def test_request_step_one_allows_every_done_window_cleanup_authorization() -> None:
    """REQUEST step 1 points to the Done-window cleanup's authorization rule, so a
    standing close (P46, P43) applies there too, not only an explicit authorization."""
    text = LANE_CLOSE_CORE.read_text(encoding="utf-8")
    request = _core_section(text, "## REQUEST mode")
    step_one = " ".join(request[: request.index("\n2. ")].split())
    assert (
        "If one of the authorizations that the **Done-window cleanup** section below accepts "
        "covers its leftover window, including a standing close, use that procedure"
    ) in step_one
    assert "explicitly authorized" not in step_one
    cleanup_section = text[text.index("\n### Done-window cleanup (REQUEST mode only)\n") :]
    cleanup_flat = " ".join(cleanup_section.split())
    assert "Use it only under one of these authorizations:" in cleanup_flat
    assert "or a standing close covers the row" in cleanup_flat


def test_standing_authority_is_scoped_and_the_floor_is_fixed() -> None:
    """The sweep's standing authority is the safe-close rows section 6 covers, and the
    4 GiB floor does not move with a sweep's --memory-floor-gib."""
    rules = LANE_RULES.read_text(encoding="utf-8")
    three = " ".join(_core_section(rules, RULES_3).split())
    assert "the `safe-close` rows that the standing closes in" in three
    assert "a lane-open Claude window (P46)" in three
    assert "any `standard-closeable` window below the 4 GiB floor (P43)" in three
    assert "lane-open Codex window above the floor still needs the operator's explicit" in three
    floor = sweep.MEMORY_FLOOR_DEFAULT_GIB
    six = " ".join(_core_section(rules, RULES_6).split())
    fixed = f"The floor for this authority is {floor} GiB (P43), whatever `--memory-floor-gib`"
    assert fixed in six
    close = " ".join(LANE_CLOSE_CORE.read_text(encoding="utf-8").split())
    assert f"that floor stays {floor} GiB whatever `--memory-floor-gib` a sweep used" in close
    assert "--memory-floor-gib" in _status_flags()


def test_closed_windows_are_handed_back_and_handovers_name_the_window() -> None:
    """W2: a lane-open window's SELF report says so and the coordinator's next sweep
    closes it; the banner stays as the fallback. W3: the handover names the outgoing
    window's title and launch type, and lane-handoff carries them."""
    close = " ".join(LANE_CLOSE_CORE.read_text(encoding="utf-8").split())
    assert "**Safe-close-ready handback:**" in close
    assert "the SELF line says `window lane-open-launched`" in close
    assert "The banner below stays as the fallback" in close
    assert "· window <lane-open-launched | operator-opened | unknown>" in close
    assert "**Window:** this window's title and launch type." in close
    handoff = " ".join(LANE_HANDOFF_CORE.read_text(encoding="utf-8").split())
    assert "**Outgoing window.**" in handoff
    assert "names its window title and launch type" in handoff
    assert "**Outgoing windows:** each old lane's window title and launch type" in handoff
    assert "· window <title> (<lane-open | operator-opened | pending>)" in handoff
    assert "shows it as `operator-close`" in handoff


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
    "session": "`session`:",
    "status": "top level:",
    "scope": "`scope`:",
    "project": "each of `projects[]`:",
    "lane": "each of `lanes[]`:",
    "diagnostics": "`diagnostics`:",
    "resources": "`resources`:",
    "queue_holder": "each of `queue_holders[]`:",
    "attention": "`attention` (when non-null):",
    "coordinator_attention": "`coordinator_attention`:",
    "sweep": "each of `sweep[]` (only with `--sweep`):",
}


def _status_doc_section() -> str:
    text = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    start = text.index("## Lane status (read-only)")
    return text[start : text.index("\n## ", start + 1)]


def test_away_check_docs_cover_findings_scope_unknowns_and_exit_codes() -> None:
    text = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    start = text.index("## Before stepping away (read-only)")
    section = text[start : text.index("\n## ", start + 1)]
    for flag in ("--project", "--all-projects", "--mine", "--json"):
        assert flag in section
    for kind in away.KINDS:
        assert f"`{kind}`" in section
    assert away.SCHEMA in section
    assert "Exit codes: 0 no findings observed; 3 one or more findings" in section
    assert "not proof that every lane is healthy" in section
    assert "first refused project fails the whole command before observation" in section


def _status_flags() -> list[str]:
    status_parser = _subcommands(cli.build_parser())["status"]
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
    assert flags == [
        "--all-projects",
        "--coordinator-attention",
        "--details",
        "--expand-idle",
        "--json",
        "--live",
        "--memory-floor-gib",
        "--mine",
        "--needs-attention",
        "--project",
        "--stale-after",
        "--sweep",
    ]
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


def test_cli_doc_lists_exactly_the_sweep_kinds_and_actions() -> None:
    """docs/cli.md's sweep table is sweep.SWEEP_KINDS in order, each with its
    SWEEP_ACTIONS word; status uses those very objects (one source of truth)."""
    section = _status_doc_section()
    rows = re.findall(r"^\| `([a-z-]+)` \| `([a-z-]+)` \| ", section, re.MULTILINE)
    assert tuple(kind for kind, _ in rows) == sweep.SWEEP_KINDS
    assert dict(rows) == dict(sweep.SWEEP_ACTIONS)
    assert status.SWEEP_KINDS is sweep.SWEEP_KINDS
    assert status.SWEEP_ACTIONS is sweep.SWEEP_ACTIONS
    assert tuple(sweep.SWEEP_ACTIONS) == sweep.SWEEP_KINDS
    flat = " ".join(section.split())
    assert f"`{status.SWEEP_KEY}` follows last" in flat
    assert f"default {sweep.MEMORY_FLOOR_DEFAULT_GIB}" in flat
    assert "The CLI never reads process trees or launcher files" in flat
    assert "A project's main sees only its own project's rows" in flat
    assert "never a note body" in flat


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
    "collapsed_idle",
    "free_memory_bytes",
    "queue_holders",
    "queues_complete",
    "deadline_utc",
    "attention",
    "coordinator_attention",
    "priority",
    "kind",
    "evidence",
    "summary",
    "delivery",
    "action",
)


def _cli_verbs() -> set[str]:
    return set(_subcommands(cli.build_parser()))


def test_lane_status_core_runs_status_json_and_only_the_read_only_cleanup_verifier() -> None:
    """The optional coordinator view adds the existing read-only closeability verifier;
    it never adds another Passing Notes verb or the helper's close form."""
    text = LANE_STATUS_CORE.read_text(encoding="utf-8")
    allowed = _core_section(text, "## Commands")
    blocks = re.findall(r"```text\n(.*?)```", allowed, re.DOTALL)
    assert len(blocks) == 2
    block = blocks[0]
    lines = [line for line in block.splitlines() if line.strip()]
    assert sorted(line.split()[2] for line in lines) == ["--all-projects", "--mine", "--project"]
    for line in lines:
        assert line.startswith("passing-notes status --") and " --json" in line, line
    assert "close_done_window.py --project <P> --public-inventory" in blocks[1]
    assert "--close" not in blocks[1]
    for verb in LANE_STATUS_FORBIDDEN:
        assert f"passing-notes {verb}" not in allowed, verb
    verbs = _cli_verbs()
    assert {"status", "card", "wake", "ack", "post", "read"} <= verbs
    before_donts = text[: text.index("\n## What NOT to do\n")]
    assert set(re.findall(r"passing-notes ([a-z]+)", before_donts)) & verbs == {"status"}
    donts = " ".join(_core_section(text, "## What NOT to do").split())
    for verb in LANE_STATUS_FORBIDDEN:
        assert f"`{verb}`" in donts, verb


def test_lane_status_sweep_runs_only_status_json_and_the_public_inventory() -> None:
    """lane-status --sweep names exactly two commands: status with --json and --sweep,
    and the close helper's --public-inventory; it maps done-window-open rows to
    safe-close (standard-closeable) or operator-close (anything else)."""
    text = LANE_STATUS_CORE.read_text(encoding="utf-8")
    section = _core_section(text, "## Sweep report")
    blocks = re.findall(r"```text\n(.*?)```", section, re.DOTALL)
    assert len(blocks) == 1
    lines = [line for line in blocks[0].splitlines() if line.strip()]
    assert len(lines) == 2
    status_line, helper_line = lines
    assert status_line.startswith("passing-notes status ")
    assert " --json " in status_line and " --sweep " in status_line
    assert helper_line.endswith("close_done_window.py --project <P> --public-inventory")
    assert set(re.findall(r"passing-notes ([a-z]+)", section)) == {"status"}
    mentions = re.findall(r"close_done_window\.py[^\n]*", section)
    assert mentions and all(m.endswith("--project <P> --public-inventory") for m in mentions)
    for forbidden in ("--close", "--list", "passing-notes ack", "passing-notes post"):
        assert forbidden not in section, forbidden
    flat = " ".join(section.split())
    assert "and no other" in flat
    assert "`safe-close` when the inventory has a row for the same project and lane" in flat
    assert "`kind: standard-closeable` and `closeable: true`" in flat
    assert "`operator-close` otherwise" in flat
    assert "Never show `safe-close` from the CLI row alone" in flat
    assert "any other process needs the operator" in flat
    for kind in ("done-window-open", "memory-below-floor"):
        assert kind in sweep.SWEEP_KINDS and f"`{kind}`" in flat
    assert "safe-close" in sweep.SWEEP_ACTIONS.values()
    assert "operator-close" not in sweep.SWEEP_ACTIONS.values()  # the skill's word (D64)
    assert re.search(r"\[--sweep \[--memory-floor-gib <N>\]\]", text)


def test_coordinator_alert_workflow_is_pinned_to_structured_evidence() -> None:
    rules = " ".join((ROOT / "docs" / "lane-rules.md").read_text(encoding="utf-8").split())
    core = LANE_STATUS_CORE.read_text(encoding="utf-8")
    for phrase in (
        "immediately before launching a build, test or independent-review gate",
        "A coordinator hold that must land before a lane starts its next gate",
        "A lane that finishes a gate with a non-passing result",
        "No live agent is watching cards between turns",
        "A queued Codex doorbell is not a read or reply",
    ):
        assert phrase in rules
    assert "P4 done-window-cleanup" in core
    assert "safe-projection JSON with `project`, `lane`" in core
    assert "session-process-unverified" in core
    assert "never emits a pid" in core and "launcher path" in core


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
    must not break the row or render. WINDOW uses additive observed liveness."""
    flat = " ".join(LANE_STATUS_CORE.read_text(encoding="utf-8").split())
    assert "**Untrusted text stays inert.**" in flat
    for step in (
        r"a line feed as `\n`, a carriage return as `\r`, a tab as `\t`",
        "Put the result in a code span, so no link, image, HTML or emphasis in it renders",
        "one backtick longer than the longest run of backticks in the text",
        r"Escape each `|` as `\|`. GitHub tables need this even inside a code span",
    ):
        assert step in flat, step
    assert "`WINDOW` is `window_state`" in flat
    assert "legacy `window` describes a binding record" in flat


def test_lane_status_core_uses_cli_attention_before_legacy_ranking() -> None:
    text = LANE_STATUS_CORE.read_text(encoding="utf-8")
    assert "Render the CLI's `attention` directly" in text
    assert (
        text.index("(1) activity `pending-approval`")
        < text.index("(2) activity `suspected-stall`")
        < text.index("(3) state `blocked`")
        < text.index("(5) `open_unblocking` above 0")
    )


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
