"""Compact monitoring through the CLI, retaining the complete JSON lane rows."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import Invoke, child_env, fake_status_sessions, run_cli
from test_status import NOW, OTHER, SESSION, bind, put_card, put_note, status_run, tree

from passing_notes import cli, liveness, queue, status, status_resources, wake
from passing_notes.store import Store, StoreError

STAMP = "2030-01-02T10:00:00Z"
ITEM = "Step 17 review of the synthetic monitoring implementation: " + "checks " * 8 + "PASS r2"


def cards(root: Path) -> None:
    for project, state in (("alpha", "idle"), ("beta", "idle"), ("gamma", "active")):
        put_card(root, project, "main", state, ITEM, STAMP)
        bind(root, project, "main", SESSION)
    put_card(root, "alpha", "peer", "idle", "Another window", STAMP)
    bind(root, "alpha", "peer", OTHER)


def held(root: Path, project: str, resource: str = "heavy-run") -> None:
    store = Store(root)
    record = queue.grant(
        queue.empty_record(project, resource), "worker", "main", NOW, 20, "PRIVATE-SYNTHETIC-REASON"
    )
    with queue.locked(store, project, resource) as path:
        queue.write(store, path, record)


def test_real_cli_compacts_own_idle_rows_and_preserves_news_and_resources(
    store_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_status_sessions(
        tmp_path / "listed", monkeypatch, [{"sessionId": SESSION, "status": "idle"}]
    )
    cards(store_root)
    held(store_root, "alpha")
    held(store_root, "hidden-project")
    put_note(store_root, "alpha", "worker", "main", 1, needs="fyi")
    env = child_env(store_root, {cli.CLAUDE_SESSION_ENV: SESSION})
    before = tree(store_root)
    text = run_cli(["status", "--mine", "--live"], root=store_root, env=env)
    assert text.returncode == 0, text.stderr
    lines = text.stdout.splitlines()
    assert lines[0].startswith("resources: memory free ")
    assert "alpha/heavy-run=worker until 2030-01-02T12:20:00Z" in lines[0]
    assert "main in 2 projects (idle; 2 rows; open: 1; expand: --expand-idle)" in lines[1]
    assert "no lane cards" not in text.stdout
    assert "hidden-project" not in text.stdout and "PRIVATE-SYNTHETIC-REASON" not in text.stdout
    rows = [line.split() for line in lines if line.startswith(("alpha ", "beta ", "gamma "))]
    assert [(row[0], row[1]) for row in rows] == [("alpha", "peer"), ("gamma", "main")]
    assert "..." in text.stdout and "PASS r2" in text.stdout
    assert "lanes: 2  idle: 2 collapsed" in text.stdout

    result = run_cli(["status", "--mine", "--live", "--json"], root=store_root, env=env)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["schema"] == "passing-notes.status/1"
    assert data["collapsed_idle"] == ["alpha/main", "beta/main"]
    assert sum(len(block["lanes"]) for block in data["projects"]) == 4
    assert data["projects"][2]["lanes"][0]["item"] == ITEM
    assert data["resources"]["queue_holders"] == [
        {
            "project": "alpha",
            "resource": "heavy-run",
            "lane": "worker",
            "deadline_utc": "2030-01-02T12:20:00Z",
        }
    ]

    expanded = run_cli(["status", "--mine", "--live", "--expand-idle"], root=store_root, env=env)
    assert expanded.returncode == 0, expanded.stderr
    assert "this window:" not in expanded.stdout
    assert "beta" in expanded.stdout and "idle: 2 collapsed" not in expanded.stdout
    assert "lanes: 4" in expanded.stdout
    assert tree(store_root) == before


def test_expansion_changes_only_presentation_metadata(
    invoke: Invoke,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_status_sessions(
        store_root.parent / "listed", monkeypatch, [{"sessionId": SESSION, "status": "idle"}]
    )
    cards(store_root)
    monkeypatch.setenv(cli.CLAUDE_SESSION_ENV, SESSION)
    monkeypatch.setattr(cli, "_now_utc", lambda: NOW)
    monkeypatch.setattr(status_resources, "free_memory_bytes", lambda: 3 * 2**30)
    compact = json.loads(invoke("status", "--mine", "--json").stdout)
    full = json.loads(invoke("status", "--mine", "--json", "--expand-idle").stdout)
    assert compact.pop("collapsed_idle") == ["alpha/main", "beta/main"]
    assert full.pop("collapsed_idle") == []
    assert compact == full
    code, text, _ = status_run(invoke, store_root, "--mine")
    assert code == 0 and text.startswith("resources: memory free 3.0 GiB | queue holders: none\n")
    unbound = run_cli(
        ["status", "--all-projects", "--json"],
        root=store_root,
        env=child_env(store_root, {cli.CLAUDE_SESSION_ENV: ""}),
    )
    assert unbound.returncode == 0, unbound.stderr
    assert json.loads(unbound.stdout)["collapsed_idle"] == []


@pytest.mark.parametrize("alert", ["reply", "waiting", "pending-approval", "suspected-stall"])
def test_collapsing_own_idle_rows_preserves_attention_and_keeps_host_alerts_visible(
    invoke: Invoke,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    alert: str,
) -> None:
    monkeypatch.setattr(
        liveness.Observer,
        "observe",
        lambda self, binding, now: liveness.Evidence("wakeable", "idle"),
    )
    put_card(store_root, "alpha", "main", "idle", "Synthetic idle card", STAMP)
    bind(store_root, "alpha", "main", SESSION)
    monkeypatch.setenv(cli.CLAUDE_SESSION_ENV, SESSION)
    if alert == "reply":
        put_note(store_root, "alpha", "worker", "main", 1, needs="reply")
    elif alert == "waiting":
        store = Store(store_root)
        card = store.scan_cards("alpha")[0][0]
        store.publish_card(replace(card, waiting_on="operator"))
    else:
        monkeypatch.setattr(
            liveness.Observer,
            "observe",
            lambda self, binding, now: liveness.Evidence("wakeable", alert),
        )
    result = invoke("status", "--mine", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["collapsed_idle"] == (["alpha/main"] if alert in {"reply", "waiting"} else [])
    assert data["attention"]["lane"] == "main"
    assert data["projects"][0]["lanes"][0]["item"] == "Synthetic idle card"
    text = invoke("status", "--mine").stdout
    assert "look here first: alpha/main" in text
    if alert == "reply":
        assert "open: 1/1r; expand: --expand-idle" in text


@pytest.mark.parametrize(
    "window,activity,children",
    [
        ("busy", "idle", 0),
        ("waiting", "idle", 0),
        ("wakeable", "running", 1),
        ("wakeable", "recent", 0),
        ("wakeable", "idle", 1),
        ("unknown", "idle", None),
        ("wakeable", "unknown", None),
    ],
)
def test_own_idle_row_collapses_despite_busy_or_unknown_host_evidence(
    invoke: Invoke,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    window: str,
    activity: str,
    children: int | None,
) -> None:
    put_card(store_root, "alpha", "main", "idle", "Synthetic work", STAMP)
    bind(store_root, "alpha", "main", SESSION)
    monkeypatch.setenv(cli.CLAUDE_SESSION_ENV, SESSION)
    monkeypatch.setattr(
        liveness.Observer,
        "observe",
        lambda self, binding, now: liveness.Evidence(window, activity, child_processes=children),
    )
    result = invoke("status", "--mine", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["collapsed_idle"] == (
        [] if window == "waiting" else ["alpha/main"]
    )
    text = invoke("status", "--mine").stdout
    if window == "waiting":
        assert "this window:" not in text and "Synthetic work" in text
    else:
        assert "this window:" in text and "Synthetic work" not in text


@pytest.mark.parametrize(
    "case",
    [
        "probe",
        "other-child",
        "wrapper-child",
        "permission",
        "waiting",
        "open-agent",
        "unknown-agent",
        "missing-census",
        "not-descendant",
        "not-caller",
        "suspected-stall",
    ],
)
def test_real_observer_caller_idle_collapse_tolerates_a_watcher_and_unknown_evidence(
    invoke: Invoke,
    store_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    # The production Observer derives busy/running from a host listing, a fresh
    # transcript and a census containing this CLI process, wrappers and a probe child.
    host_pid, shell_pid, launcher_pid, helper_pid, other_pid = range(1_000_000, 1_000_005)
    probe_pid = os.getpid()
    parents = {
        host_pid: 1,
        shell_pid: host_pid,
        launcher_pid: shell_pid,
        probe_pid: launcher_pid,
        helper_pid: probe_pid,
    }
    if case == "other-child":
        parents[other_pid] = host_pid
    elif case == "wrapper-child":
        parents[other_pid] = shell_pid
    elif case == "not-descendant":
        parents[probe_pid] = other_pid
    elif case == "suspected-stall":
        parents = {host_pid: 1}
    host_root = tmp_path / "probe-host"
    fake_status_sessions(
        host_root,
        monkeypatch,
        [
            {
                "sessionId": SESSION,
                "pid": host_pid,
                "status": "waiting" if case in {"permission", "waiting"} else "busy",
                "waitingFor": "permission prompt" if case == "permission" else None,
            }
        ],
    )
    project = host_root / "data" / "projects" / "synthetic-project"
    project.mkdir(parents=True)
    transcript = project / f"{SESSION}.jsonl"
    transcript.write_text("Synthetic parent; content is never read.", encoding="utf-8")
    os.utime(transcript, (NOW.timestamp(), NOW.timestamp()))
    if case in {"open-agent", "unknown-agent", "suspected-stall"}:
        agent = project / SESSION / "subagents" / "agent-example.jsonl"
        agent.parent.mkdir(parents=True)
        event = {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}
        agent.write_text(
            json.dumps(event) + "\n" if case != "unknown-agent" else "partial", encoding="utf-8"
        )
        os.utime(agent, (NOW.timestamp(), NOW.timestamp()))
        if case == "suspected-stall":
            for path in (transcript, agent):
                os.utime(path, (NOW.timestamp() - 3600, NOW.timestamp() - 3600))
    census_calls: list[bool] = []

    def census() -> dict[int, int] | None:
        census_calls.append(True)
        return None if case == "missing-census" else parents

    monkeypatch.setattr(liveness, "process_parents", census)
    monkeypatch.setattr(cli, "_now_utc", lambda: NOW)
    if case != "not-caller":
        monkeypatch.setenv(cli.CLAUDE_SESSION_ENV, SESSION)
    for name in ("alpha", "beta"):
        put_card(store_root, name, "main", "idle", "Synthetic idle project", STAMP)
        bind(store_root, name, "main", SESSION)
    result = invoke("status", "--project", "alpha", "--project", "beta", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert census_calls == [True]
    keep_visible = case in {"permission", "waiting", "suspected-stall", "not-caller"}
    assert data["collapsed_idle"] == ([] if keep_visible else ["alpha/main", "beta/main"])
    for block in data["projects"]:
        row = block["lanes"][0]
        if case in {"probe", "other-child", "wrapper-child"}:
            assert (row["window_state"], row["activity"], row["child_processes"]) == (
                "busy",
                "running",
                4 if case == "probe" else 5,
            )
            assert row["transcript_age_minutes"] == 0
        elif case == "suspected-stall":
            assert row["activity"] == "suspected-stall"


def test_item_escaping_keeps_both_ends_without_cutting_escape_tokens() -> None:
    item = "X" * 18 + "\u4e2d" + "M" * 100 + "\u4e2d" + "z" * 37
    shown = status.ascii_cell(item, 64, keep_tail=True)
    assert shown == "X" * 18 + "..." + r"\u4e2d" + "z" * 37
    assert len(shown) == 64 and shown.isascii()
    assert status.ascii_cell("Short PASS", 64, keep_tail=True) == "Short PASS"


@pytest.mark.parametrize(
    "prompt_activity,stall_activity,prompt_state,expected",
    [
        ("pending-approval", "suspected-stall", "active", "prompted"),
        ("unknown", "suspected-stall", "active", "stalled"),
        ("pending-approval", "suspected-stall", "done", "stalled"),
        ("unknown", "unknown", "active", "blocked"),
    ],
)
def test_cli_attention_prioritizes_approval_then_stall_before_state_or_reply_count(
    invoke: Invoke,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompt_activity: str,
    stall_activity: str,
    prompt_state: str,
    expected: str,
) -> None:
    for lane, state in (
        ("prompted", prompt_state),
        ("stalled", "active"),
        ("blocked", "blocked"),
        ("questions", "active"),
    ):
        put_card(store_root, "alpha", lane, state, "Synthetic work", STAMP)
        bind(store_root, "alpha", lane, SESSION)
    for minute in range(3):
        put_note(store_root, "alpha", "worker", "questions", minute, needs="reply")

    def evidence(self: liveness.Observer, binding: wake.Binding, now: object) -> liveness.Evidence:
        return liveness.Evidence(
            activity={
                "prompted": prompt_activity,
                "stalled": stall_activity,
            }.get(binding.lane, "unknown")
        )

    monkeypatch.setattr(liveness.Observer, "observe", evidence)
    result = invoke("status", "--project", "alpha", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["attention"]["project"] == "alpha"
    assert data["attention"]["lane"] == expected
    if expected == "prompted":
        assert data["attention"]["reason"] == "activity pending-approval"
    elif expected == "stalled":
        assert data["attention"]["reason"] == "activity suspected-stall"
    code, text, _ = status_run(invoke, store_root, "--project", "alpha")
    assert code == 0 and f"look here first: alpha/{expected} - " in text


@pytest.mark.parametrize("failure", ["invalid", "unreadable", "cap"])
def test_incomplete_queue_evidence_never_claims_no_holders(
    invoke: Invoke,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    held(store_root, "alpha")
    if failure == "invalid":
        queue.record_path(Store(store_root), "alpha", "heavy-run").write_bytes(b"not json")
    elif failure == "cap":
        monkeypatch.setattr(status_resources, "MAX_QUEUE_ENTRIES", 0)
    else:

        def unavailable(*args: object, **kwargs: object) -> queue.QueueView:
            raise StoreError("store-unusable", "PRIVATE-SYNTHETIC-ERROR")

        monkeypatch.setattr(queue, "show", unavailable)
    code, text, _ = status_run(invoke, store_root, "--project", "alpha")
    assert code == 0
    assert "queue holders: unknown (incomplete)" in text
    assert "PRIVATE-SYNTHETIC" not in text
    data = json.loads(invoke("status", "--project", "alpha", "--json").stdout)
    assert data["resources"]["queues_complete"] is False
    assert data["diagnostics"]["total"] == 1


@pytest.mark.parametrize("dependency", ["operator", "lane:reviewer"])
def test_attention_reason_keeps_the_dependency_when_another_rule_wins(
    invoke: Invoke,
    store_root: Path,
    dependency: str,
) -> None:
    put_card(store_root, "alpha", "worker", "blocked", "Synthetic work", STAMP)
    store = Store(store_root)
    card = store.scan_cards("alpha")[0][0]
    store.publish_card(replace(card, waiting_on=dependency))
    result = invoke("status", "--project", "alpha", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["attention"] == {
        "project": "alpha",
        "lane": "worker",
        "reason": f"state blocked; waiting on {dependency}",
    }


def test_resource_scan_ignores_other_projects_and_free_queues(
    invoke: Invoke,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held(store_root, "alpha")
    held(store_root, "hidden-project")
    store = Store(store_root)
    with queue.locked(store, "alpha", "free-resource") as path:
        queue.write(store, path, queue.empty_record("alpha", "free-resource"))
    queue.record_path(store, "hidden-project", "heavy-run").write_bytes(b"invalid foreign queue")
    calls: list[str] = []
    original = queue.show

    def show(
        store: Store, project: str, resource: str | None = None, *, max_entries: int | None = None
    ) -> queue.QueueView:
        calls.append(project)
        return original(store, project, resource, max_entries=max_entries)

    monkeypatch.setattr(queue, "show", show)
    result = invoke("status", "--project", "alpha", "--json")
    data = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert calls == ["alpha"]
    assert data["resources"]["queues_complete"] is True
    assert len(data["resources"]["queue_holders"]) == 1
    assert data["diagnostics"]["total"] == 0
    assert "free-resource" not in result.stdout and "hidden-project" not in result.stdout
