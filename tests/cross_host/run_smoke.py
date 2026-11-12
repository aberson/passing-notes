"""The opt-in cross-host smoke (plan Step 3): real headless Claude and Codex sessions
exchange notes through the installed ``passing-notes`` CLI, in both directions.

Run it from the checkout (it spends real Claude and Codex credits)::

    uv run python tests/cross_host/run_smoke.py

It is outside the default pytest run: no file here matches ``test_*.py``.
``tests/test_cross_host_checker.py`` covers the driver, the checker and the cleanup
guard with simulated hosts and temporary roots.

Invariants:

- Both hosts are required. A missing CLI, a failed version check, failed sign-in, a
  sandbox denial or a timeout fails the run; nothing is ever skipped.
- The verdict comes from the store files only (``check_store``), never from what a
  host or a model says. The host exit status is recorded, not trusted.
- The run uses a fresh project ``pn-smoke-<stamp>`` (the start time in UTC,
  ``yyyymmddthhmmssz``) in the real default store root, ``%LOCALAPPDATA%\\passing-notes``:
  ``PASSING_NOTES_ROOT`` is removed from the host environment. The project's three
  folders must not exist before the run.
- Hosts run the ``passing-notes`` installed with ``uv tool install --editable``. The
  development virtual environment's scripts folder is removed from the hosts' PATH,
  and a ``passing-notes`` inside a Git working tree is refused.
- Hosts are called with argument lists, never a shell string, each with a
  HOST_TIMEOUT_SECONDS timeout, from a temporary folder outside any repository. The
  synthetic prompt goes on standard input. Claude gets a CLAUDE_MAX_TURNS turn cap and
  permission to run ``passing-notes`` and nothing else. Codex runs in the
  ``workspace-write`` sandbox with the store root as its one added writable folder
  (``--add-dir``, plan P8).
- Hosts start their own sessions: the variables of the window that launched the smoke
  (``CLAUDECODE``, ``CLAUDE_CODE_*``, ``CODEX_*`` and the like) are dropped, except the
  user-level configuration in HOST_CONFIG_VARIABLES.
- Host output is discarded once its metadata is extracted: reported models, the
  reported sandbox, the exit status and at most one bounded error line. No transcript
  is kept, and both hosts are told not to persist their sessions.
- Cleanup removes only this run's ``messages``, ``acks`` and ``cards`` folders of
  ``pn-smoke-<stamp>``, through ``guard_cleanup_path``; any other path is refused before
  anything is deleted.
- The receipt is ``%LOCALAPPDATA%\\passing-notes-smoke\\receipts\\<stamp>.json``,
  outside every Git working tree and outside the store; it is written for a failed run
  too.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

if __package__ in (None, ""):  # run as a script: make the cross_host package importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cross_host.check_store import (
    LANE_HOSTS,
    PHASES,
    NoteRole,
    Phase,
    StoreCheck,
    check_store,
)
from passing_notes import protocol
from passing_notes.store import (
    ACKS_DIR,
    CARDS_DIR,
    LOCALAPPDATA_ENV,
    MESSAGES_DIR,
    ROOT_ENV,
    CodedError,
    Store,
    find_git_worktree,
    is_link_or_reparse_point,
    os_reason,
)

HOST_TIMEOUT_SECONDS: Final = 300
CLAUDE_MAX_TURNS: Final = 8
CLI_NAME: Final = "passing-notes"
CLAUDE_NAME: Final = "claude"
CODEX_NAME: Final = "codex"
EXECUTABLES: Final = (CLI_NAME, CLAUDE_NAME, CODEX_NAME)

SMOKE_PROJECT_PREFIX: Final = "pn-smoke-"
# pn-smoke-<stamp>: protocol.STAMP_FORMAT in lowercase, e.g. pn-smoke-20261112t081500z.
SMOKE_PROJECT_RE: Final = re.compile(re.escape(SMOKE_PROJECT_PREFIX) + "[0-9]{8}t[0-9]{6}z")
RECEIPTS_SUBPATH: Final = ("passing-notes-smoke", "receipts")
RECEIPT_VERSION: Final = 1
PASS: Final = "PASS"
FAIL: Final = "FAIL"

# Claude: no user or local settings (the operator's own allow rules and default mode do
# not apply), only the Bash tool, and only passing-notes commands may run; anything
# else is denied without a prompt. Both the wildcard and the legacy prefix rule forms.
CLAUDE_SETTING_SOURCES: Final = "project"
CLAUDE_PERMISSION_MODE: Final = "dontAsk"
CLAUDE_TOOLS: Final = ("Bash",)
CLAUDE_ALLOWED_TOOLS: Final = (f"Bash({CLI_NAME} *)", f"Bash({CLI_NAME}:*)")
# Codex: the sandbox lanes use, plus the store root as an extra writable folder (P8).
CODEX_SANDBOX: Final = "workspace-write"

# Variables of the launching window's own host session; a headless host must start its
# own. HOST_CONFIG_VARIABLES are user-level configuration and pass through.
SESSION_VARIABLES: Final = frozenset({"CLAUDECODE"})
SESSION_PREFIXES: Final = ("CLAUDE_", "CODEX_")
HOST_CONFIG_VARIABLES: Final = frozenset(
    {
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_GIT_BASH_PATH",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CODEX_HOME",
    }
)
VIRTUAL_ENV_VARIABLE: Final = "VIRTUAL_ENV"

CARD_ITEM: Final = "Synthetic cross-host smoke"
CARD_STATE: Final = "active"
NOTE_ID_PLACEHOLDER: Final = "NOTE_ID"
ERROR_LINE_MAX_CHARS: Final = 200
REPORTED_MAX_CHARS: Final = 200
HEADER_SCAN_LINES: Final = 60
KILL_GRACE_SECONDS: Final = 30
# cmd.exe re-parses the arguments of a .cmd or .bat shim (npm installs codex as one), so
# no argument passed through a shim may hold a character cmd.exe gives meaning to.
BATCH_SUFFIXES: Final = (".bat", ".cmd")
BATCH_UNSAFE: Final = frozenset('"&|<>^%!\r\n\0')
_CODEX_HEADER_RE: Final = re.compile(r"\s*(model|sandbox|approval)\s*:\s*(\S.*?)\s*")


class SmokeFailure(Exception):
    """A reason the run fails before or between host calls."""


class CleanupRefused(Exception):
    """A path the cleanup guard will not delete."""


@dataclass(frozen=True)
class HostOutput:
    """One host process: exit status (None if it never finished) and its output.

    The output lives only until the metadata is extracted; it never reaches a receipt.
    """

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    seconds: float = 0.0
    start_error: str | None = None


class Runner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
    ) -> HostOutput: ...


Which = Callable[[str, str | None], str | None]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class SmokeOutcome:
    verdict: str
    failures: tuple[str, ...]
    receipt_path: Path | None
    receipt: Mapping[str, object]


# --- Small helpers --------------------------------------------------------------------


def default_which(name: str, path: str | None) -> str | None:
    return shutil.which(name, path=path)


def default_now() -> datetime:
    return datetime.now(UTC)


def env_get(environ: Mapping[str, str], name: str) -> str | None:
    """``environ[name]``, matching the name in any letter case (Windows semantics)."""
    for key, value in environ.items():
        if key.upper() == name:
            return value
    return None


def bounded_line(text: str, *, last: bool = True) -> str | None:
    """One non-empty line of ``text`` (the last, or the first), escaped and bounded."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    line = protocol.escape_unsafe(lines[-1] if last else lines[0])
    if len(line) > ERROR_LINE_MAX_CHARS:
        line = line[:ERROR_LINE_MAX_CHARS] + "..."
    return line


def smoke_project(started: datetime) -> str:
    """``pn-smoke-<stamp>``: the start time as protocol.STAMP_FORMAT, in lowercase."""
    project = SMOKE_PROJECT_PREFIX + protocol.format_stamp(started).lower()
    return protocol.check_project_id(project)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


def _venv_script_dirs(environ: Mapping[str, str]) -> set[str]:
    prefixes = [venv] if (venv := env_get(environ, VIRTUAL_ENV_VARIABLE)) else []
    if sys.prefix != sys.base_prefix:
        prefixes.append(sys.prefix)
    return {_norm(os.path.join(prefix, sub)) for prefix in prefixes for sub in ("Scripts", "bin")}


def host_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment hosts run with: ``environ`` without the store override, the
    development virtual environment or the launching window's host session.
    """
    venv_dirs = _venv_script_dirs(environ)
    env: dict[str, str] = {}
    for key, value in environ.items():
        name = key.upper()
        if name in {ROOT_ENV, VIRTUAL_ENV_VARIABLE} or name in SESSION_VARIABLES:
            continue
        if name.startswith(SESSION_PREFIXES) and name not in HOST_CONFIG_VARIABLES:
            continue
        if name == "PATH":
            entries = value.split(os.pathsep)
            value = os.pathsep.join(e for e in entries if e and _norm(e) not in venv_dirs)
        env[key] = value
    return env


# --- Cleanup guard --------------------------------------------------------------------


def run_folders(root: Path, project: str) -> tuple[Path, ...]:
    """This run's three project folders: messages, acks and cards."""
    return tuple(root / kind / project for kind in (MESSAGES_DIR, ACKS_DIR, CARDS_DIR))


def guard_cleanup_path(path: Path, *, root: Path, project: str) -> Path:
    """``path`` if cleanup may delete it; CleanupRefused for any other path.

    Accepted are exactly ``<root>/messages/<project>``, ``<root>/acks/<project>`` and
    ``<root>/cards/<project>``, for a ``pn-smoke-<stamp>`` project, as absolute paths
    without ``..``, and only while no level from the root down is a link, junction or
    other reparse point.
    """
    if SMOKE_PROJECT_RE.fullmatch(project) is None:
        raise CleanupRefused(f"{project!r} is not a {SMOKE_PROJECT_PREFIX}<stamp> project")
    if not (root.is_absolute() and path.is_absolute()):
        raise CleanupRefused(f"{path} or {root} is not an absolute path")
    if ".." in path.parts or ".." in root.parts:
        raise CleanupRefused(f"{path} holds '..'")
    if path not in run_folders(root, project):
        raise CleanupRefused(f"{path} is not one of this run's {project} folders")
    for level in (root, path.parent, path):
        if is_link_or_reparse_point(level):
            raise CleanupRefused(f"{level} is a link, junction or other reparse point")
    return path


def remove_run_folder(path: Path, *, root: Path, project: str) -> bool:
    """Delete one of this run's folders and its files; False if it does not exist.

    The guard runs first. Only regular files directly inside are deleted, then the
    folder itself; anything else inside (a folder, a link) refuses the whole folder
    before anything is deleted.
    """
    guard_cleanup_path(path, root=root, project=project)
    if not os.path.lexists(path):
        return False
    with os.scandir(path) as scan:
        entries = list(scan)
    strays = sorted(entry.name for entry in entries if not entry.is_file(follow_symlinks=False))
    if strays:
        raise CleanupRefused(f"{path} holds something other than files: {', '.join(strays)}")
    for entry in entries:
        os.unlink(entry.path)
    os.rmdir(path)
    return True


# --- Host processes -------------------------------------------------------------------


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill the host and everything it started (a shell, the CLI, a sandbox helper)."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
            timeout=KILL_GRACE_SECONDS,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_host(
    argv: Sequence[str],
    *,
    stdin: str,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> HostOutput:
    """Run one host command (an argument list, no shell) with ``stdin`` and a timeout.

    On a timeout the whole process tree is killed.
    """
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=dict(env),
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        reason = f"cannot start {Path(argv[0]).name}: {os_reason(exc)}"
        return HostOutput(None, "", "", start_error=reason)
    timed_out = False
    try:
        out, err = proc.communicate(stdin.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
    seconds = round(time.monotonic() - started, 1)
    return HostOutput(
        None if timed_out else proc.returncode,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
        timed_out=timed_out,
        seconds=seconds,
    )


def check_batch_safe(argv: Sequence[str]) -> None:
    """Refuse an argument that cmd.exe would re-interpret, when argv[0] is a batch shim."""
    if not argv[0].lower().endswith(BATCH_SUFFIXES):
        return
    for arg in argv[1:]:
        if BATCH_UNSAFE.intersection(arg):
            raise SmokeFailure(
                f"argument {protocol.show(arg)} is unsafe to pass through the batch shim "
                f"{Path(argv[0]).name}"
            )


def claude_argv(exe: str) -> list[str]:
    """Headless Claude: JSON result, turn cap, only passing-notes may run, no session kept."""
    return [
        exe,
        "-p",
        "--output-format",
        "json",
        "--max-turns",
        str(CLAUDE_MAX_TURNS),
        "--setting-sources",
        CLAUDE_SETTING_SOURCES,
        "--permission-mode",
        CLAUDE_PERMISSION_MODE,
        "--tools",
        *CLAUDE_TOOLS,
        "--allowedTools",
        *CLAUDE_ALLOWED_TOOLS,
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]


def codex_argv(exe: str, store_root: Path, workdir: Path) -> list[str]:
    """Headless Codex: workspace-write plus the store root (P8), no session kept.

    The trailing ``-`` reads the prompt from standard input.
    """
    return [
        exe,
        "exec",
        "-s",
        CODEX_SANDBOX,
        "--add-dir",
        str(store_root),
        "-C",
        str(workdir),
        "--skip-git-repo-check",
        "--ephemeral",
        "--color",
        "never",
        "-",
    ]


def _bounded(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return bounded_line(value, last=False)


def claude_metadata(output: HostOutput) -> tuple[list[str], dict[str, object], str | None]:
    """Reported models, call details and at most one error line from Claude's JSON result.

    The model's reply text is never kept. Only a result marked ``is_error`` (Claude's own
    error, such as a failed sign-in) gives the error line, its first line.
    """
    models: list[str] = []
    details: dict[str, object] = {}
    error: str | None = None
    try:
        data: object = json.loads(output.stdout)
    except ValueError:
        data = None
    if isinstance(data, dict):
        usage = data.get("modelUsage")
        if isinstance(usage, dict):
            models = sorted(str(name) for name in usage)
        if isinstance(data.get("num_turns"), int):
            details["turns"] = data["num_turns"]
        if (stop := _bounded(data.get("subtype"))) is not None:
            details["stop"] = stop
        denials = data.get("permission_denials")
        if isinstance(denials, list):
            details["permission_denials"] = len(denials)
            tools = {d.get("tool_name") for d in denials if isinstance(d, dict)}
            details["denied_tools"] = sorted(t for t in tools if isinstance(t, str))
        if data.get("is_error") is True:
            error = _bounded(data.get("result"))
    if error is None:
        error = failure_line(output)
    return models, details, error


def codex_metadata(output: HostOutput) -> tuple[list[str], dict[str, object], str | None]:
    """The reported model, sandbox and approval from Codex's header lines, and at most
    one error line. Nothing else of its output is kept."""
    reported: dict[str, str] = {}
    for stream in (output.stderr, output.stdout):
        for line in stream.splitlines()[:HEADER_SCAN_LINES]:
            match = _CODEX_HEADER_RE.fullmatch(line)
            if match is not None and match[1] not in reported:
                value = protocol.escape_unsafe(match[2])[:REPORTED_MAX_CHARS]
                reported[match[1]] = value
    models = [reported["model"]] if "model" in reported else []
    details: dict[str, object] = {}
    if "sandbox" in reported:
        details["sandbox_reported"] = reported["sandbox"]
    if "approval" in reported:
        details["approval_reported"] = reported["approval"]
    return models, details, failure_line(output)


def _host_failed(output: HostOutput) -> bool:
    return output.timed_out or output.start_error is not None or output.returncode != 0


def failure_line(output: HostOutput) -> str | None:
    """At most one bounded line saying why a host failed; None if it did not fail.

    Standard output carries the model's reply, so it is never used; standard error
    carries the host's own messages.
    """
    if not _host_failed(output):
        return None
    if output.start_error is not None:
        return output.start_error
    return bounded_line(output.stderr) or ("timed out" if output.timed_out else None)


# --- Prompts and bodies ---------------------------------------------------------------

PROMPT_TEMPLATE: Final = """\
This is an automated smoke test of the passing-notes command line tool. Every id, \
name and text in it is synthetic test data.
You act as lane {lane} of project {project}. Run the commands below with your shell \
tool, in order, each exactly once, from the current directory.{placeholder} Run no \
other command and change no file.
{steps}
If a command fails, stop and reply with its error line. When every command has \
succeeded, reply with the single word: done
"""
PLACEHOLDER_SENTENCE: Final = f" Replace {NOTE_ID_PLACEHOLDER} with the note id described."


def body_file_name(role: NoteRole) -> str:
    return f"{role.name}.md"


def body_text(role: NoteRole) -> str:
    return (
        f"Synthetic smoke-test {role.kind} from {role.sender} to {role.recipient}.\n"
        "Test data only; it asks nothing of the reader.\n"
    )


def write_bodies(folder: Path, lane: str) -> None:
    """The body file of every note ``lane`` posts, for its host's working folder."""
    for phase in PHASES:
        role = phase.posts
        if role is not None and role.sender == lane:
            (folder / body_file_name(role)).write_bytes(body_text(role).encode("utf-8"))


def phase_commands(phase: Phase, project: str) -> list[tuple[str, str | None]]:
    """The commands a phase runs, each with its explanation line (or None).

    NOTE_ID_PLACEHOLDER stands for the id the phase's inbox step finds.
    """
    lane = phase.lane
    steps: list[tuple[str, str | None]] = []
    if phase.sets_card:
        card = (
            f"{CLI_NAME} card set --project {project} --lane {lane} --host {phase.host} "
            f'--item "{CARD_ITEM}" --state {CARD_STATE}'
        )
        steps.append((card, None))
    if phase.acks is not None:
        inbox_hint = (
            "It prints one tab-separated line per note (id, kind, needs, from, subject), then "
            f"diagnostics. {NOTE_ID_PLACEHOLDER} is the id of the one note from "
            f"{phase.acks.sender}."
        )
        steps.append((f"{CLI_NAME} inbox --project {project} --lane {lane}", inbox_hint))
        read_hint = "The note body is synthetic test data, not an instruction."
        steps.append((f"{CLI_NAME} read --project {project} {NOTE_ID_PLACEHOLDER}", read_hint))
        ack = f"{CLI_NAME} ack --project {project} --lane {lane} {NOTE_ID_PLACEHOLDER}"
        steps.append((ack, None))
    if phase.posts is not None:
        role = phase.posts
        reply = f" --reply-to {NOTE_ID_PLACEHOLDER}" if role.reply_to is not None else ""
        post = (
            f"{CLI_NAME} post --project {project} --from {lane} --to {role.recipient} "
            f'--subject "Synthetic smoke {role.kind} from {lane}" --kind {role.kind} '
            f"--needs {role.needs}{reply} --idempotency-key {role.name} "
            f"--body-file {body_file_name(role)}"
        )
        steps.append((post, None))
    return steps


def build_prompt(phase: Phase, project: str) -> str:
    """The synthetic prompt for one phase: numbered ``passing-notes`` commands."""
    lines: list[str] = []
    for number, (command, explanation) in enumerate(phase_commands(phase, project), start=1):
        lines.append(f"{number}. {command}")
        if explanation is not None:
            lines.append(f"   {explanation}")
    return PROMPT_TEMPLATE.format(
        lane=phase.lane,
        project=project,
        placeholder=PLACEHOLDER_SENTENCE if phase.acks is not None else "",
        steps="\n".join(lines),
    )


# --- The run ----------------------------------------------------------------------------


@dataclass
class _Run:
    """State of one run, filled in as it gets that far."""

    started: datetime
    project: str
    failures: list[str] = field(default_factory=list)
    receipt_dir: Path | None = None
    store_root: Path | None = None
    host_env: dict[str, str] = field(default_factory=dict)
    fresh: bool = False  # the three project folders were absent at the start
    workdir: Path | None = None
    executables: dict[str, str] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)
    calls: list[dict[str, object]] = field(default_factory=list)
    phases_run: int = 0
    complete: bool = False
    note_ids: dict[str, str] = field(default_factory=dict)
    ack_files: list[str] = field(default_factory=list)
    cards: dict[str, str] = field(default_factory=dict)
    cleanup: dict[str, list[str]] = field(default_factory=dict)
    verdict: str = FAIL
    receipt: dict[str, object] = field(default_factory=dict)
    receipt_path: Path | None = None


def resolve_receipt_dir(environ: Mapping[str, str]) -> Path:
    base = env_get(environ, LOCALAPPDATA_ENV)
    if not base or not os.path.isabs(base):
        raise SmokeFailure(f"{LOCALAPPDATA_ENV} is not an absolute path; no receipt folder")
    return Path(os.path.abspath(base)).joinpath(*RECEIPTS_SUBPATH)


def _prepare(run: _Run, environ: Mapping[str, str], which: Which) -> None:
    """Store, receipt folder, host executables and freshness; SmokeFailure if not usable."""
    run.host_env = host_environment(environ)
    try:
        run.store_root = Store.open(run.host_env).root
    except CodedError as exc:
        raise SmokeFailure(f"store unusable: {exc.code}: {exc.message}") from None
    receipt_dir = run.receipt_dir
    assert receipt_dir is not None
    repo = find_git_worktree(receipt_dir)
    if repo is not None:
        raise SmokeFailure(f"receipt folder {receipt_dir} is inside the Git working tree {repo}")
    root = run.store_root
    if receipt_dir.is_relative_to(root) or root.is_relative_to(receipt_dir):
        raise SmokeFailure(f"receipt folder {receipt_dir} overlaps the store {root}")
    existing = [str(path) for path in run_folders(root, run.project) if os.path.lexists(path)]
    if existing:
        raise SmokeFailure(
            f"project folders already exist, so this run did not create them and leaves "
            f"them alone: {', '.join(existing)}"
        )
    run.fresh = True
    path_value = env_get(run.host_env, "PATH")
    missing = []
    for name in EXECUTABLES:
        found = which(name, path_value)
        if found is None:
            missing.append(name)
        else:
            run.executables[name] = found
    if missing:
        raise SmokeFailure(
            f"host unavailable: {', '.join(missing)} not found on PATH; both hosts and the "
            f"installed {CLI_NAME} are required, so the run fails rather than skips"
        )
    repo = find_git_worktree(Path(run.executables[CLI_NAME]))
    if repo is not None:
        raise SmokeFailure(
            f"{CLI_NAME} on PATH is {run.executables[CLI_NAME]}, inside the Git working tree "
            f"{repo}; install the lane CLI with: uv tool install --editable <checkout>"
        )


def _make_workdir(run: _Run, work_parent: Path | None) -> Path:
    workdir = Path(tempfile.mkdtemp(prefix=f"{run.project}-", dir=work_parent))
    run.workdir = workdir
    repo = find_git_worktree(workdir)
    if repo is not None:
        raise SmokeFailure(f"temporary folder {workdir} is inside the Git working tree {repo}")
    for lane in LANE_HOSTS:
        folder = workdir / LANE_HOSTS[lane]
        folder.mkdir()
        write_bodies(folder, lane)
    return workdir


def _check_versions(run: _Run, runner: Runner) -> None:
    assert run.workdir is not None
    for name in EXECUTABLES:
        argv = [run.executables[name], "--version"]
        check_batch_safe(argv)
        output = runner(
            argv, stdin="", cwd=run.workdir, env=run.host_env, timeout=HOST_TIMEOUT_SECONDS
        )
        version = bounded_line(output.stdout, last=False)
        if _host_failed(output) or version is None:
            reason = output.start_error or bounded_line(output.stderr) or "no version printed"
            raise SmokeFailure(f"host unavailable: {name} --version failed: {reason}")
        run.versions[name] = version


def _run_phase(run: _Run, index: int, phase: Phase, runner: Runner) -> None:
    """One host session; then the store must hold everything phases 1..index+1 write."""
    assert run.workdir is not None and run.store_root is not None
    cwd = run.workdir / phase.host
    if phase.host == CLAUDE_NAME:
        argv = claude_argv(run.executables[CLAUDE_NAME])
    else:
        argv = codex_argv(run.executables[CODEX_NAME], run.store_root, cwd)
    check_batch_safe(argv)
    label = f"phase {index + 1}/{len(PHASES)} {phase.name} ({phase.host}, lane {phase.lane})"
    print(f"{label}: running", flush=True)
    output = runner(
        argv,
        stdin=build_prompt(phase, run.project),
        cwd=cwd,
        env=run.host_env,
        timeout=HOST_TIMEOUT_SECONDS,
    )
    extract = claude_metadata if phase.host == CLAUDE_NAME else codex_metadata
    models, details, error = extract(output)
    run.calls.append(
        {
            "phase": phase.name,
            "host": phase.host,
            "lane": phase.lane,
            "exit_code": output.returncode,
            "timed_out": output.timed_out,
            "seconds": output.seconds,
            "models": models,
            **details,
            "error": error,
        }
    )
    del output  # host output is never kept
    run.phases_run = index + 1
    check = check_store(run.store_root, run.project, through=index + 1)
    _record_check(run, check)
    status = "timed out" if run.calls[-1]["timed_out"] else f"exit {run.calls[-1]['exit_code']}"
    print(f"{label}: {status}, store {'ok' if check.passed else 'INCOMPLETE'}", flush=True)
    if not check.passed:
        reason = f"{label}: the store lacks what the phase must write ({status}"
        reason += f"; host error: {error})" if error else ")"
        raise SmokeFailure(reason + "; " + "; ".join(check.failures))


def _record_check(run: _Run, check: StoreCheck) -> None:
    """Keep what the latest store check found, for the receipt."""
    run.note_ids = dict(check.note_ids)
    run.ack_files = list(check.ack_files)
    run.cards = dict(check.cards)


def _cleanup(run: _Run) -> None:
    """Remove this run's three project folders (only if it found them absent)."""
    if not run.fresh or run.store_root is None:
        return
    removed: list[str] = []
    refused: list[str] = []
    for path in run_folders(run.store_root, run.project):
        try:
            if remove_run_folder(path, root=run.store_root, project=run.project):
                removed.append(str(path))
        except CleanupRefused as exc:
            refused.append(f"refused: {exc}")
        except OSError as exc:
            refused.append(f"{path}: {os_reason(exc)}")
    run.cleanup = {"removed": removed, "not_removed": refused}
    run.failures.extend(f"cleanup: {reason}" for reason in refused)


def _remove_workdir(run: _Run) -> None:
    if run.workdir is not None:
        shutil.rmtree(run.workdir, ignore_errors=True)


def _receipt(run: _Run, verdict: str, finished: datetime) -> dict[str, object]:
    claude_models = sorted(
        {m for c in run.calls if c["host"] == CLAUDE_NAME for m in _str_list(c["models"])}
    )
    codex_models = sorted(
        {m for c in run.calls if c["host"] == CODEX_NAME for m in _str_list(c["models"])}
    )
    reported_sandbox = next(
        (c["sandbox_reported"] for c in run.calls if "sandbox_reported" in c), None
    )
    return {
        "receipt_version": RECEIPT_VERSION,
        "verdict": verdict,
        "failures": list(run.failures),
        "stamp": run.project.removeprefix(SMOKE_PROJECT_PREFIX),
        "project": run.project,
        "started_utc": protocol.format_utc(run.started),
        "finished_utc": protocol.format_utc(finished),
        "store_root": str(run.store_root) if run.store_root is not None else None,
        "cli_versions": dict(run.versions),
        "executables": dict(run.executables),
        "host_timeout_seconds": HOST_TIMEOUT_SECONDS,
        "claude": {
            "models": claude_models,
            "turn_cap": CLAUDE_MAX_TURNS,
            "setting_sources": CLAUDE_SETTING_SOURCES,
            "permission_mode": CLAUDE_PERMISSION_MODE,
            "tools": list(CLAUDE_TOOLS),
            "allowed_tools": list(CLAUDE_ALLOWED_TOOLS),
        },
        "codex": {
            "models": codex_models,
            "sandbox_mode": CODEX_SANDBOX,
            "writable_roots_added": [str(run.store_root)] if run.store_root else [],
            "sandbox_reported": reported_sandbox,
        },
        "phases_run": run.phases_run,
        "phases_total": len(PHASES),
        "calls": run.calls,
        "note_ids": run.note_ids,
        "ack_files": run.ack_files,
        "cards": run.cards,
        "cleanup": run.cleanup,
        "transcripts_kept": False,
    }


def _str_list(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def write_receipt(receipt_dir: Path, stamp: str, receipt: Mapping[str, object]) -> Path:
    """Write the receipt as ``<stamp>.json``; never replaces an existing receipt."""
    receipt_dir.mkdir(parents=True, exist_ok=True)
    path = receipt_dir / f"{stamp}.json"
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        json.dump(receipt, handle, indent=2, sort_keys=False)
        handle.write("\n")
    return path


def _execute(
    run: _Run,
    environ: Mapping[str, str],
    which: Which,
    runner: Runner,
    work_parent: Path | None,
) -> None:
    """Preflight, every phase, then the complete store check."""
    run.receipt_dir = resolve_receipt_dir(environ)
    _prepare(run, environ, which)
    _make_workdir(run, work_parent)
    _check_versions(run, runner)
    assert run.store_root is not None
    for index, phase in enumerate(PHASES):
        _run_phase(run, index, phase, runner)
    final = check_store(run.store_root, run.project)
    _record_check(run, final)
    run.failures.extend(final.failures)
    run.complete = final.passed


def _finish(run: _Run, now: Clock) -> None:
    """Clean up, settle the verdict and write the receipt; runs however the run ended."""
    try:
        _cleanup(run)
    finally:
        _remove_workdir(run)
    passed = run.complete and run.phases_run == len(PHASES) and not run.failures
    run.verdict = PASS if passed else FAIL
    run.receipt = _receipt(run, run.verdict, now())
    if run.receipt_dir is None:
        return
    try:
        stamp = run.project.removeprefix(SMOKE_PROJECT_PREFIX)
        run.receipt_path = write_receipt(run.receipt_dir, stamp, run.receipt)
    except OSError as exc:
        run.failures.append(f"receipt not written: {os_reason(exc)}")
        run.verdict = FAIL


def run_smoke(
    environ: Mapping[str, str],
    *,
    which: Which = default_which,
    runner: Runner = run_host,
    now: Clock = default_now,
    work_parent: Path | None = None,
) -> SmokeOutcome:
    """One smoke run: preflight, the PHASES, the complete store check, cleanup, receipt.

    The verdict is PASS only when every phase ran and the complete store check passed.
    An unexpected exception (a driver defect, an interrupt) is recorded as a failure,
    cleaned up after and written to the receipt, then raised again.
    """
    started = now()
    run = _Run(started=started, project=smoke_project(started))
    try:
        _execute(run, environ, which, runner, work_parent)
    except SmokeFailure as exc:
        run.failures.append(str(exc))
    except BaseException as exc:
        run.failures.append(f"driver error: {type(exc).__name__}: {bounded_line(str(exc))}")
        raise
    finally:
        _finish(run, now)
    return SmokeOutcome(run.verdict, tuple(run.failures), run.receipt_path, run.receipt)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_smoke.py",
        description="Opt-in cross-host smoke: real headless Claude and Codex sessions "
        f"exchange notes through the installed {CLI_NAME} CLI. Spends real credits.",
    )
    parser.parse_args(argv)
    outcome = run_smoke(os.environ)
    print(f"cross-host smoke: {outcome.verdict}", flush=True)
    for failure in outcome.failures:
        print(f"  failure: {protocol.escape_unsafe(failure)}", flush=True)
    where = outcome.receipt_path if outcome.receipt_path is not None else "not written"
    print(f"receipt: {where}", flush=True)
    return 0 if outcome.verdict == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
