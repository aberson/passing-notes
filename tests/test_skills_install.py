"""tools/skills_install.py: build, install, reinstall and uninstall in temporary folders."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "skills_install.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("skills_install", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["skills_install"] = module
    spec.loader.exec_module(module)
    return module


si = _load()


def test_support_python_cache_is_not_installed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(si.SKILLS_SOURCE, source, ignore=shutil.ignore_patterns("__pycache__"))
    cache = source / "lane-open" / "scripts" / "__pycache__"
    cache.mkdir()
    (cache / "synthetic.pyc").write_bytes(b"synthetic cache")
    files = si.build_files("codex", source)
    assert "lane-open/scripts/codex-preflight.py" in files
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in files)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    skills = tmp_path / "home" / "skills"
    skills.mkdir(parents=True)
    (skills / "other-skill").mkdir()
    (skills / "other-skill" / "SKILL.md").write_text("not ours\n", encoding="utf-8")
    return skills


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    path = tmp_path / "skill-manifest.json"
    path.write_text(json.dumps({"skills": [{"name": "user-project"}]}), encoding="utf-8")
    return path


def frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n")
    block = text[4 : text.index("\n---\n", 4)]
    out: dict[str, str] = {}
    for line in block.splitlines():
        key, _, value = line.partition(": ")
        out[key] = json.loads(value) if value.startswith('"') else value
    return out


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_build_is_self_contained_and_marked(host: str) -> None:
    files = si.build_files(host)
    assert set(files) >= {f"{n}/{f}" for n in si.SKILLS for f in ("SKILL.md", "core.md")}
    assert "lane-open/scripts/open-lane.ps1" in files
    assert "lane-close/scripts/close_done_window.py" in files
    assert not any("providers" in rel or "skill-mesh-entry" in rel for rel in files)
    for name in si.SKILLS:
        skill = files[f"{name}/SKILL.md"].decode("utf-8")
        front = frontmatter(skill)
        assert front["name"] == name and front["description"]
        assert ("user-invocable" in front) == (host == "claude")
        assert si.MARKER in skill and "SKILL-MESH-GENERATED-FILE" not in skill
        assert "[core.md](core.md)" in skill and "../core.md" not in skill
        assert f"{host} entry point" in skill.lower() or f"{host} provider" in skill.lower()
        assert si.MARKER in files[f"{name}/core.md"].decode("utf-8")


def test_install_reinstall_uninstall_round_trip(root: Path, manifest: Path) -> None:
    first = si.install("codex", root, manifest)
    assert "lane-open/SKILL.md" in first.written
    ledger = json.loads(si.ledger_path(root).read_text(encoding="utf-8"))
    assert ledger["tool"] == si.LEDGER_TOOL and len(ledger["files"]) == len(first.written)
    again = si.install("codex", root, manifest)
    assert again.written == () and again.removed == ()  # nothing changed
    gone = si.uninstall(root)
    assert set(gone.removed) == set(first.written) and gone.kept == ()
    assert not si.ledger_path(root).exists()
    assert not any((root / n).exists() for n in si.SKILLS)
    assert (root / "other-skill" / "SKILL.md").read_text(encoding="utf-8") == "not ours\n"


def test_uninstall_refuses_a_tampered_file(root: Path, manifest: Path) -> None:
    si.install("claude", root, manifest)
    tampered = root / "lane-close" / "core.md"
    tampered.write_text("edited by hand\n", encoding="utf-8")
    report = si.uninstall(root)
    assert report.kept == ("lane-close/core.md",)
    assert tampered.read_text(encoding="utf-8") == "edited by hand\n"
    assert si.read_ledger(si.ledger_path(root)) == {
        "lane-close/core.md": json.loads(si.ledger_path(root).read_text())["files"][
            "lane-close/core.md"
        ]
    }
    assert si.main(["uninstall", "--host", "claude", "--root", str(root)]) == 1


def test_install_refuses_to_overwrite_a_file_it_does_not_own(root: Path, manifest: Path) -> None:
    (root / "lane-open").mkdir()
    foreign = root / "lane-open" / "SKILL.md"
    foreign.write_text("someone else's skill\n", encoding="utf-8")
    with pytest.raises(si.Refused, match="not owned"):
        si.install("claude", root, manifest)
    assert foreign.read_text(encoding="utf-8") == "someone else's skill\n"
    assert not si.ledger_path(root).exists()
    assert not (root / "lane-close").exists()  # checked before anything was written


def test_install_refuses_a_changed_owned_file(root: Path, manifest: Path) -> None:
    si.install("claude", root, manifest)
    (root / "lane-open" / "SKILL.md").write_text("hand edit\n", encoding="utf-8")
    with pytest.raises(si.Refused, match="changed since it was installed"):
        si.install("claude", root, manifest)


def test_install_refuses_names_skill_mesh_claims(root: Path, tmp_path: Path) -> None:
    clash = tmp_path / "clash.json"
    clash.write_text(json.dumps({"skills": [{"name": "lane-close"}]}), encoding="utf-8")
    with pytest.raises(si.Refused, match="lane-close"):
        si.install("claude", root, clash)
    assert not si.ledger_path(root).exists()


def test_corrupt_ledger_deletes_nothing(root: Path, manifest: Path) -> None:
    si.install("codex", root, manifest)
    si.ledger_path(root).write_text("{broken", encoding="utf-8")
    with pytest.raises(si.Refused, match="unreadable"):
        si.uninstall(root)
    assert (root / "lane-open" / "SKILL.md").exists()


def test_ledger_entries_outside_the_lane_skills_are_refused(root: Path) -> None:
    (root / "other-skill" / "keep.md").write_text("x", encoding="utf-8")
    si.write_ledger(si.ledger_path(root), {"other-skill/keep.md": si.sha256(b"x")})
    with pytest.raises(si.Refused, match="outside the lane skills"):
        si.uninstall(root)
    assert (root / "other-skill" / "keep.md").exists()


def test_build_command_writes_both_hosts(tmp_path: Path) -> None:
    assert si.main(["build", "--out", str(tmp_path / "out")]) == 0
    for host in ("claude", "codex"):
        assert (tmp_path / "out" / host / "lane-handoff" / "SKILL.md").is_file()
    assert not (tmp_path / "out" / "gpt").exists()


def test_build_command_emits_lane_status_for_both_hosts(tmp_path: Path) -> None:
    """Steps 17 and 25: ``build --out`` writes lane-status/SKILL.md, core.md and the
    bounded plan helper per host, and nothing else for that skill."""
    assert si.main(["build", "--out", str(tmp_path / "out")]) == 0
    source = si.SKILLS_SOURCE / "lane-status" / "scripts" / "plan_progress.py"
    for host in si.HOSTS:
        folder = tmp_path / "out" / host / "lane-status"
        assert sorted(
            p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()
        ) == ["SKILL.md", "core.md", "scripts/plan_progress.py"]
        assert (folder / "scripts" / "plan_progress.py").read_bytes() == source.read_bytes()
        skill = (folder / "SKILL.md").read_text(encoding="utf-8")
        assert frontmatter(skill)["name"] == "lane-status"
        assert si.MARKER in skill and "[core.md](core.md)" in skill
        core = (folder / "core.md").read_text(encoding="utf-8")
        assert si.MARKER in core and "passing-notes status --all-projects --json" in core


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_lane_status_installs_and_uninstalls_in_a_temporary_root(
    host: str, root: Path, manifest: Path
) -> None:
    report = si.install(host, root, manifest)
    expected = {
        "lane-status/SKILL.md",
        "lane-status/core.md",
        "lane-status/scripts/plan_progress.py",
    }
    assert expected <= set(report.written)
    skill = (root / "lane-status" / "SKILL.md").read_text(encoding="utf-8")
    assert ("user-invocable" in frontmatter(skill)) == (host == "claude")
    assert f"lane-status - {host} entry point" in skill.lower()
    owned = json.loads(si.ledger_path(root).read_text(encoding="utf-8"))["files"]
    assert expected <= set(owned)
    gone = si.uninstall(root)
    assert expected <= set(gone.removed)
    assert not (root / "lane-status").exists()
    assert (root / "other-skill" / "SKILL.md").read_text(encoding="utf-8") == "not ours\n"


def test_install_refuses_lane_status_when_skill_mesh_claims_it(root: Path, tmp_path: Path) -> None:
    clash = tmp_path / "clash.json"
    clash.write_text(json.dumps({"skills": [{"name": "lane-status"}]}), encoding="utf-8")
    with pytest.raises(si.Refused, match="lane-status"):
        si.install("codex", root, clash)
    assert not si.ledger_path(root).exists() and not (root / "lane-status").exists()


def test_skill_mesh_manifest_does_not_claim_the_lane_skills() -> None:
    """The real Skill Mesh manifest (read only) claims none of this tool's names, so
    install would not refuse them. Skipped where the sibling checkout is absent."""
    manifest = si.default_manifest()
    if not manifest.is_file():
        pytest.skip(f"no Skill Mesh manifest at {manifest}")
    assert {"lane-status", "coordinator-handover"} <= set(si.SKILLS)
    assert si.manifest_names(manifest) & set(si.SKILLS) == set()


HANDOVER = "coordinator-handover"


def test_build_command_emits_coordinator_handover_for_both_hosts(tmp_path: Path) -> None:
    """Step 23: ``build --out`` writes coordinator-handover/SKILL.md and core.md per host,
    and nothing else for that skill (it has no support files)."""
    assert HANDOVER in si.SKILLS
    assert si.main(["build", "--out", str(tmp_path / "out")]) == 0
    for host in si.HOSTS:
        folder = tmp_path / "out" / host / HANDOVER
        assert sorted(p.name for p in folder.rglob("*")) == ["SKILL.md", "core.md"]
        skill = (folder / "SKILL.md").read_text(encoding="utf-8")
        front = frontmatter(skill)
        assert front["name"] == HANDOVER and front["description"]
        assert ("user-invocable" in front) == (host == "claude")
        assert f"{HANDOVER} - {host} entry point" in skill.lower()
        assert si.MARKER in skill and "[core.md](core.md)" in skill
        core = (folder / "core.md").read_text(encoding="utf-8")
        assert si.MARKER in core and "# Coordinator Handover" in core


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_coordinator_handover_installs_and_uninstalls_in_a_temporary_root(
    host: str, root: Path, manifest: Path
) -> None:
    pair = {f"{HANDOVER}/SKILL.md", f"{HANDOVER}/core.md"}
    report = si.install(host, root, manifest)
    assert pair <= set(report.written)
    owned = json.loads(si.ledger_path(root).read_text(encoding="utf-8"))["files"]
    assert pair <= set(owned)
    assert si.install(host, root, manifest).written == ()  # a reinstall changes nothing
    gone = si.uninstall(root)
    assert pair <= set(gone.removed) and gone.kept == ()
    assert not (root / HANDOVER).exists()
    assert (root / "other-skill" / "SKILL.md").read_text(encoding="utf-8") == "not ours\n"


def test_install_refuses_coordinator_handover_when_skill_mesh_claims_it(
    root: Path, tmp_path: Path
) -> None:
    clash = tmp_path / "clash.json"
    clash.write_text(json.dumps({"skills": [{"name": HANDOVER}]}), encoding="utf-8")
    with pytest.raises(si.Refused, match=HANDOVER):
        si.install("claude", root, clash)
    assert not si.ledger_path(root).exists() and not (root / HANDOVER).exists()


def test_coordinator_handover_package_matches_its_skill_mesh_entry() -> None:
    skill = si.SKILLS_SOURCE / HANDOVER
    entry = json.loads((skill / "skill-mesh-entry.json").read_text(encoding="utf-8"))
    assert entry["name"] == HANDOVER and entry["support_assets"] == []
    assert set(entry["providers"]) == {"claude", "codex", "gpt"}
    prefix = f"skills/{HANDOVER}/"
    for rel in [entry["core"], *entry["providers"].values()]:
        assert rel.startswith(prefix)
        assert (skill / rel.removeprefix(prefix)).is_file(), rel
    for provider in entry["providers"].values():
        text = (skill / provider.removeprefix(prefix)).read_text(encoding="utf-8")
        assert "[../core.md](../core.md)" in text
    claude = (skill / "providers" / "claude.md").read_text(encoding="utf-8")
    assert claude.startswith(f"---\nname: {HANDOVER}\n")
