"""The lane-open skill package and its launcher script (dry runs only; no window opens)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from passing_notes import protocol

SKILL = Path(__file__).resolve().parents[1] / "skills" / "lane-open"
SCRIPT = SKILL / "scripts" / "open-lane.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")

needs_powershell = pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None, reason="the launcher is Windows PowerShell"
)


def run_script(
    tmp_path: Path, *args: str, prompt: str = "You are lane x.\n"
) -> tuple[int, dict[str, Any]]:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    argv = [
        str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
        "-WorkingDirectory", str(tmp_path), "-PromptFile", str(prompt_file),
        "-StateRoot", str(tmp_path / "state"), "-StoreRoot", str(tmp_path / "store"),
        *args,
    ]  # fmt: skip
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    return done.returncode, json.loads(done.stdout.strip().splitlines()[-1])


@needs_powershell
def test_claude_lane_is_an_independent_visible_window(tmp_path: Path) -> None:
    code, out = run_script(
        tmp_path, "-Project", "example-project", "-Lane", "api-client", "-HostName", "claude",
        "-Model", "opus", "-Effort", "medium", "-Title", "example - API client lane", "-DryRun",
    )  # fmt: skip
    assert code == 0
    assert out["launched"] is False and out["dry_run"] is True
    text = out["launcher_text"]
    # The caller's session markers are removed; configuration variables stay.
    assert "$_.Name -like 'CLAUDE*' -and $_.Name -ne 'CLAUDE_CONFIG_DIR'" in text
    assert "$_.Name -like 'CODEX_*' -and $_.Name -ne 'CODEX_HOME'" in text
    assert (
        "& claude -n 'example - API client lane' --model 'opus' --effort 'medium' $prompt" in text
    )
    for forbidden in ("--bg", "--background", "dangerously", "bypassPermissions", "-p "):
        assert forbidden not in text
    assert not (tmp_path / "state").exists()  # a dry run writes nothing


@needs_powershell
def test_codex_lane_gets_the_store_as_a_writable_root(tmp_path: Path) -> None:
    code, out = run_script(
        tmp_path, "-Project", "example-project", "-Lane", "api-client", "-HostName", "codex",
        "-Model", "gpt-6-astra", "-Effort", "high", "-DryRun",
    )  # fmt: skip
    assert code == 0
    text = out["launcher_text"]
    assert f"--add-dir '{tmp_path / 'store'}'" in text
    assert "-m 'gpt-6-astra' -c 'model_reasoning_effort=high' $prompt" in text
    assert "exec" not in text and "dangerously" not in text
    assert out["title"] == "example-project - api-client lane"


@needs_powershell
def test_double_quotes_in_the_prompt_are_reported(tmp_path: Path) -> None:
    code, out = run_script(
        tmp_path, "-Project", "p", "-Lane", "l", "-HostName", "claude", "-DryRun",
        prompt='Say "hi".\n',
    )  # fmt: skip
    assert code == 0
    assert out["double_quotes_replaced"] is True


@needs_powershell
@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["-Project", "Bad_Id", "-Lane", "l"], "project id"),
        (["-Project", "p", "-Lane", "all"], "lane id"),
        (["-Project", "p", "-Lane", "l", "-Title", 'a "quoted" title'], "title"),
        (["-Project", "p", "-Lane", "l", "-Model", "opus; rm"], "model"),
    ],
)
def test_bad_arguments_launch_nothing(tmp_path: Path, args: list[str], reason: str) -> None:
    code, out = run_script(tmp_path, *args, "-HostName", "claude")
    assert code == 2
    assert out["launched"] is False and reason in out["error"]
    assert not (tmp_path / "state").exists()


def test_script_is_ascii_only() -> None:
    # Windows PowerShell 5.1 reads a no-BOM script as ANSI; non-ASCII can corrupt parsing.
    assert all(b < 128 for b in SCRIPT.read_bytes())


SKILLS_ROOT = SKILL.parent
LANE_SKILLS = ("lane-open", "lane-close", "lane-handoff")


@pytest.mark.parametrize("name", LANE_SKILLS)
def test_package_matches_its_skill_mesh_entry(name: str) -> None:
    skill = SKILLS_ROOT / name
    entry = json.loads((skill / "skill-mesh-entry.json").read_text(encoding="utf-8"))
    assert entry["name"] == name
    assert set(entry["providers"]) == {"claude", "codex", "gpt"}
    prefix = f"skills/{name}/"
    for rel in [entry["core"], *entry["providers"].values()]:
        assert rel.startswith(prefix)
        assert (skill / rel.removeprefix(prefix)).is_file(), rel
    for provider in entry["providers"].values():
        text = (skill / provider.removeprefix(prefix)).read_text(encoding="utf-8")
        assert "[../core.md](../core.md)" in text
    claude = (skill / "providers" / "claude.md").read_text(encoding="utf-8")
    assert claude.startswith(f"---\nname: {name}\n")
    for asset in entry["support_assets"]:
        assert asset["source"].startswith(f"passing-notes/{prefix}")
        assert (SKILLS_ROOT.parent / asset["source"].removeprefix("passing-notes/")).exists()


@pytest.mark.parametrize("name", LANE_SKILLS)
def test_skill_commands_use_real_protocol_values(name: str) -> None:
    """Every --kind, --needs, --state and --host value a skill core uses is one the CLI accepts."""
    text = (SKILLS_ROOT / name / "core.md").read_text(encoding="utf-8")
    checks = {
        "--kind": protocol.KINDS,
        "--needs": protocol.NEEDS,
        "--state": protocol.CARD_STATES,
    }
    for flag, allowed in checks.items():
        for value in re.findall(rf"{flag} ([a-z-]+)", text):
            assert value in allowed, f"{name}: {flag} {value}"


def test_handoff_composes_close_and_open() -> None:
    text = (SKILLS_ROOT / "lane-handoff" / "core.md").read_text(encoding="utf-8")
    assert "`lane-close`" in text and "`lane-open`" in text
    assert "support script" in text


def test_out_of_sample_fixes_stay_in_the_skills() -> None:
    """Regressions found in the 2026-11-13 run on real lanes."""
    handoff = (SKILLS_ROOT / "lane-handoff" / "core.md").read_text(encoding="utf-8")
    close = (SKILLS_ROOT / "lane-close" / "core.md").read_text(encoding="utf-8")
    # A lane with no card can still be open.
    assert "missing card does not mean closed" in handoff
    assert "A missing card is not closed" in close
    # The successor never reuses an old lane's id (self-acks are impossible).
    assert "must differ from every old lane's id" in handoff
    # Late handovers are rung, not left in the inbox.
    assert "Ring for late handovers" in handoff
    # The close reply carries the full SELF report line.
    assert "this run's `SELF:` report line, verbatim" in close
    # --all closes the shipper last, and never while lanes are still open.
    assert "## ALL mode" in close and "Close the shipper, last" in close
    assert "do not close the shipper" in close
