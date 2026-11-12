"""The cross-host smoke (plan Step 3) without real hosts.

Covers the store-only checker's calibration (a complete synthetic exchange passes; a
missing ack, a wrong reader, a broken ``reply_to`` and a missing card each fail it),
the cleanup guard, and the driver end to end with simulated hosts: a perfect host runs
each prompt's commands through the real CLI entry point, so the prompts themselves are
checked too.

Nothing here calls a real model or touches the real store. Every store root, receipt
folder and working folder is temporary, and conftest's ``store_root`` fixture points
PASSING_NOTES_ROOT and LOCALAPPDATA at temporary folders as well.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import REPO_ROOT, MakeLink, snapshot
from cross_host import run_smoke
from cross_host.check_store import (
    CLAUDE_ANSWER,
    CLAUDE_LANE,
    CLAUDE_QUESTION,
    CODEX_ANSWER,
    CODEX_LANE,
    CODEX_QUESTION,
    LANE_HOSTS,
    PHASES,
    NoteRole,
    Phase,
    check_store,
)
from cross_host.run_smoke import (
    FAIL,
    NOTE_ID_PLACEHOLDER,
    PASS,
    CleanupRefused,
    HostOutput,
    SmokeFailure,
    guard_cleanup_path,
    remove_run_folder,
    run_folders,
)

from passing_notes import cli, protocol
from passing_notes.store import ROOT_ENV, Store, find_git_worktree, resolve_root

STARTED = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
PROJECT = run_smoke.smoke_project(STARTED)
STAMP = PROJECT.removeprefix(run_smoke.SMOKE_PROJECT_PREFIX)
COMMAND_RE = re.compile(r"[0-9]+\. (passing-notes .+)")
# Simulated host output that must never reach a receipt.
TRANSCRIPT_MARKER = "synthetic-transcript-marker-7f3a"
ROLES = (CLAUDE_QUESTION, CODEX_ANSWER, CODEX_QUESTION, CLAUDE_ANSWER)
ACK_PHASES = tuple(phase for phase in PHASES if phase.acks is not None)
REPLY_ROLES = (CODEX_ANSWER, CLAUDE_ANSWER)


# --- A perfect simulated host ---------------------------------------------------------


def call_cli(args: Sequence[str]) -> tuple[int, str, str]:
    """``cli.main`` in-process, with its standard output and error captured."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


def perform_phase(
    prompt: str, phase: Phase, cwd: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Act as a host that follows ``prompt`` exactly: run each numbered command through
    the real CLI entry point, from ``cwd``, against the store ``root``.

    The placeholder becomes the id of the one inbox line from the acked note's sender.
    """
    commands = [m[1] for line in prompt.splitlines() if (m := COMMAND_RE.fullmatch(line))]
    assert commands, prompt
    note_id: str | None = None
    with monkeypatch.context() as patch:
        patch.setenv(ROOT_ENV, str(root))
        patch.chdir(cwd)
        for command in commands:
            args = shlex.split(command)
            assert args[0] == run_smoke.CLI_NAME, command
            if NOTE_ID_PLACEHOLDER in args:
                assert note_id is not None, command
                args = [note_id if arg == NOTE_ID_PLACEHOLDER else arg for arg in args]
            code, out, err = call_cli(args[1:])
            assert code == 0, (command, err)
            if args[1] == "inbox":
                assert phase.acks is not None
                rows = [line.split("\t") for line in out.splitlines() if "\t" in line]
                found = [row[0] for row in rows if row[3] == phase.acks.sender]
                assert len(found) == 1, out
                note_id = found[0]


def build_exchange(
    root: Path, work: Path, monkeypatch: pytest.MonkeyPatch, *, through: int = len(PHASES)
) -> None:
    """The first ``through`` phases, run from their prompts by a perfect host."""
    for phase in PHASES[:through]:
        folder = work / phase.host
        folder.mkdir(parents=True, exist_ok=True)
        run_smoke.write_bodies(folder, phase.lane)
        perform_phase(run_smoke.build_prompt(phase, PROJECT), phase, folder, root, monkeypatch)


@pytest.fixture
def exchange(store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store root holding the complete synthetic exchange in PROJECT."""
    build_exchange(store_root, tmp_path / "work", monkeypatch)
    return store_root


def note_ids(root: Path) -> Mapping[str, str]:
    check = check_store(root, PROJECT)
    assert check.passed, check.failures
    return check.note_ids


def replace_text(path: Path, old: str, new: str) -> None:
    data = path.read_bytes()
    assert old.encode("utf-8") in data, (path, old)
    path.write_bytes(data.replace(old.encode("utf-8"), new.encode("utf-8"), 1))


def assert_fails(root: Path, *fragments: str) -> None:
    check = check_store(root, PROJECT)
    assert not check.passed
    joined = "\n".join(check.failures)
    for fragment in fragments:
        assert fragment in joined, joined


# --- The exchange itself --------------------------------------------------------------


def test_phases_cover_both_directions_and_each_host_sets_its_card_once() -> None:
    assert dict(LANE_HOSTS) == {CLAUDE_LANE: "claude", CODEX_LANE: "codex"}
    for phase in PHASES:
        if phase.posts is not None:
            assert phase.posts.sender == phase.lane
        if phase.acks is not None:
            assert phase.acks.recipient == phase.lane  # only a recipient may ack
        if phase.posts is not None and phase.posts.reply_to is not None:
            assert phase.acks is not None and phase.posts.reply_to == phase.acks.name
    posted = [phase.posts for phase in PHASES if phase.posts is not None]
    acked = [phase.acks for phase in PHASES if phase.acks is not None]
    assert posted == list(ROLES) and sorted(r.name for r in acked) == sorted(r.name for r in ROLES)
    assert [p.lane for p in PHASES if p.sets_card] == [CLAUDE_LANE, CODEX_LANE]
    for lane in LANE_HOSTS:  # a host's card comes with its first phase
        first = next(phase for phase in PHASES if phase.lane == lane)
        assert first.sets_card
    # Claude opens one exchange and Codex the other; each is answered by the other host.
    assert (CLAUDE_QUESTION.sender, CODEX_ANSWER.sender) == (CLAUDE_LANE, CODEX_LANE)
    assert (CODEX_QUESTION.sender, CLAUDE_ANSWER.sender) == (CODEX_LANE, CLAUDE_LANE)


def test_prompts_hold_only_synthetic_cli_commands() -> None:
    for phase in PHASES:
        prompt = run_smoke.build_prompt(phase, PROJECT)
        commands = [m[1] for line in prompt.splitlines() if (m := COMMAND_RE.fullmatch(line))]
        assert all(f"--project {PROJECT}" in command for command in commands)
        replies = phase.posts is not None and phase.posts.reply_to is not None
        assert (NOTE_ID_PLACEHOLDER in prompt) == (phase.acks is not None)
        assert ("--reply-to" in prompt) == replies
        assert "synthetic" in prompt.lower()
        assert ":\\" not in prompt and ":/" not in prompt  # no real path, only relative names


# --- Checker calibration --------------------------------------------------------------


def test_complete_synthetic_exchange_passes(exchange: Path) -> None:
    check = check_store(exchange, PROJECT)
    assert check.passed, check.failures
    assert set(check.note_ids) == {role.name for role in ROLES}
    assert len(check.ack_files) == len(ACK_PHASES)
    assert dict(check.cards) == {CLAUDE_LANE: "claude", CODEX_LANE: "codex"}
    store = Store(exchange)
    for role in REPLY_ROLES:
        assert role.reply_to is not None
        note = store.load_note(PROJECT, check.note_ids[role.name])
        assert note.reply_to == check.note_ids[role.reply_to]


def test_the_checker_only_reads(exchange: Path) -> None:
    before = snapshot(exchange)
    check_store(exchange, PROJECT)
    assert snapshot(exchange) == before


@pytest.mark.parametrize("phase", ACK_PHASES, ids=lambda phase: phase.name)
def test_a_missing_ack_fails(exchange: Path, phase: Phase) -> None:
    assert phase.acks is not None
    note_id = note_ids(exchange)[phase.acks.name]
    Store(exchange).ack_path(PROJECT, note_id, phase.lane).unlink()
    assert_fails(exchange, f"ack of {phase.acks.name} by {phase.lane}: no ack file")


@pytest.mark.parametrize("reader", [CLAUDE_LANE, "other-lane"])
def test_an_ack_by_the_wrong_reader_fails(exchange: Path, reader: str) -> None:
    note_id = note_ids(exchange)[CLAUDE_QUESTION.name]
    store = Store(exchange)
    store.ack_path(PROJECT, note_id, CODEX_LANE).unlink()
    ack = protocol.make_ack(reader, STARTED)
    store.ack_path(PROJECT, note_id, reader).write_bytes(protocol.render_ack(ack).encode())
    assert_fails(
        exchange,
        f"ack of {CLAUDE_QUESTION.name} by {CODEX_LANE}: no ack file",
        f"unexpected ack {protocol.ack_file_name(note_id, reader)}",
    )


def test_an_ack_file_whose_reader_line_names_another_lane_fails(exchange: Path) -> None:
    note_id = note_ids(exchange)[CLAUDE_QUESTION.name]
    path = Store(exchange).ack_path(PROJECT, note_id, CODEX_LANE)
    replace_text(path, f"reader: {CODEX_LANE}", f"reader: {CLAUDE_LANE}")
    assert_fails(exchange, "store diagnostic", "reader does not match the file name")


@pytest.mark.parametrize("role", REPLY_ROLES, ids=lambda role: role.name)
@pytest.mark.parametrize("target", ["none", "a missing note", "another note"])
def test_a_broken_reply_to_fails(exchange: Path, role: NoteRole, target: str) -> None:
    ids = note_ids(exchange)
    assert role.reply_to is not None
    other = ids[CLAUDE_QUESTION.name if role is CLAUDE_ANSWER else CODEX_QUESTION.name]
    new = {
        "none": protocol.REPLY_TO_NONE,
        "a missing note": "20300101T000000Z-lane-x-to-lane-y-0000",
        "another note": other,
    }[target]
    path = Store(exchange).note_path(PROJECT, ids[role.name])
    replace_text(path, f"reply_to: {ids[role.reply_to]}", f"reply_to: {new}")
    assert_fails(exchange, role.name)


@pytest.mark.parametrize("lane", list(LANE_HOSTS))
def test_a_missing_card_fails(exchange: Path, lane: str) -> None:
    Store(exchange).card_path(PROJECT, lane).unlink()
    assert_fails(exchange, f"card of {lane}: missing")


def test_a_card_naming_the_wrong_host_fails(exchange: Path) -> None:
    replace_text(Store(exchange).card_path(PROJECT, CODEX_LANE), "host: codex", "host: claude")
    assert_fails(exchange, f"card of {CODEX_LANE}: host is claude, expected codex")


def test_a_partial_check_covers_only_the_phases_run(
    store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_exchange(store_root, tmp_path / "work", monkeypatch, through=1)
    assert check_store(store_root, PROJECT, through=1).passed
    assert not check_store(store_root, PROJECT, through=2).passed
    assert_fails(store_root, f"card of {CODEX_LANE}: missing", f"{CODEX_ANSWER.name}: no note")


def test_an_empty_store_fails(store_root: Path) -> None:
    assert_fails(store_root, f"{CLAUDE_QUESTION.name}: no note", f"card of {CLAUDE_LANE}")


def test_unexpected_notes_cards_and_stray_files_fail(
    exchange: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = tmp_path / "extra.md"
    body.write_bytes(b"Synthetic extra body.\n")
    monkeypatch.setenv(ROOT_ENV, str(exchange))
    post = ["post", "--project", PROJECT, "--from", CODEX_LANE, "--to", "all"]
    post += ["--subject", "Synthetic extra", "--kind", "fyi", "--needs", "fyi"]
    assert call_cli([*post, "--body-file", str(body)])[0] == 0
    card = ["card", "set", "--project", PROJECT, "--lane", "other-lane", "--host", "other"]
    assert call_cli([*card, "--item", "Synthetic", "--state", "idle"])[0] == 0
    (exchange / "messages" / PROJECT / "stray.md.0000abcd.tmp").write_bytes(b"partial")
    assert_fails(exchange, "unexpected note", "unexpected card of other-lane", "store diagnostic")


def test_through_out_of_range_is_a_usage_error(store_root: Path) -> None:
    with pytest.raises(ValueError):
        check_store(store_root, PROJECT, through=len(PHASES) + 1)


# --- The cleanup guard ----------------------------------------------------------------

PathCase = Callable[[Path, Path], Path]
REFUSED_PATHS: dict[str, PathCase] = {
    "the store root": lambda root, other: root,
    "messages": lambda root, other: root / "messages",
    "acks": lambda root, other: root / "acks",
    "cards": lambda root, other: root / "cards",
    "another project": lambda root, other: root / "messages" / "pn-uat",
    "another smoke run": lambda root, other: root / "messages" / "pn-smoke-20300101t120001z",
    "a longer name": lambda root, other: root / "messages" / f"{PROJECT}x",
    "another kind of folder": lambda root, other: root / "notes" / PROJECT,
    "a file inside": lambda root, other: root / "messages" / PROJECT / "x.md",
    "a traversal": lambda root, other: root / "messages" / PROJECT / ".." / "pn-uat",
    "a traversal back": lambda root, other: root / "messages" / PROJECT / ".." / PROJECT,
    "a relative path": lambda root, other: Path("messages") / PROJECT,
    "another root": lambda root, other: other / "messages" / PROJECT,
    "the root's parent": lambda root, other: root.parent,
}


def test_the_cleanup_guard_accepts_exactly_this_runs_three_folders(store_root: Path) -> None:
    folders = run_folders(store_root, PROJECT)
    assert [path.parent.name for path in folders] == ["messages", "acks", "cards"]
    for path in folders:
        assert guard_cleanup_path(path, root=store_root, project=PROJECT) == path


@pytest.mark.parametrize("case", list(REFUSED_PATHS))
def test_the_cleanup_guard_refuses_every_other_path(
    store_root: Path, tmp_path: Path, case: str
) -> None:
    path = REFUSED_PATHS[case](store_root, tmp_path / "other-root")
    with pytest.raises(CleanupRefused):
        guard_cleanup_path(path, root=store_root, project=PROJECT)
    with pytest.raises(CleanupRefused):
        remove_run_folder(path, root=store_root, project=PROJECT)


@pytest.mark.parametrize(
    "project", ["pn-uat", "pn-smoke-", "pn-smoke-20300101T120000Z", "pn-smoke-2030", "x"]
)
def test_the_cleanup_guard_refuses_a_project_that_is_not_a_smoke_run(
    store_root: Path, project: str
) -> None:
    for path in run_folders(store_root, project):
        with pytest.raises(CleanupRefused):
            guard_cleanup_path(path, root=store_root, project=project)


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_the_cleanup_guard_refuses_a_linked_folder(
    store_root: Path, tmp_path: Path, dir_link: MakeLink, kind: str
) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.md").write_bytes(b"keep")
    link = store_root / "messages" / PROJECT
    link.parent.mkdir(parents=True)
    dir_link(link, target, kind)
    with pytest.raises(CleanupRefused):
        remove_run_folder(link, root=store_root, project=PROJECT)
    assert (target / "keep.md").read_bytes() == b"keep"


def test_cleanup_removes_this_runs_folders_and_nothing_else(
    exchange: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = run_smoke.smoke_project(datetime(2030, 1, 1, 12, 0, 1, tzinfo=UTC))
    for kind in ("messages", "acks", "cards"):
        for project in ("pn-uat", other):
            folder = exchange / kind / project
            folder.mkdir()
            (folder / "keep.md").write_bytes(b"keep")
    (exchange / "stray-at-root.md").write_bytes(b"keep")
    before = {key: value for key, value in snapshot(exchange).items() if PROJECT not in key}
    for path in run_folders(exchange, PROJECT):
        assert remove_run_folder(path, root=exchange, project=PROJECT)
        assert not path.exists()
        assert not remove_run_folder(path, root=exchange, project=PROJECT)  # already gone
    assert snapshot(exchange) == before


def test_cleanup_refuses_a_folder_holding_a_subfolder_and_deletes_nothing(
    exchange: Path,
) -> None:
    folder = run_folders(exchange, PROJECT)[0]
    (folder / "nested").mkdir()
    before = snapshot(exchange)
    with pytest.raises(CleanupRefused):
        remove_run_folder(folder, root=exchange, project=PROJECT)
    assert snapshot(exchange) == before


# --- The driver with simulated hosts --------------------------------------------------


@dataclass(frozen=True)
class HostCall:
    argv: list[str]
    stdin: str
    cwd: Path
    env: dict[str, str]
    timeout: float


Behaviour = Callable[[], HostOutput]


class FakeHosts:
    """Simulated hosts. A version call answers; a phase call runs the prompt as a
    perfect host would, unless ``phases`` gives that phase another behaviour."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        phases: Mapping[str, Behaviour] | None = None,
        versions: Mapping[str, HostOutput] | None = None,
    ) -> None:
        self.monkeypatch = monkeypatch
        self.phases = dict(phases or {})
        self.versions = dict(versions or {})
        self.calls: list[HostCall] = []
        self.phase_calls: list[str] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
    ) -> HostOutput:
        self.calls.append(HostCall(list(argv), stdin, cwd, dict(env), timeout))
        name = Path(argv[0]).stem
        if list(argv[1:]) == ["--version"]:
            return self.versions.get(name, HostOutput(0, f"{name} 9.9.9-test\n", ""))
        phase = PHASES[len(self.phase_calls)]
        self.phase_calls.append(phase.name)
        assert name == phase.host
        if phase.name in self.phases:
            return self.phases[phase.name]()
        root = resolve_root(env)
        perform_phase(stdin, phase, cwd, root, self.monkeypatch)
        if phase.host == "claude":
            result = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 4,
                "result": TRANSCRIPT_MARKER,
                "modelUsage": {"claude-test-model": {"outputTokens": 1}},
                "permission_denials": [],
            }
            return HostOutput(0, json.dumps(result), TRANSCRIPT_MARKER, seconds=1.0)
        header = [
            "OpenAI Codex (test)",
            "--------",
            "model: gpt-test-model",
            f"sandbox: workspace-write [workdir, {root}]",
            "approval: never",
            "--------",
            TRANSCRIPT_MARKER,
        ]
        return HostOutput(0, TRANSCRIPT_MARKER, "\n".join(header), seconds=1.0)


@dataclass
class SmokeSetup:
    environ: dict[str, str]
    localappdata: Path
    bin_dir: Path
    venv_scripts: Path
    work: Path

    @property
    def root(self) -> Path:
        return self.localappdata / "passing-notes"

    @property
    def receipts(self) -> Path:
        return self.localappdata / "passing-notes-smoke" / "receipts"

    def which(self, name: str, path: str | None) -> str | None:
        assert path is not None and str(self.venv_scripts) not in path
        return str(self.bin_dir / f"{name}.exe")


@pytest.fixture
def setup(tmp_path: Path) -> SmokeSetup:
    """A launching environment as ``uv run`` inside a Claude window would give it."""
    localappdata = tmp_path / "localappdata"
    bin_dir = tmp_path / "bin"
    venv = tmp_path / "venv"
    work = tmp_path / "work"
    for folder in (localappdata, bin_dir, venv / "Scripts", work):
        folder.mkdir(parents=True)
    environ = {
        "LOCALAPPDATA": str(localappdata),
        "PATH": os.pathsep.join([str(venv / "Scripts"), str(bin_dir)]),
        "VIRTUAL_ENV": str(venv),
        ROOT_ENV: str(tmp_path / "override-store"),
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SESSION_ID": "synthetic-session",
        "CLAUDE_CODE_ENTRYPOINT": "synthetic",
        "CODEX_THREAD_ID": "synthetic-thread",
        "CODEX_HOME": str(tmp_path / "codex-home"),
        "SYNTHETIC_KEEP": "kept",
    }
    return SmokeSetup(environ, localappdata, bin_dir, venv / "Scripts", work)


def smoke(
    setup: SmokeSetup, hosts: FakeHosts, which: run_smoke.Which | None = None
) -> run_smoke.SmokeOutcome:
    return run_smoke.run_smoke(
        setup.environ,
        which=which or setup.which,
        runner=hosts,
        now=lambda: STARTED,
        work_parent=setup.work,
    )


def load_receipt(setup: SmokeSetup, outcome: run_smoke.SmokeOutcome) -> dict[str, object]:
    assert outcome.receipt_path == setup.receipts / f"{STAMP}.json"
    text = outcome.receipt_path.read_text(encoding="utf-8")
    assert TRANSCRIPT_MARKER not in text  # no host output is kept
    receipt: dict[str, object] = json.loads(text)
    assert receipt == outcome.receipt
    return receipt


def assert_run_folders_gone(setup: SmokeSetup) -> None:
    assert not any(path.exists() for path in run_folders(setup.root, PROJECT))


def test_a_full_simulated_run_passes_and_writes_its_receipt(
    setup: SmokeSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep = setup.root / "messages" / "pn-uat"
    keep.mkdir(parents=True)
    (keep / "keep.md").write_bytes(b"keep")
    hosts = FakeHosts(monkeypatch)
    outcome = smoke(setup, hosts)
    assert outcome.verdict == PASS, outcome.failures
    receipt = load_receipt(setup, outcome)
    assert receipt["verdict"] == PASS and receipt["failures"] == []
    assert receipt["project"] == PROJECT and receipt["phases_run"] == len(PHASES)
    versions = {name: f"{name} 9.9.9-test" for name in run_smoke.EXECUTABLES}
    assert receipt["cli_versions"] == versions
    assert receipt["claude"] == {
        "models": ["claude-test-model"],
        "turn_cap": 8,
        "setting_sources": "project",
        "permission_mode": "dontAsk",
        "tools": ["Bash"],
        "allowed_tools": ["Bash(passing-notes *)", "Bash(passing-notes:*)"],
    }
    assert receipt["codex"] == {
        "models": ["gpt-test-model"],
        "sandbox_mode": "workspace-write",
        "writable_roots_added": [str(setup.root)],
        "sandbox_reported": f"workspace-write [workdir, {setup.root}]",
    }
    note_ids = receipt["note_ids"]
    assert isinstance(note_ids, dict) and set(note_ids) == {role.name for role in ROLES}
    assert receipt["cards"] == {CLAUDE_LANE: "claude", CODEX_LANE: "codex"}
    ack_files = receipt["ack_files"]
    assert isinstance(ack_files, list) and len(ack_files) == len(ACK_PHASES)
    calls = receipt["calls"]
    assert isinstance(calls, list) and [c["phase"] for c in calls] == [p.name for p in PHASES]
    assert all(c["error"] is None and c["exit_code"] == 0 for c in calls)
    # The receipt is outside the store and every repository; the run's folders are gone,
    # and the store's other contents are untouched.
    assert outcome.receipt_path is not None
    assert not outcome.receipt_path.is_relative_to(setup.root)
    assert find_git_worktree(outcome.receipt_path) is None
    assert_run_folders_gone(setup)
    assert (keep / "keep.md").read_bytes() == b"keep"
    assert receipt["cleanup"] == {
        "removed": [str(path) for path in run_folders(setup.root, PROJECT)],
        "not_removed": [],
    }
    assert not any(setup.work.iterdir())  # the working folders are removed too


def test_hosts_are_called_as_the_smoke_contract_says(
    setup: SmokeSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosts = FakeHosts(monkeypatch)
    assert smoke(setup, hosts).verdict == PASS
    phase_calls = [call for call in hosts.calls if call.argv[1:] != ["--version"]]
    assert len(phase_calls) == len(PHASES)
    for call, phase in zip(phase_calls, PHASES, strict=True):
        assert call.timeout == 300
        assert call.stdin == run_smoke.build_prompt(phase, PROJECT)  # prompt on stdin only
        assert not any("smoke test" in arg for arg in call.argv)
        assert call.cwd.name == phase.host and find_git_worktree(call.cwd) is None
        # The store override, the dev venv and the launching window's session are gone.
        for name in (ROOT_ENV, "VIRTUAL_ENV", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
            assert name not in call.env
        assert "CLAUDE_CODE_ENTRYPOINT" not in call.env and "CODEX_THREAD_ID" not in call.env
        assert call.env["CODEX_HOME"] == setup.environ["CODEX_HOME"]
        assert call.env["SYNTHETIC_KEEP"] == "kept"
        assert call.env["PATH"] == str(setup.bin_dir)
        argv = call.argv
        if phase.host == "claude":
            assert argv[:2] == [str(setup.bin_dir / "claude.exe"), "-p"]
            pairs = {argv[i]: argv[i + 1] for i in range(1, len(argv) - 1)}
            assert pairs["--max-turns"] == "8"
            assert pairs["--permission-mode"] == "dontAsk"
            assert pairs["--setting-sources"] == "project"
            assert pairs["--tools"] == "Bash"
            assert "--no-session-persistence" in argv
        else:
            assert argv[:2] == [str(setup.bin_dir / "codex.exe"), "exec"]
            assert argv[argv.index("-s") + 1] == "workspace-write"
            assert argv[argv.index("--add-dir") + 1] == str(setup.root)
            assert argv[argv.index("-C") + 1] == str(call.cwd)
            assert "--skip-git-repo-check" in argv and "--ephemeral" in argv
            assert argv[-1] == "-"


@pytest.mark.parametrize("missing", run_smoke.EXECUTABLES)
def test_a_missing_host_fails_the_run_and_is_never_skipped(
    setup: SmokeSetup, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    def which(name: str, path: str | None) -> str | None:
        return None if name == missing else setup.which(name, path)

    hosts = FakeHosts(monkeypatch)
    outcome = smoke(setup, hosts, which=which)
    assert outcome.verdict == FAIL
    assert any("host unavailable" in f and missing in f for f in outcome.failures)
    assert load_receipt(setup, outcome)["verdict"] == FAIL
    assert hosts.calls == []  # no host was ever started
    assert_run_folders_gone(setup)


def test_no_host_on_path_fails_through_the_real_lookup(
    setup: SmokeSetup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)  # Windows lookups also search the current folder
    setup.environ["PATH"] = str(empty)
    hosts = FakeHosts(monkeypatch)
    outcome = run_smoke.run_smoke(
        setup.environ, runner=hosts, now=lambda: STARTED, work_parent=setup.work
    )
    assert outcome.verdict == FAIL
    assert "claude, codex" in outcome.failures[0] and "rather than skips" in outcome.failures[0]
    assert load_receipt(setup, outcome)["verdict"] == FAIL


def test_the_script_reports_failure_when_no_host_is_installed(tmp_path: Path) -> None:
    """The real entry point, as an operator runs it, with no host on PATH."""
    empty = tmp_path / "empty"
    localappdata = tmp_path / "localappdata"
    empty.mkdir()
    localappdata.mkdir()
    env = {
        "LOCALAPPDATA": str(localappdata),
        "PATH": str(empty),
        "PYTHONIOENCODING": "utf-8",
    }
    for name in ("SYSTEMROOT", "PATHEXT", "TEMP", "TMP"):
        if name in os.environ:
            env[name] = os.environ[name]
    script = REPO_ROOT / "tests" / "cross_host" / "run_smoke.py"
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        cwd=empty,
        env=env,
        timeout=120,
        check=False,
    )
    out = proc.stdout.decode("utf-8")
    assert proc.returncode == 1, (out, proc.stderr.decode("utf-8"))
    assert "cross-host smoke: FAIL" in out and "host unavailable" in out
    receipts = list((localappdata / "passing-notes-smoke" / "receipts").iterdir())
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text(encoding="utf-8"))["verdict"] == FAIL
    assert not (localappdata / "passing-notes" / "messages").exists()


def claude_error(message: str) -> Behaviour:
    def behave() -> HostOutput:
        result = {"type": "result", "subtype": "success", "is_error": True, "result": message}
        return HostOutput(1, json.dumps(result), "", seconds=1.0)

    return behave


def test_failed_sign_in_fails_the_run(setup: SmokeSetup, monkeypatch: pytest.MonkeyPatch) -> None:
    hosts = FakeHosts(monkeypatch, phases={"claude-opens": claude_error("Not logged in")})
    outcome = smoke(setup, hosts)
    assert outcome.verdict == FAIL
    assert "claude-opens" in outcome.failures[0] and "Not logged in" in outcome.failures[0]
    receipt = load_receipt(setup, outcome)
    calls = receipt["calls"]
    assert isinstance(calls, list) and len(calls) == 1  # the run stops at the failed phase
    assert calls[0]["exit_code"] == 1 and calls[0]["error"] == "Not logged in"
    assert hosts.phase_calls == ["claude-opens"]


def test_a_sandbox_denial_fails_the_run_and_cleans_up(
    setup: SmokeSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    denied = "Access is denied. (synthetic sandbox refusal)"
    hosts = FakeHosts(
        monkeypatch,
        phases={"codex-answers": lambda: HostOutput(0, TRANSCRIPT_MARKER, denied)},
    )
    outcome = smoke(setup, hosts)
    assert outcome.verdict == FAIL
    assert "codex-answers" in outcome.failures[0]
    assert f"ack of {CLAUDE_QUESTION.name} by {CODEX_LANE}" in outcome.failures[0]
    receipt = load_receipt(setup, outcome)
    assert receipt["phases_run"] == 2
    # Claude's phase wrote messages and cards folders; cleanup removed them.
    cleanup = receipt["cleanup"]
    assert isinstance(cleanup, dict) and len(cleanup["removed"]) == 2
    assert_run_folders_gone(setup)


def test_a_timed_out_host_fails_the_run(setup: SmokeSetup, monkeypatch: pytest.MonkeyPatch) -> None:
    hung = HostOutput(None, TRANSCRIPT_MARKER, "", timed_out=True, seconds=300.0)
    hosts = FakeHosts(monkeypatch, phases={"claude-opens": lambda: hung})
    outcome = smoke(setup, hosts)
    assert outcome.verdict == FAIL and "timed out" in outcome.failures[0]
    receipt = load_receipt(setup, outcome)
    calls = receipt["calls"]
    assert isinstance(calls, list) and calls[0]["timed_out"] is True


def test_a_driver_defect_still_cleans_up_and_writes_a_failed_receipt(
    setup: SmokeSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    def defect() -> HostOutput:
        raise RuntimeError("synthetic driver defect")

    hosts = FakeHosts(monkeypatch, phases={"claude-answers": defect})
    with pytest.raises(RuntimeError):
        smoke(setup, hosts)
    receipts = list(setup.receipts.iterdir())
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["verdict"] == FAIL
    assert receipt["failures"] == ["driver error: RuntimeError: synthetic driver defect"]
    assert_run_folders_gone(setup)


def test_a_failed_version_check_fails_the_run(
    setup: SmokeSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = HostOutput(1, "", "synthetic: not signed in\n")
    hosts = FakeHosts(monkeypatch, versions={"codex": broken})
    outcome = smoke(setup, hosts)
    assert outcome.verdict == FAIL
    assert "host unavailable: codex --version failed" in outcome.failures[0]
    assert hosts.phase_calls == []


def test_the_development_venv_cli_is_refused(
    setup: SmokeSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    def which(name: str, path: str | None) -> str | None:
        if name == run_smoke.CLI_NAME:
            return str(REPO_ROOT / ".venv" / "Scripts" / "passing-notes.exe")
        return setup.which(name, path)

    hosts = FakeHosts(monkeypatch)
    outcome = smoke(setup, hosts, which=which)
    assert outcome.verdict == FAIL and "uv tool install --editable" in outcome.failures[0]
    assert hosts.calls == []


def test_a_project_that_already_exists_is_left_alone(
    setup: SmokeSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = setup.root / "cards" / PROJECT
    folder.mkdir(parents=True)
    (folder / "keep.md").write_bytes(b"keep")
    hosts = FakeHosts(monkeypatch)
    outcome = smoke(setup, hosts)
    assert outcome.verdict == FAIL and "already exist" in outcome.failures[0]
    assert (folder / "keep.md").read_bytes() == b"keep"
    assert hosts.calls == []
    assert load_receipt(setup, outcome)["cleanup"] == {}


# --- Driver helpers -------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="Windows names variables and paths in any case")
def test_host_environment_matches_names_in_any_case() -> None:
    venv = os.path.join(os.sep, "synthetic", "venv")
    scripts = os.path.join(venv, "Scripts")
    env = run_smoke.host_environment(
        {
            "Path": os.pathsep.join([scripts.upper(), "keep-me"]),
            "virtual_env": venv,
            "passing_notes_root": "x",
            "ClaudeCode": "1",
            "claude_code_session_id": "x",
            "Codex_Home": "kept",
        }
    )
    assert env == {"Path": "keep-me", "Codex_Home": "kept"}


def test_batch_shims_refuse_arguments_cmd_would_reinterpret() -> None:
    run_smoke.check_batch_safe(["C:/bin/codex.CMD", "exec", "-s", "workspace-write", "-"])
    run_smoke.check_batch_safe(["C:/bin/claude.exe", 'any "quoted" text'])
    for bad in ['a"b', "a&b", "a|b", "a%b", "a^b", "a\nb"]:
        with pytest.raises(SmokeFailure):
            run_smoke.check_batch_safe(["C:/bin/codex.cmd", "exec", bad])


def test_claude_metadata_keeps_models_and_counts_but_never_the_reply() -> None:
    result = {
        "subtype": "error_max_turns",
        "is_error": False,
        "num_turns": 8,
        "result": TRANSCRIPT_MARKER,
        "modelUsage": {"model-b": {}, "model-a": {}},
        "permission_denials": [{"tool_name": "Write", "tool_input": {"x": 1}}],
    }
    models, details, error = run_smoke.claude_metadata(HostOutput(0, json.dumps(result), ""))
    assert models == ["model-a", "model-b"] and error is None
    assert details == {
        "turns": 8,
        "stop": "error_max_turns",
        "permission_denials": 1,
        "denied_tools": ["Write"],
    }
    assert TRANSCRIPT_MARKER not in json.dumps(details)
    _, _, error = run_smoke.claude_metadata(HostOutput(2, "not json", "line 1\nlast line\n"))
    assert error == "last line"


def test_codex_metadata_reads_only_the_header_and_a_bounded_error() -> None:
    stderr = "model: m1\nsandbox: workspace-write\napproval: never\n" + "x" * 500
    models, details, error = run_smoke.codex_metadata(HostOutput(3, TRANSCRIPT_MARKER, stderr))
    assert models == ["m1"]
    assert details == {"sandbox_reported": "workspace-write", "approval_reported": "never"}
    assert error is not None and len(error) <= run_smoke.ERROR_LINE_MAX_CHARS + 3
    _, _, error = run_smoke.codex_metadata(HostOutput(0, "", "model: m1\n"))
    assert error is None


def test_run_host_passes_stdin_and_kills_a_host_that_times_out(tmp_path: Path) -> None:
    echo = [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"]
    output = run_smoke.run_host(echo, stdin="synthetic", cwd=tmp_path, env=os.environ, timeout=60)
    assert output.returncode == 0 and output.stdout.strip() == "SYNTHETIC"
    sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
    output = run_smoke.run_host(sleeper, stdin="", cwd=tmp_path, env=os.environ, timeout=1)
    assert output.timed_out and output.returncode is None and output.seconds < 60
    missing = [str(tmp_path / "no-such-host.exe")]
    output = run_smoke.run_host(missing, stdin="", cwd=tmp_path, env=os.environ, timeout=5)
    assert output.start_error is not None and output.returncode is None


def test_the_project_name_follows_the_stamp_rule() -> None:
    assert PROJECT == "pn-smoke-20300101t120000z"
    assert run_smoke.SMOKE_PROJECT_RE.fullmatch(PROJECT)
    assert protocol.check_project_id(PROJECT) == PROJECT
