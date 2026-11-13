"""Build and install the passing-notes lane skills for Claude Code and Codex.

The skills (lane-open, lane-close, lane-handoff) live in ``skills/<name>/`` in Skill
Mesh's source shape: a provider-neutral ``core.md`` plus thin ``providers/<host>.md``
wrappers. They are not in the Skill Mesh catalog (operator decision 2026-11-13), so
this tool builds and installs them itself, the Skill Mesh way:

* **build** writes, per host, ``<name>/SKILL.md`` (YAML frontmatter, a generated-file
  header with this tool's own marker, then the wrapper text), ``<name>/core.md`` and
  the skill's support files, all self-contained in the skill folder.
* **install** copies one host's build into its discovery root and records every file
  it wrote, with its SHA-256, in its own ledger. It refuses to overwrite any file it
  does not own, or one whose bytes no longer match the ledger, and refuses a skill
  name that Skill Mesh's manifest also claims. It never touches another skill folder.
* **uninstall** removes only owned files whose bytes still match the ledger. A
  tampered or unreadable entry is kept and reported, and a corrupt ledger deletes
  nothing.

Usage (from the passing-notes checkout)::

    uv run python tools/skills_install.py build --out <folder>
    uv run python tools/skills_install.py install --host claude|codex [--root <skills folder>]
    uv run python tools/skills_install.py uninstall --host claude|codex [--root <skills folder>]
    uv run python tools/skills_install.py status --host claude|codex [--root <skills folder>]

Exit codes: 0 done; 1 refused or partly refused (nothing unsafe was written or
deleted); 2 bad arguments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
SKILLS_SOURCE: Final = REPO_ROOT / "skills"
SKILLS: Final = ("lane-open", "lane-close", "lane-handoff")
HOSTS: Final = ("claude", "codex")
MARKER: Final = "PASSING-NOTES-GENERATED-FILE"
LEDGER_NAME: Final = ".passing-notes-skills-install.json"
LEDGER_TOOL: Final = "passing-notes-skills"
LEDGER_VERSION: Final = 1
SKIP_SUPPORT: Final = frozenset({"core.md", "providers", "skill-mesh-entry.json"})


class Refused(Exception):
    """The operation would be unsafe; nothing unsafe was done."""


@dataclass(frozen=True)
class Report:
    written: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()


# --- Paths -----------------------------------------------------------------------------


def coding_root() -> Path:
    """The workspace that holds this checkout (``<coding-root>/passing-notes``)."""
    return REPO_ROOT.parent


def default_root(host: str) -> Path:
    if host == "claude":
        return coding_root() / ".claude" / "skills"
    return Path.home() / ".agents" / "skills"


def ledger_path(root: Path) -> Path:
    """Beside the skills folder, like Skill Mesh's ``<home>/.skill-mesh-install.json``."""
    return root.parent / LEDGER_NAME


def default_manifest() -> Path:
    return coding_root() / "skill-mesh" / "config" / "skill-manifest.json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- Build -----------------------------------------------------------------------------


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5 :].lstrip("\n")
    return text


def _header(source: str, host: str) -> str:
    return (
        "<!-- GENERATED FILE - DO NOT EDIT.\n"
        f"     Marker: {MARKER}\n"
        "     Produced by passing-notes tools/skills_install.py.\n"
        f"     Canonical source: passing-notes/{source}\n"
        f"     Profile: {host}\n"
        "     Edit the canonical source and rebuild; edits here are overwritten. -->\n"
    )


def _entry(name: str, source: Path) -> Mapping[str, object]:
    data = json.loads((source / name / "skill-mesh-entry.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("name") != name:
        raise Refused(f"{name}: skill-mesh-entry.json does not name {name}")
    return data


def render_skill_md(name: str, host: str, source: Path = SKILLS_SOURCE) -> str:
    entry = _entry(name, source)
    description = entry.get("description")
    if not isinstance(description, str) or not description:
        raise Refused(f"{name}: skill-mesh-entry.json has no description")
    wrapper = (source / name / "providers" / f"{host}.md").read_text(encoding="utf-8")
    body = _strip_frontmatter(wrapper).replace("](../core.md)", "](core.md)")
    body = body.replace("[../core.md]", "[core.md]")
    front = ["---", f"name: {name}", f"description: {json.dumps(description)}"]
    if host == "claude":
        front.append("user-invocable: true")
    front.append("---")
    header = _header(f"skills/{name}/providers/{host}.md", host)
    return "\n".join(front) + "\n" + header + "\n" + body


def build_files(host: str, source: Path = SKILLS_SOURCE) -> dict[str, bytes]:
    """Every file of one host's build, keyed by ``<skill>/<relative path>`` (``/``)."""
    if host not in HOSTS:
        raise Refused(f"unknown host {host}")
    files: dict[str, bytes] = {}
    for name in SKILLS:
        folder = source / name
        files[f"{name}/SKILL.md"] = render_skill_md(name, host, source).encode("utf-8")
        core = (folder / "core.md").read_text(encoding="utf-8")
        files[f"{name}/core.md"] = (_header(f"skills/{name}/core.md", host) + "\n" + core).encode(
            "utf-8"
        )
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(folder)
            if path.is_file() and rel.parts[0] not in SKIP_SUPPORT:
                files[f"{name}/{rel.as_posix()}"] = path.read_bytes()
    return files


def build(out: Path, source: Path = SKILLS_SOURCE) -> Report:
    written: list[str] = []
    for host in HOSTS:
        for rel, data in build_files(host, source).items():
            target = out / host / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            written.append(f"{host}/{rel}")
    return Report(written=tuple(written))


# --- Ledger ----------------------------------------------------------------------------


def read_ledger(path: Path) -> dict[str, str]:
    """Owned files (``<skill>/<rel>`` -> sha256). Missing: empty. Corrupt: Refused."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refused(f"ledger {path} is unreadable ({exc}); nothing changed") from None
    files = data.get("files") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("tool") != LEDGER_TOOL
        or data.get("version") != LEDGER_VERSION
        or not isinstance(files, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())
    ):
        raise Refused(f"ledger {path} is not a {LEDGER_TOOL} v{LEDGER_VERSION} ledger")
    for rel in files:
        _check_rel(rel)
    return dict(files)


def write_ledger(path: Path, files: Mapping[str, str]) -> None:
    body = {"tool": LEDGER_TOOL, "version": LEDGER_VERSION, "files": dict(sorted(files.items()))}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _check_rel(rel: str) -> None:
    """A ledger key must stay inside one of this tool's skill folders."""
    parts = rel.split("/")
    if parts[0] not in SKILLS or len(parts) < 2 or any(p in ("", ".", "..") for p in parts):
        raise Refused(f"ledger entry {rel!r} is outside the lane skills; nothing changed")


def _target(root: Path, rel: str) -> Path:
    _check_rel(rel)
    return root.joinpath(*rel.split("/"))


# --- Install / uninstall ---------------------------------------------------------------


def manifest_names(manifest: Path) -> set[str]:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    skills = data.get("skills", []) if isinstance(data, dict) else []
    return {s["name"] for s in skills if isinstance(s, dict) and isinstance(s.get("name"), str)}


def install(host: str, root: Path, manifest: Path | None, source: Path = SKILLS_SOURCE) -> Report:
    if manifest is not None and manifest.exists():
        clash = sorted(manifest_names(manifest) & set(SKILLS))
        if clash:
            raise Refused(f"Skill Mesh's manifest already claims {', '.join(clash)}")
    if not root.is_dir():
        raise Refused(f"skills folder {root} does not exist")
    ledger = ledger_path(root)
    owned = read_ledger(ledger)
    files = build_files(host, source)

    # Check everything before writing anything.
    problems: list[str] = []
    for rel in files:
        target = _target(root, rel)
        if target.exists():
            if rel not in owned:
                problems.append(f"{target} exists and is not owned by {LEDGER_TOOL}")
            elif sha256(target.read_bytes()) != owned[rel]:
                problems.append(f"{target} was changed since it was installed")
    for name in SKILLS:
        folder = root / name
        if folder.is_dir():
            for path in folder.rglob("*"):
                rel = f"{name}/{path.relative_to(folder).as_posix()}"
                if path.is_file() and rel not in owned and rel not in files:
                    problems.append(f"{path} is in a lane skill folder but not owned")
    if problems:
        raise Refused("; ".join(problems))

    written: list[str] = []
    new_owned = dict(owned)
    for rel, data in files.items():
        target = _target(root, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and sha256(target.read_bytes()) == sha256(data):
            new_owned[rel] = sha256(data)
            continue
        tmp = target.with_name(target.name + ".pn-tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        new_owned[rel] = sha256(data)
        written.append(rel)
    # Files this tool owned before that the new build no longer has.
    removed: list[str] = []
    kept: list[str] = []
    for rel in sorted(set(owned) - set(files)):
        target = _target(root, rel)
        if not target.exists():
            new_owned.pop(rel, None)
        elif sha256(target.read_bytes()) == owned[rel]:
            target.unlink()
            new_owned.pop(rel, None)
            removed.append(rel)
        else:
            kept.append(rel)
    write_ledger(ledger, new_owned)
    _prune_empty(root)
    return Report(tuple(written), tuple(removed), tuple(kept))


def uninstall(root: Path) -> Report:
    ledger = ledger_path(root)
    owned = read_ledger(ledger)
    removed: list[str] = []
    kept: list[str] = []
    remaining: dict[str, str] = {}
    for rel, digest in sorted(owned.items()):
        target = _target(root, rel)
        if not target.exists():
            continue
        if target.is_file() and sha256(target.read_bytes()) == digest:
            target.unlink()
            removed.append(rel)
        else:
            kept.append(rel)
            remaining[rel] = digest
    if remaining:
        write_ledger(ledger, remaining)
    elif ledger.exists():
        ledger.unlink()
    _prune_empty(root)
    return Report(removed=tuple(removed), kept=tuple(kept))


def _prune_empty(root: Path) -> None:
    """Remove empty folders inside the lane skill folders only."""
    for name in SKILLS:
        folder = root / name
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        if not any(folder.iterdir()):
            folder.rmdir()


# --- CLI -------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skills_install", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build both hosts' skill folders into --out")
    b.add_argument("--out", type=Path, required=True)
    for cmd in ("install", "uninstall", "status"):
        p = sub.add_parser(cmd)
        p.add_argument("--host", choices=HOSTS, required=True)
        p.add_argument("--root", type=Path, help="the host's skills folder")
        if cmd == "install":
            p.add_argument("--manifest", type=Path, default=default_manifest())
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            report = build(args.out)
            print(f"built {len(report.written)} files into {args.out}")
            return 0
        root: Path = args.root or default_root(args.host)
        if args.command == "status":
            owned = read_ledger(ledger_path(root))
            for rel, digest in sorted(owned.items()):
                target = _target(root, rel)
                state = "missing"
                if target.exists():
                    state = "ok" if sha256(target.read_bytes()) == digest else "changed"
                print(f"{state}\t{rel}")
            print(f"{len(owned)} owned files in {root}")
            return 0
        if args.command == "install":
            report = install(args.host, root, args.manifest)
        else:
            report = uninstall(root)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    for rel in report.written:
        print(f"wrote\t{rel}")
    for rel in report.removed:
        print(f"removed\t{rel}")
    for rel in report.kept:
        print(f"kept (changed or not a file, not deleted)\t{rel}")
    print(
        f"{args.command} {getattr(args, 'host', '')}: {len(report.written)} written, "
        f"{len(report.removed)} removed, {len(report.kept)} kept, in {root}"
    )
    return 1 if report.kept else 0


if __name__ == "__main__":
    sys.exit(main())
