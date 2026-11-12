"""Shared test fixtures.

Every test runs against a temporary store root through PASSING_NOTES_ROOT, and
LOCALAPPDATA points at another temporary folder, so no test can reach the real
%LOCALAPPDATA%\\passing-notes store or write inside the repository.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import pytest

from passing_notes import cli, protocol
from passing_notes.store import LOCALAPPDATA_ENV, ROOT_ENV, Store, find_git_worktree

REPO_ROOT = Path(__file__).resolve().parents[1]
# Committed fixture files: CRLF throughout, so no checkout setting changes their bytes
# (see .gitattributes and lf_file below).
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROJECT = "example-project"
LINK_KINDS = ("junction", "symlink")
# The synthetic creation time of test notes and acks (matches note_text's stamp).
CREATED = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
ESC = chr(27)  # starts a terminal escape sequence; must never reach the output raw
TAB = chr(9)
# Characters that hide or reorder text: each must reach the output only as an escape.
ZWSP = chr(0x200B)  # zero-width space
WORD_JOINER = chr(0x2060)
ZWNBSP = chr(0xFEFF)  # zero-width no-break space (the BOM)
RLO = chr(0x202E)  # right-to-left override: shows the rest of the line reversed
RLI = chr(0x2067)  # right-to-left isolate
HANGUL_FILLER = chr(0x3164)  # a letter that renders as nothing
VS16 = chr(0xFE0F)  # variation selector 16
NEL = chr(0x85)  # next line
LINE_SEPARATOR = chr(0x2028)
NBSP = chr(0xA0)  # visible as a space; not escaped
CYRILLIC_IE = chr(0x415)  # looks like "E"; not escaped
LONE_SURROGATE = "\udc80"  # an undecodable byte in POSIX argv; Windows argv can hold one too

MakeLink = Callable[[Path, Path, str], None]
StoreCall = Callable[[Store], object]


class Crash(BaseException):
    """Stands in for a process killed mid-publication (not caught as an OSError)."""


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
def opened(store_root: Path) -> Store:
    return Store.open()


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


class Invoke(Protocol):
    def __call__(self, *args: str, stdin: bytes | None = None) -> Result: ...


@pytest.fixture
def invoke(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> Invoke:
    """Call ``cli.main`` in-process; returns a Result."""

    def call(*args: str, stdin: bytes | None = None) -> Result:
        capsys.readouterr()
        if stdin is not None:
            monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin)))
        code = cli.main(list(args))
        out, err = capsys.readouterr()
        return Result(code, out, err)

    return call


def body_lines(result: Result, begin: str = cli.BODY_BEGIN, end: str = cli.BODY_END) -> list[str]:
    """The body printed between the ``begin`` and ``end`` frame lines (``read`` uses the
    defaults, ``card show --lane`` the CARD_BODY_ ones), without the gutter every body
    line carries."""
    lines = result.lines
    framed = lines[lines.index(begin) + 1 : lines.index(end)]
    bare = cli.BODY_GUTTER.rstrip()
    assert all(line == bare or line.startswith(cli.BODY_GUTTER) for line in framed), framed
    return [line[len(cli.BODY_GUTTER) :] for line in framed]


def card_set_args(
    lane: str = "lane-b",
    item: str = "Synthetic item",
    *extra: str,
    host: str = "claude",
    state: str = "active",
    project: str = PROJECT,
) -> list[str]:
    """A ``card set`` command with every required flag, then ``extra``."""
    return [
        "card",
        "set",
        "--project",
        project,
        "--lane",
        lane,
        "--host",
        host,
        "--item",
        item,
        "--state",
        state,
        *extra,
    ]


def unsafe_in(text: str) -> list[str]:
    """Characters of ``text`` that must only print escaped. Newlines end lines, and tabs
    are left out: a body line and the column separator of inbox and card lines print
    them as-is."""
    return [ch for ch in text if ch not in "\t\n" and protocol.is_unsafe_char(ch)]


# A child process that runs the production ``cli.main`` with a race made repeatable:
# the note time is fixed, the note hex comes from its arguments, and its first call of
# the store function named by ``held`` (the step that publishes a temporary file under
# its final name) waits until every racer has written its temporary file, so the
# publications collide.
RACER = """
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from passing_notes import cli, store

barrier, racers, held, hexes, *argv = sys.argv[1:]
values = iter(hexes.split(","))
store.default_hex = lambda: next(values)
store.default_now = lambda: datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
publish = getattr(store, held)
arrived = []


def publish_together(src, dst):
    if not arrived:
        arrived.append(src.name)
        Path(barrier, src.name).touch()
        deadline = time.monotonic() + 30
        while len(list(Path(barrier).iterdir())) < int(racers):
            if time.monotonic() > deadline:
                break
            time.sleep(0.01)
    publish(src, dst)


setattr(store, held, publish_together)
raise SystemExit(cli.main(argv))
"""


def run_together(
    store_root: Path, script: str, runs: Sequence[Sequence[str]], waiting: str
) -> list[Result]:
    """Start one ``python -c script`` per argument list at once; their results, in order.

    Before its own arguments each child gets a new barrier folder, shared by all, and
    the number of children. It adds a file there on reaching the point where every
    child waits for the others (``waiting``, named in the failure message); every child
    must have added one.
    """
    barrier = store_root.parent / "barrier"
    barrier.mkdir()
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(barrier), str(len(runs)), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=store_root.parent,
            env=child_env(store_root),
        )
        for args in runs
    ]
    results = []
    for proc in processes:
        out, err = proc.communicate(timeout=120)
        decoded = (data.decode("utf-8").replace("\r\n", "\n") for data in (out, err))
        results.append(Result(proc.returncode, *decoded))
    arrived = len(list(barrier.iterdir()))
    assert arrived == len(runs), f"a child never reached {waiting}: {[r.stderr for r in results]}"
    return results


def race(
    store_root: Path, runs: list[tuple[str, list[str]]], held: str = "rename_no_replace"
) -> list[Result]:
    """Start one racer per (hexes, arguments) at once; their results, in order.

    Every racer holds its first call of ``store.<held>`` until all have reached it.
    """
    return run_together(store_root, RACER, [[held, hexes, *args] for hexes, args in runs], held)


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


# A canonical card using every field: render_card must reproduce these exact bytes.
CARD_FULL = lf_file(
    "---",
    "lane: lane-b",
    "project: example-project",
    "host: codex",
    "window: example-window-1",
    "checkout: C:/work/example-project",
    "branch: step-2",
    "owns:",
    "  - src/example/",
    "  - docs/example.md",
    "holds:",
    "  - engine lock",
    "item: Step 2 tests",
    "state: active",
    "next_checkpoint: after the unit suite passes",
    "updated_utc: 2030-01-01T12:30:00Z",
    "---",
    "",
    "Synthetic card notes.",
    "Second line.",
)

# The smallest card: no optional field, no list entry and no body.
CARD_MINIMAL = lf_file(
    "---",
    "lane: lane-a",
    "project: example-project",
    "host: claude",
    "owns:",
    "holds:",
    "item: Synthetic item",
    "state: idle",
    "updated_utc: 2030-01-01T12:30:00Z",
    "---",
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


def place_card(root: Path, data: bytes, name: str, project: str = PROJECT) -> Path:
    """Write a card file into the store by hand, as a lane without the CLI would."""
    folder = root / "cards" / project
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
