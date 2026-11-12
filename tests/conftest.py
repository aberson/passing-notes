"""Shared test fixtures.

Every test runs against a temporary store root through PASSING_NOTES_ROOT, and
LOCALAPPDATA points at another temporary folder, so no test can reach the real
%LOCALAPPDATA%\\passing-notes store or write inside the repository.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from passing_notes.store import LOCALAPPDATA_ENV, ROOT_ENV, find_git_worktree

REPO_ROOT = Path(__file__).resolve().parents[1]
# Committed fixture files: CRLF throughout, so no checkout setting changes their bytes
# (see .gitattributes and lf_file below).
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROJECT = "example-project"
LINK_KINDS = ("junction", "symlink")
# The synthetic creation time of test notes and acks (matches note_text's stamp).
CREATED = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
ESC = chr(27)  # starts a terminal escape sequence; must never reach the output raw

MakeLink = Callable[[Path, Path, str], None]


@pytest.fixture(autouse=True)
def store_root(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh store root outside any Git working tree, set for in-process calls."""
    base = tmp_path_factory.mktemp("pn")
    repo = find_git_worktree(base)
    if repo is not None:
        pytest.fail(f"temporary folder {base} is inside the Git working tree {repo}")
    root = base / "store"
    monkeypatch.setenv(ROOT_ENV, str(root))
    monkeypatch.setenv(LOCALAPPDATA_ENV, str(base / "localappdata"))
    return root


@pytest.fixture
def dir_link() -> Iterator[MakeLink]:
    """Make ``link`` a directory junction or symbolic link to ``target``.

    Junctions need no privilege on Windows. Directory symlinks need Developer Mode
    or elevation there, so a refused one skips the test with the reason. Links
    (never their targets) are removed at teardown.
    """
    made: list[Path] = []

    def make(link: Path, target: Path, kind: str) -> None:
        if kind == "junction":
            if os.name != "nt":
                pytest.skip("directory junctions exist only on Windows")
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                reason = proc.stderr.decode("utf-8", "replace").strip()
                pytest.skip(f"cannot create a junction here: {reason}")
        else:
            try:
                os.symlink(target, link, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"cannot create a directory symlink without privilege: {exc}")
        made.append(link)

    yield make
    for link in reversed(made):
        try:
            if os.name == "nt":
                os.rmdir(link)  # removes the junction or symlink itself, not the target
            else:
                os.unlink(link)
        except OSError:
            pass


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def lines(self) -> list[str]:
        return self.stdout.splitlines()


def console_script() -> list[str]:
    """The installed ``passing-notes`` console script next to this interpreter."""
    name = "passing-notes.exe" if os.name == "nt" else "passing-notes"
    script = Path(sys.executable).with_name(name)
    if not script.exists():
        pytest.fail(f"console script {script} is missing; run `uv sync`")
    return [str(script)]


def module_command() -> list[str]:
    return [sys.executable, "-m", "passing_notes"]


def child_env(root: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env[ROOT_ENV] = str(root)
    env[LOCALAPPDATA_ENV] = str(root.parent / "localappdata")
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def run_cli(
    args: Sequence[str],
    *,
    root: Path,
    stdin: bytes | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    command: Sequence[str] | None = None,
) -> Result:
    """Run the real console entry point in a separate process."""
    proc = subprocess.run(
        [*(command or console_script()), *args],
        input=stdin if stdin is not None else b"",
        capture_output=True,
        cwd=cwd or root.parent,
        env=dict(env) if env is not None else child_env(root),
        timeout=120,
        check=False,
    )
    return Result(
        proc.returncode,
        proc.stdout.decode("utf-8").replace("\r\n", "\n"),
        proc.stderr.decode("utf-8").replace("\r\n", "\n"),
    )


def note_id(stamp: str, sender: str, recipient: str, hex4: str) -> str:
    return f"{stamp}-{sender}-to-{recipient}-{hex4}"


def note_text(
    *,
    stamp: str = "20300101T120000Z",
    created_utc: str = "2030-01-01T12:00:00Z",
    sender: str = "lane-a",
    recipient: str = "lane-b",
    hex4: str = "1a2b",
    project: str = PROJECT,
    subject: str = "Synthetic subject",
    needs: str = "ack",
    kind: str | None = "fyi",
    extra: Sequence[tuple[str, str]] = (),
    refs: Sequence[str] = (),
    body: str = "Synthetic body.",
    newline: str = "\n",
) -> str:
    """A hand-written style note; ``extra`` adds header lines before ``reply_to``."""
    lines = [
        "---",
        f"id: {note_id(stamp, sender, recipient, hex4)}",
        f"created_utc: {created_utc}",
        f"project: {project}",
        f"from: {sender}",
        f"to: {recipient}",
        f"subject: {subject}",
    ]
    if kind is not None:
        lines.append(f"kind: {kind}")
    lines.append(f"needs: {needs}")
    lines.extend(f"{key}: {value}" for key, value in extra)
    lines.append("reply_to: none")
    lines.append("refs:")
    lines.extend(f"  - {ref}" for ref in refs)
    lines.extend(["---", "", body])
    return newline.join(lines) + newline


def lf_file(*lines: str) -> bytes:
    """The bytes of a file whose every line ends in LF.

    Data whose exact LF bytes matter is built here instead of being committed under
    tests/fixtures/: Git rewrites an LF-only text file to CRLF when it writes it with
    core.autocrlf=true before .gitattributes is in effect (a landing computes every
    path's bytes before it writes any, so the change that adds .gitattributes is not
    covered by it). Each line is a one-line literal, so these bytes are the same
    whatever line endings this source file was checked out with.
    """
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


# A canonical v1 note using every field: render_note must reproduce these exact bytes.
V1_NOTE_FULL = lf_file(
    "---",
    "id: 20300101T121000Z-lane-b-to-lane-a-0c0c",
    "created_utc: 2030-01-01T12:10:00Z",
    "project: example-project",
    "from_project: other-project",
    "from: lane-b",
    "to: lane-a",
    "subject: Synthetic answer with every v1 field",
    "kind: answer",
    "needs: ack",
    "work: example-project Step 2",
    "revision: 0000000",
    "supersedes: 20300101T120900Z-lane-b-to-lane-a-0d0d",
    "reply_to: 20300101T120000Z-lane-a-to-lane-b-0a0a",
    "idempotency_key: example-key.1",
    "refs:",
    "  - commit 0000000",
    "---",
    "",
    "Synthetic body.",
)

# A hand-written v0 note to every lane, with LF line endings and no kind.
V0_NOTE_TO_ALL = lf_file(
    "---",
    "id: 20300101T120100Z-lane-c-to-all-0b0b",
    "created_utc: 2030-01-01T12:01:00Z",
    "project: example-project",
    "from: lane-c",
    "to: all",
    "subject: Synthetic hello to every lane",
    "needs: fyi",
    "reply_to: none",
    "refs:",
    "---",
    "",
    "Synthetic introduction.",
)


def place_note(root: Path, text: str | bytes, name: str, project: str = PROJECT) -> Path:
    """Write a note file into the store by hand, as a lane without the CLI would."""
    folder = root / "messages" / project
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return path


def place_ack(root: Path, data: bytes, name: str, project: str = PROJECT) -> Path:
    folder = root / "acks" / project
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def snapshot(folder: Path) -> dict[str, bytes]:
    """Every file under ``folder`` with its bytes (empty if it does not exist)."""
    if not folder.exists():
        return {}
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }
