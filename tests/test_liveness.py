"""Synthetic host evidence: quiet work is not a proven approval prompt."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from conftest import Invoke, MakeLink, child_env, run_cli

from passing_notes import liveness, protocol, status, wake
from passing_notes.store import Store

SESSION = "11111111-2222-4333-8444-555555555555"
NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)
CANARY = "synthetic-secret-must-not-leak"


class Host:
    def __init__(self, state: str = "waiting", waiting_for: str | None = None) -> None:
        self.calls = 0
        self.state = state
        self.waiting_for = waiting_for

    def list_sessions(self) -> list[wake.Session]:
        self.calls += 1
        return [
            wake.Session(
                SESSION, "synthetic", self.state, "interactive", 10, waiting_for=self.waiting_for
            )
        ]

    def send(self, address: str, text: str, permission_mode: str) -> wake.SendResult:
        raise AssertionError("status must never send")


def transcripts(root: Path, *, completed: bool = False) -> Path:
    project = root / "projects" / "synthetic-project"
    project.mkdir(parents=True)
    parent = project / f"{SESSION}.jsonl"
    parent.write_text(CANARY, encoding="utf-8")  # parent is statted, never parsed
    agent = project / SESSION / "subagents" / "agent-example.jsonl"
    agent.parent.mkdir(parents=True)
    event = {
        "type": "assistant",
        "message": {
            "stop_reason": "end_turn" if completed else "tool_use",
            "content": [{"type": "text" if completed else "tool_use", "text": CANARY}],
        },
    }
    agent.write_text(json.dumps(event) + "\n", encoding="utf-8")
    for path in (parent, agent):
        os.utime(path, (NOW.timestamp() - 3600, NOW.timestamp() - 3600))
    return agent


def binding(host: str = "claude") -> wake.Binding:
    return wake.Binding("alpha", "worker", host, SESSION, "default", "2030-01-01T10:00:00Z")


def observer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    child: bool = False,
    state: str = "waiting",
    reason: str | None = None,
) -> liveness.Observer:
    monkeypatch.setattr(liveness, "process_parents", lambda: {10: 1, **({11: 10} if child else {})})
    return liveness.Observer(Host(state, reason), tmp_path)


def test_stall_is_suspected_and_long_running_child_is_never_stalled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcripts(tmp_path)
    quiet = observer(tmp_path, monkeypatch).observe(binding(), NOW)
    assert (quiet.activity, quiet.open_subagents, quiet.transcript_age_minutes) == (
        "suspected-stall",
        1,
        60,
    )
    running = observer(tmp_path, monkeypatch, child=True).observe(binding(), NOW)
    assert (running.activity, running.child_processes) == ("running", 1)
    assert CANARY not in repr(quiet) + repr(running)


def test_permission_requires_explicit_host_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = observer(tmp_path, monkeypatch, reason="permission prompt").observe(binding(), NOW)
    assert evidence.activity == "pending-approval"
    assert evidence.window_state == "waiting"
    assert evidence.transcript_age_minutes is None
    assert (
        observer(tmp_path, monkeypatch, reason=CANARY).observe(binding(), NOW).activity == "unknown"
    )


def test_recent_agent_write_and_completed_agent_prevent_stall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = transcripts(tmp_path, completed=True)
    assert observer(tmp_path, monkeypatch).observe(binding(), NOW).activity == "unknown"
    os.utime(agent, (NOW.timestamp(), NOW.timestamp()))
    evidence = observer(tmp_path, monkeypatch).observe(binding(), NOW)
    assert (evidence.activity, evidence.open_subagents) == ("recent", 0)


def test_missing_process_evidence_does_not_mean_no_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcripts(tmp_path)
    probe = observer(tmp_path, monkeypatch)
    monkeypatch.setattr(liveness, "process_parents", lambda: None)
    evidence = probe.observe(binding(), NOW)
    assert evidence.child_processes is None and evidence.activity == "unknown"


def test_partial_tail_and_agent_limit_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = transcripts(tmp_path)
    agent.write_text('{"type":"assistant"', encoding="utf-8")
    os.utime(agent, (NOW.timestamp() - 3600, NOW.timestamp() - 3600))
    assert observer(tmp_path, monkeypatch).observe(binding(), NOW).open_subagents is None
    monkeypatch.setattr(liveness, "MAX_AGENTS", 0)
    evidence = observer(tmp_path, monkeypatch).observe(binding(), NOW)
    assert evidence.activity == "unknown" and evidence.transcript_age_minutes is None


def test_host_listing_is_cached_and_codex_is_not_probed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = Host("idle")
    monkeypatch.setattr(liveness, "process_parents", lambda: None)
    probe = liveness.Observer(host, tmp_path)
    assert probe.observe(binding("codex"), NOW).reason == "codex-resume-unverified"
    assert host.calls == 0
    assert probe.observe(binding(), NOW).window_state == "wakeable"
    probe.observe(binding(), NOW)
    assert host.calls == 1


def test_cli_displays_evidence_without_mutating_store(
    invoke: Invoke,
    store_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_status import tree

    transcripts(tmp_path / "host")
    Store(store_root).publish_card(
        protocol.make_card(
            project="alpha",
            lane="worker",
            host="claude",
            item="Synthetic work",
            state="waiting",
            updated=NOW,
            window=SESSION,
            waiting_on="operator",
        )
    )
    wake.save_binding(Store(store_root), binding())
    probe = observer(tmp_path / "host", monkeypatch)
    monkeypatch.setattr(status, "Observer", lambda: probe)
    from passing_notes import cli

    monkeypatch.setattr(cli, "_now_utc", lambda: NOW)
    before = tree(store_root)
    result = invoke("status", "--project", "alpha", "--json")
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)["projects"][0]["lanes"][0]
    assert row["window"] == "bound"  # original schema meaning is retained
    assert (row["waiting_on"], row["window_state"], row["activity"]) == (
        "operator",
        "waiting",
        "suspected-stall",
    )
    assert CANARY not in result.stdout and str(tmp_path) not in result.stdout
    assert tree(store_root) == before


@pytest.mark.parametrize(
    "reference",
    [
        "operator",
        "lane:reviewer",
        "resource:engine",
        "time:2030-01-02T12:00:00Z",
        "note:20300101T120000Z-lane-a-to-lane-b-1a2b",
    ],
)
def test_real_entry_point_waiting_on_roundtrip_and_clear(store_root: Path, reference: str) -> None:
    args = [
        "card",
        "set",
        "--project",
        "alpha",
        "--lane",
        "worker",
        "--host",
        "other",
        "--item",
        "Synthetic wait",
        "--state",
        "waiting",
    ]
    set_result = run_cli([*args, "--waiting-on", reference], root=store_root)
    assert set_result.returncode == 0, set_result.stderr
    shown = run_cli(["status", "--project", "alpha", "--json"], root=store_root)
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["projects"][0]["lanes"][0]["waiting_on"] == reference
    assert run_cli(args, root=store_root).returncode == 0
    assert Store(store_root).load_card("alpha", "worker").waiting_on is None


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "lane:../escape",
        "lane:all",
        "note:bad",
        "resource:../engine",
        "time:tomorrow",
        "approval",
    ],
)
def test_bad_wait_reference_never_writes(invoke: Invoke, store_root: Path, reference: str) -> None:
    result = invoke(
        "card",
        "set",
        "--project",
        "alpha",
        "--lane",
        "worker",
        "--host",
        "other",
        "--item",
        "Synthetic",
        "--state",
        "waiting",
        "--waiting-on",
        reference,
    )
    assert result.returncode == 2 and not store_root.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows CLI host adapter")
def test_real_cli_observes_permission_wait_from_synthetic_host(
    store_root: Path,
    tmp_path: Path,
) -> None:
    from test_status import tree

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rows = [
        {
            "sessionId": SESSION,
            "status": "waiting",
            "waitingFor": "permission prompt",
            "kind": "interactive",
            "name": "synthetic",
            "pid": None,
        }
    ]
    (bin_dir / "claude.cmd").write_text("@echo off\n@echo " + json.dumps(rows) + "\n")
    Store(store_root).publish_card(
        protocol.make_card(
            project="alpha",
            lane="worker",
            host="claude",
            item="Synthetic work",
            state="active",
            updated=NOW,
            window=SESSION,
        )
    )
    wake.save_binding(Store(store_root), binding())
    before = tree(store_root)
    result = run_cli(
        ["status", "--project", "alpha", "--json"],
        root=store_root,
        env=child_env(
            store_root,
            {
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "CLAUDE_CONFIG_DIR": str(tmp_path / "missing-host-data"),
            },
        ),
    )
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)["projects"][0]["lanes"][0]
    assert (row["window_state"], row["activity"], row["activity_reason"]) == (
        "waiting",
        "pending-approval",
        "host-permission-wait",
    )
    assert tree(store_root) == before


def test_failed_host_is_unknown_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = Host()
    calls = []

    def fail() -> list[wake.Session]:
        calls.append(1)
        raise wake.WakeError(wake.WAKE_HOST_UNAVAILABLE, CANARY)

    monkeypatch.setattr(host, "list_sessions", fail)
    probe = liveness.Observer(host, tmp_path)
    for _ in range(2):
        evidence = probe.observe(binding(), NOW)
        assert evidence.window_state == "unknown" and evidence.reason == "host-unavailable"
        assert CANARY not in repr(evidence)
    assert len(calls) == 1


def test_transcript_lookup_at_realistic_scale_and_direct_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcripts(tmp_path)
    projects = tmp_path / "projects"
    for number in range(600):
        (projects / f"old-worktree-{number}").mkdir()
    assert liveness.transcript_evidence(tmp_path, SESSION, NOW) == (60, 1)
    # This cwd's conventional directory is synthetic-project. No project listing
    # may run on the direct path, even if the fallback bound would be exceeded.
    monkeypatch.setattr(liveness, "MAX_PROJECTS", 1)
    assert liveness.transcript_evidence(tmp_path, SESSION, NOW, "synthetic/project") == (60, 1)
    probe = observer(tmp_path, monkeypatch)
    sessions = probe.host.list_sessions()
    monkeypatch.setattr(
        probe.host, "list_sessions", lambda: [replace(sessions[0], cwd="synthetic/project")]
    )
    assert probe.observe(binding(), NOW).activity == "suspected-stall"
    with pytest.raises(ValueError, match="entry limit"):
        liveness.transcript_evidence(tmp_path, SESSION, NOW, "missing/cwd")


def test_ambiguous_session_lookup_is_refused(tmp_path: Path) -> None:
    transcripts(tmp_path)
    other = tmp_path / "projects" / "second-project"
    other.mkdir()
    (other / f"{SESSION}.jsonl").write_text("synthetic", encoding="utf-8")
    with pytest.raises(ValueError, match="ambiguous"):
        liveness.transcript_evidence(tmp_path, SESSION, NOW)


@pytest.mark.parametrize("kind", ["junction", "symlink"])
@pytest.mark.parametrize("level", ["root", "projects", "project", "session", "subagents"])
def test_transcript_directory_links_are_refused(
    tmp_path: Path,
    dir_link: MakeLink,
    kind: str,
    level: str,
) -> None:
    target = tmp_path / "target"
    transcripts(target)
    relative = {
        "root": Path(),
        "projects": Path("projects"),
        "project": Path("projects/synthetic-project"),
        "session": Path("projects/synthetic-project") / SESSION,
        "subagents": Path("projects/synthetic-project") / SESSION / "subagents",
    }[level]
    view = tmp_path / "view"
    link = view / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    if level in {"session", "subagents"}:
        (view / "projects" / "synthetic-project" / f"{SESSION}.jsonl").write_text("synthetic")
    dir_link(link, target / relative, kind)
    with pytest.raises(ValueError, match="linked"):
        liveness.transcript_evidence(view, SESSION, NOW, "synthetic/project")


@pytest.mark.parametrize("which", ["parent", "agent"])
def test_transcript_file_reparse_points_are_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    which: str,
) -> None:
    agent = transcripts(tmp_path)
    selected = agent if which == "agent" else agent.parents[2] / f"{SESSION}.jsonl"
    original = Path.lstat

    def flagged(path: Path) -> os.stat_result:
        info = original(path)
        if path == selected:
            return cast(
                os.stat_result,
                SimpleNamespace(
                    st_mode=info.st_mode,
                    st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                ),
            )
        return info

    monkeypatch.setattr(Path, "lstat", flagged)
    with pytest.raises(ValueError, match="invalid"):
        liveness.transcript_evidence(tmp_path, SESSION, NOW, "synthetic/project")
    if which == "agent":
        assert liveness._unfinished(agent) is None


def test_tail_read_drops_partial_first_line_and_never_reads_old_tool_use(tmp_path: Path) -> None:
    agent = transcripts(tmp_path)
    tool = agent.read_bytes()
    # The old tool call is outside the read window, whose first line is partial.
    agent.write_bytes(tool + b"x" * (liveness.TAIL_BYTES + 100) + b'\n{"type":"progress"}\n')
    assert liveness._unfinished(agent) is None
    # A complete, recent tool call after that partial line still counts.
    agent.write_bytes(b"x" * (liveness.TAIL_BYTES + 100) + b"\n" + tool)
    assert liveness._unfinished(agent) is True


def test_bad_agent_tail_does_not_discard_other_activity(tmp_path: Path) -> None:
    agent = transcripts(tmp_path)
    broken = agent.with_name("agent-broken.jsonl")
    broken.write_bytes(b"not-json\n")
    os.utime(broken, (NOW.timestamp() - 3600, NOW.timestamp() - 3600))
    assert liveness.transcript_evidence(tmp_path, SESSION, NOW) == (60, 1)


@pytest.mark.parametrize("age,expected", [(29, "recent"), (30, "suspected-stall")])
def test_stall_threshold_for_unfinished_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    age: int,
    expected: str,
) -> None:
    agent = transcripts(tmp_path)
    os.utime(agent, (NOW.timestamp() - age * 60, NOW.timestamp() - age * 60))
    assert observer(tmp_path, monkeypatch).observe(binding(), NOW).activity == expected


def test_idle_host_and_shared_session_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcripts(tmp_path)
    probe = observer(tmp_path, monkeypatch, state="idle")
    first = probe.observe(binding(), NOW)
    assert first.activity == "idle"
    second = probe.observe(replace(binding(), project="beta", lane="reviewer"), NOW)
    assert second is first
    assert isinstance(probe.host, Host) and probe.host.calls == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows census adapter")
@pytest.mark.parametrize(
    "case",
    [
        "timeout",
        "denied",
        "exit",
        "oversize",
        "json",
        "object",
        "row",
        "bool",
        "string",
        "negative",
        "duplicate",
        "too-many",
    ],
)
def test_process_census_rejects_unusable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    monkeypatch.setenv("SystemRoot", str(tmp_path / "windows"))

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert Path(argv[0]).is_absolute() and Path(argv[0]).name == "powershell.exe"
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        assert kwargs["timeout"] == liveness.PROCESS_TIMEOUT
        if case == "timeout":
            raise subprocess.TimeoutExpired(argv, 8)
        if case == "denied":
            raise PermissionError("synthetic denial")
        rows: object = [{"ProcessId": 10, "ParentProcessId": 1}]
        if case == "object":
            rows = {}
        elif case == "row":
            rows = [None]
        elif case in {"bool", "string", "negative"}:
            rows = [
                {
                    "ProcessId": {"bool": True, "string": "10", "negative": -1}[case],
                    "ParentProcessId": 1,
                }
            ]
        elif case == "duplicate":
            rows = [*cast(list[object], rows), *cast(list[object], rows)]
        elif case == "too-many":
            rows = [
                {"ProcessId": i, "ParentProcessId": 0} for i in range(liveness.MAX_PROCESSES + 1)
            ]
        output = json.dumps(rows).encode()
        if case == "oversize":
            output = b" " * liveness.PROCESS_OUTPUT_BYTES + output
        elif case == "json":
            output = b"not-json"
        return subprocess.CompletedProcess(argv, 1 if case == "exit" else 0, output, b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert liveness.process_parents() is None


@pytest.mark.skipif(os.name != "nt", reason="Windows census adapter")
def test_process_census_accepts_ids_and_requires_system_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SystemRoot", str(tmp_path / "windows"))
    rows = [{"ProcessId": 10, "ParentProcessId": 1}, {"ProcessId": 11, "ParentProcessId": 10}]
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps(rows).encode(), b""
        ),
    )
    assert liveness.process_parents() == {10: 1, 11: 10}
    monkeypatch.delenv("SystemRoot")
    assert liveness.process_parents() is None
    monkeypatch.setenv("SystemRoot", "relative")
    assert liveness.process_parents() is None


@pytest.mark.parametrize(
    "parents,pid,expected",
    [
        ({10: 1}, None, None),
        ({10: 1}, 99, None),
        ({10: 11, 11: 10}, 10, None),
        ({10: 10}, 10, None),
        ({10: 1}, 10, 0),
        ({10: 1, 11: 10, 12: 11, 20: 1}, 10, 2),
    ],
)
def test_descendant_census_rejects_missing_identity_and_cycles(
    parents: dict[int, int],
    pid: int | None,
    expected: int | None,
) -> None:
    assert liveness.descendants(parents, pid) == expected
