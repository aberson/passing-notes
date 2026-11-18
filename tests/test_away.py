"""Before-away monitoring through CLI scopes, validated notes and host evidence."""

from __future__ import annotations

import errno
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import Invoke, child_env, fake_status_sessions, run_cli
from test_cli import ClosedPipe
from test_status import SESSION, bind, put_card, put_note, tree
from test_status_replies import note

from passing_notes import cli, liveness, status, wake
from passing_notes.store import Store

STAMP = "2030-01-02T10:00:00Z"
CANARY = "SYNTHETIC-NOTE-CONTENT-NOT-FOR-STATUS"


def card(root: Path, lane: str, state: str = "active", waiting: str | None = None) -> None:
    put_card(root, "alpha", lane, state, f"Synthetic {lane} work", STAMP)
    store = Store(root)
    saved = next(c for c in store.scan_cards("alpha")[0] if c.lane == lane)
    store.publish_card(replace(saved, waiting_on=waiting))
    bind(root, "alpha", lane, SESSION)


def test_real_cli_lists_permission_wait_and_answered_decisions_age_out(
    store_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_status_sessions(
        tmp_path / "host",
        monkeypatch,
        [
            {
                "sessionId": SESSION,
                "status": "waiting",
                "waitingFor": "permission prompt",
            }
        ],
    )
    card(store_root, "worker")
    question = put_note(
        store_root,
        "alpha",
        "worker",
        "main",
        1,
        needs="operator-decision",
        subject=CANARY,
        body=CANARY,
    )
    env = child_env(store_root, {cli.CLAUDE_SESSION_ENV: ""})
    before = tree(store_root)
    result = run_cli(["away-check", "--project", "alpha", "--json"], root=store_root, env=env)
    assert result.returncode == 3, result.stderr
    data = json.loads(result.stdout)
    assert data["schema"] == "passing-notes.away-check/1"
    assert data["exit_code"] == 3
    assert [f["kind"] for f in data["findings"]] == ["pending-approval", "operator-decisions"]
    assert data["findings"][0]["lane"] == "worker"
    assert data["findings"][1]["lane"] is None and data["findings"][1]["count"] == 1
    assert CANARY not in result.stdout
    text = run_cli(["away-check", "--project", "alpha"], root=store_root, env=env)
    assert text.returncode == 3, text.stderr
    assert "pending-approval" in text.stdout and "operator-decisions" in text.stdout
    assert tree(store_root) == before
    note(Store(store_root), sender="main", to="worker", needs="fyi", reply=question)
    after = tree(store_root)
    result = run_cli(["away-check", "--project", "alpha", "--json"], root=store_root, env=env)
    assert result.returncode == 3
    assert [f["kind"] for f in json.loads(result.stdout)["findings"]] == ["pending-approval"]
    assert tree(store_root) == after
    assert not Store(store_root).ack_path("alpha", question, "main").exists()


def test_cli_lists_each_operator_need_once_and_ignores_ordinary_waits(
    invoke: Invoke,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card(store_root, "approval", waiting="operator")
    card(store_root, "stalled")
    card(store_root, "operator-waiter", state="waiting", waiting="operator")
    card(store_root, "resource", state="waiting", waiting="resource:heavy-run")
    card(store_root, "blocked", state="blocked", waiting="lane:reviewer")
    card(store_root, "finished", state="done")
    put_note(store_root, "alpha", "worker", "blocked", 1, needs="reply")
    for minute in (2, 3):
        put_note(store_root, "alpha", "worker", "operator", minute, needs="operator-decision")

    def observe(self: liveness.Observer, binding: wake.Binding, now: object) -> liveness.Evidence:
        activity = {
            "approval": "pending-approval",
            "stalled": "suspected-stall",
            "finished": "pending-approval",
        }.get(binding.lane, "idle")
        return liveness.Evidence("wakeable", activity)

    monkeypatch.setattr(liveness.Observer, "observe", observe)
    before = tree(store_root)
    result = invoke("away-check", "--project", "alpha", "--json")
    assert result.returncode == 3, result.stderr
    data = json.loads(result.stdout)
    assert [(f["lane"], f["kind"], f["count"]) for f in data["findings"]] == [
        ("approval", "pending-approval", 1),
        ("stalled", "suspected-stall", 1),
        ("operator-waiter", "operator-wait", 1),
        (None, "operator-decisions", 2),
    ]
    assert data["unknown_activity"] == 0
    assert tree(store_root) == before


@pytest.mark.parametrize("activity", ["unknown", "idle", "running", "recent"])
def test_no_observed_actions_exit_zero_without_claiming_health(
    invoke: Invoke,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    activity: str,
) -> None:
    card(store_root, "worker")
    monkeypatch.setattr(
        liveness.Observer,
        "observe",
        lambda self, binding, now: liveness.Evidence("unknown", activity),
    )
    result = invoke("away-check", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["findings"] == [] and data["exit_code"] == 0
    assert data["unknown_activity"] == int(activity == "unknown")
    text = invoke("away-check").stdout
    assert "no observed operator actions" in text
    if activity == "unknown":
        assert "not proof that every lane is healthy" in text


def test_answer_links_clear_decision_without_ack_and_requester_cannot_clear_it(
    invoke: Invoke,
    store_root: Path,
) -> None:
    store = Store(store_root)
    question = note(store)
    note(store, reply=question.id, needs="fyi")
    assert invoke("away-check", "--project", "alpha").returncode == 3
    note(store, project="dev", from_project="alpha", sender="main", reply=question.id, needs="fyi")
    before = tree(store_root)
    result = invoke("away-check", "--project", "alpha", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["findings"] == []
    assert tree(store_root) == before
    assert question.id in {entry.note.id for entry in store.inbox("alpha", "main").entries}


@pytest.mark.parametrize(
    "caller,expected,exit_code",
    [
        ("unbound", ["alpha", "beta"], 3),
        ("alpha", ["alpha"], 0),
        ("dev", ["alpha", "beta", "dev"], 3),
    ],
)
def test_default_scope_is_every_project_the_caller_may_see(
    invoke: Invoke,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
    expected: list[str],
    exit_code: int,
) -> None:
    put_card(store_root, "alpha", "main", "idle", "Synthetic", STAMP)
    note(Store(store_root), project="beta")
    if caller != "unbound":
        put_card(store_root, caller, "main", "idle", "Synthetic", STAMP)
        bind(store_root, caller, "main", SESSION)
        monkeypatch.setenv(cli.CLAUDE_SESSION_ENV, SESSION)
    result = invoke("away-check", "--json")
    assert result.returncode == exit_code, result.stderr
    data = json.loads(result.stdout)
    assert data["scope"]["projects"] == expected
    if caller == "alpha":
        assert "beta" not in result.stdout


def test_explicit_scope_refusal_precedes_all_observation(
    invoke: Invoke,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card(store_root, "main", "idle")
    monkeypatch.setenv(cli.CLAUDE_SESSION_ENV, SESSION)

    def forbidden(*args: object, **kwargs: object) -> status.Report:
        raise AssertionError("out-of-scope must not collect")

    monkeypatch.setattr(status, "collect", forbidden)
    for args in (("--all-projects",), ("--project", "alpha", "--project", "beta")):
        result = invoke("away-check", *args)
        assert result.returncode == 2 and result.stdout == ""
        assert "status-out-of-scope" in result.stderr


def test_explicit_projects_mine_and_usage_errors(
    invoke: Invoke,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card(store_root, "main", "idle")
    bind(store_root, "alpha", "main", SESSION)
    monkeypatch.setenv(cli.CLAUDE_SESSION_ENV, SESSION)
    result = invoke("away-check", "--mine", "--json")
    assert json.loads(result.stdout)["scope"]["projects"] == ["alpha"]
    monkeypatch.delenv(cli.CLAUDE_SESSION_ENV)
    assert invoke("away-check", "--mine").returncode == 2
    result = invoke(
        "away-check", "--project", "beta", "--project", "alpha", "--project", "beta", "--json"
    )
    assert json.loads(result.stdout)["scope"]["projects"] == ["beta", "alpha"]
    for args in (
        ("--mine", "--all-projects"),
        ("--project", "../escape"),
        ("--project", "alpha", "--all-projects"),
    ):
        assert invoke("away-check", *args).returncode == 2
    assert invoke("away-check", "--help").returncode == 0


def test_missing_store_stays_missing_and_queue_diagnostics_stay_visible(
    invoke: Invoke,
    store_root: Path,
) -> None:
    assert not store_root.exists()
    result = run_cli(["away-check", "--json"], root=store_root)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["findings"] == []
    assert not store_root.exists()
    folder = store_root / "queues" / "alpha"
    folder.mkdir(parents=True)
    (folder / "heavy.queue.json").write_bytes(b"invalid")
    before = tree(store_root)
    result = invoke("away-check", "--project", "alpha", "--json")
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["diagnostics"]["total"] == 1
    assert data["resources"]["queues_complete"] is False
    assert "evidence incomplete" in invoke("away-check", "--project", "alpha").stdout
    assert tree(store_root) == before


def test_unusable_store_is_an_error_not_a_clean_away_check(
    invoke: Invoke, store_root: Path
) -> None:
    store_root.write_bytes(b"synthetic non-directory store")
    result = invoke("away-check", "--json")
    assert result.returncode == 4 and result.stdout == ""
    assert "store-unusable" in result.stderr


@pytest.mark.parametrize("on_flush", [False, True])
def test_findings_and_output_failure_have_distinct_exit_codes(
    invoke: Invoke,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    on_flush: bool,
) -> None:
    note(Store(store_root))
    assert invoke("away-check", "--project", "alpha").returncode == 3
    monkeypatch.setattr(
        sys, "stdout", ClosedPipe(BrokenPipeError(errno.EPIPE, "Broken pipe"), on_flush=on_flush)
    )
    result = invoke("away-check", "--project", "alpha", "--json")
    assert result.returncode == 1
    assert "output-unwritable" in result.stderr
