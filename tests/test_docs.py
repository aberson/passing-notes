"""README, lane rules and diagrams: generated files current, links and values real."""

from __future__ import annotations

import importlib.util
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest

from passing_notes import cli, protocol, wake

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
