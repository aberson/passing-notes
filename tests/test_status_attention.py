"""The attention view is read-only and uses structured evidence, never card prose."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import Invoke
from test_status import NOW, SESSION, put_card, put_note, tree

from passing_notes import protocol, wake
from passing_notes.store import Store


def test_attention_filters_without_guessing_and_preserves_requests(
    invoke: Invoke,
    store_root: Path,
) -> None:
    for lane, state in (
        ("blocked", "blocked"),
        ("review", "waiting"),
        ("healthy", "active"),
        ("done", "done"),
        ("main", "active"),
    ):
        put_card(
            store_root, "alpha", lane, state, "review pending in prose", "2030-01-02T11:00:00Z"
        )
    Store(store_root).publish_card(
        protocol.make_card(
            project="alpha",
            lane="dependency",
            host="codex",
            state="idle",
            item="Waiting for reviewer",
            waiting_on="lane:review",
            updated=NOW,
        )
    )
    put_note(store_root, "alpha", "worker", "main", 1, needs="reply", kind="question")
    before = tree(store_root)
    result = invoke("status", "--project", "alpha", "--needs-attention", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert {row["lane"] for row in data["projects"][0]["lanes"]} == {
        "blocked",
        "review",
        "dependency",
        "main",
    }
    assert data["projects"][0]["done_count"] == 1
    assert data["collapsed_idle"] == []
    assert tree(store_root) == before


def test_attention_empty_is_not_a_health_claim(invoke: Invoke, store_root: Path) -> None:
    put_card(store_root, "alpha", "worker", "active", "review pending", "2030-01-02T11:00:00Z")
    result = invoke("status", "--project", "alpha", "--needs-attention")
    assert result.returncode == 0
    assert "absence is not proof" in result.stdout
    assert "no recorded attention matches" in result.stdout
    assert "no lane cards" not in result.stdout


def test_coordinator_attention_flag_accepts_skill_command_without_changing_status(
    invoke: Invoke, store_root: Path
) -> None:
    put_card(store_root, "alpha", "main", "active", "Synthetic work", protocol.format_utc(NOW))
    before = tree(store_root)
    normal = invoke("status", "--project", "alpha", "--live", "--json")
    coordinator = invoke(
        "status", "--project", "alpha", "--live", "--coordinator-attention", "--json"
    )
    assert normal.returncode == coordinator.returncode == 0
    normal_data = json.loads(normal.stdout)
    coordinator_data = json.loads(coordinator.stdout)
    assert normal_data["projects"] == coordinator_data["projects"]
    assert normal_data["coordinator_attention"] == coordinator_data["coordinator_attention"] == []
    assert tree(store_root) == before


def test_coordinator_attention_orders_structured_evidence_and_separates_delivery(
    invoke: Invoke, store_root: Path
) -> None:
    store = Store(store_root)
    for lane in ("main", "build", "reviewer", "read", "resolved"):
        put_card(store_root, "alpha", lane, "active", "Synthetic work", "2030-01-02T11:00:00Z")
    put_card(store_root, "alpha", "closed", "done", "Synthetic closed work", "2030-01-02T11:00:00Z")
    store.publish_card(
        protocol.make_card(
            project="alpha",
            lane="reviewer",
            host="codex",
            state="active",
            item="Synthetic independent review",
            waiting_on="time:2020-01-02T11:30:00Z",
            updated=NOW,
        )
    )
    handoff = put_note(store_root, "alpha", "build", "main", 10, needs="reply", kind="handoff")
    decision = put_note(
        store_root,
        "alpha",
        "reviewer",
        "main",
        11,
        needs="operator-decision",
        kind="question",
    )
    answered = put_note(store_root, "alpha", "resolved", "main", 12, needs="reply", kind="handoff")
    store.publish_ack("alpha", answered, protocol.make_ack("main", NOW))
    read = put_note(store_root, "alpha", "read", "main", 14, needs="reply", kind="handoff")
    store.publish_ack("alpha", read, protocol.make_ack("main", NOW))
    closed = put_note(store_root, "alpha", "closed", "main", 15, needs="reply", kind="handoff")
    store.publish_ack("alpha", closed, protocol.make_ack("main", NOW))
    store.publish_note(
        protocol.make_draft(
            project="alpha",
            from_lane="main",
            to="resolved",
            subject="Synthetic disposition",
            kind="answer",
            needs="ack",
            reply_to=answered,
        ),
        "Synthetic disposition body.",
        now=lambda: protocol.parse_utc("2030-01-02T10:13:00Z", "created_utc"),
    )
    ring, _ = wake.ring_paths(store, "alpha", handoff, "main", SESSION)
    ring.write_text(
        json.dumps(
            {
                "note_id": handoff,
                "lane": "main",
                "session_id": SESSION,
                "attempt_utc": "2030-01-02T10:10:01Z",
                "state": wake.RING_QUEUED,
            }
        ),
        encoding="utf-8",
    )

    result = invoke("status", "--project", "alpha", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["coordinator_attention"] == [
        {
            "priority": 1,
            "kind": "gate-handoff",
            "project": "alpha",
            "lane": "build",
            "evidence": handoff,
            "summary": "Synthetic subject",
            "delivery": "queued-unverified",
            "action": "read and disposition for build",
            "history": False,
        },
        {
            "priority": 1,
            "kind": "gate-handoff",
            "project": "alpha",
            "lane": "closed",
            "evidence": closed,
            "summary": "Synthetic subject",
            "delivery": "acknowledged-unreplied",
            "action": "read and disposition for closed",
            "history": True,
        },
        {
            "priority": 1,
            "kind": "gate-handoff",
            "project": "alpha",
            "lane": "read",
            "evidence": read,
            "summary": "Synthetic subject",
            "delivery": "acknowledged-unreplied",
            "action": "read and disposition for read",
            "history": False,
        },
        {
            "priority": 2,
            "kind": "operator-decision",
            "project": "alpha",
            "lane": "reviewer",
            "evidence": decision,
            "summary": "Synthetic subject",
            "delivery": "posted-unrung",
            "action": "show the operator, then reply through main",
            "history": False,
        },
        {
            "priority": 3,
            "kind": "overdue-checkpoint",
            "project": "alpha",
            "lane": "reviewer",
            "evidence": "time:2020-01-02T11:30:00Z",
            "summary": "promised review/status checkpoint elapsed",
            "delivery": "overdue",
            "action": "request status from reviewer",
            "history": False,
        },
    ]
    assert answered not in result.stdout
    assert "Synthetic disposition" not in result.stdout


def test_work_lane_cannot_see_coordinator_note_ids_or_subjects(
    invoke: Invoke, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(store_root)
    for lane in ("main", "build"):
        store.publish_card(
            protocol.make_card(
                project="alpha",
                lane=lane,
                host="codex",
                state="active",
                item="Synthetic work",
                window=SESSION if lane == "build" else None,
                updated=NOW,
            )
        )
    wake.save_binding(
        store,
        wake.Binding("alpha", "build", "codex", SESSION, "queue", "2030-01-02T10:00:00Z"),
    )
    note_id = put_note(
        store_root,
        "alpha",
        "build",
        "main",
        20,
        needs="reply",
        kind="handoff",
        subject="PRIVATE-ACTIONABLE-SUBJECT",
    )
    monkeypatch.setenv("CODEX_THREAD_ID", SESSION)

    result = invoke("status", "--project", "alpha", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["coordinator_attention"] == []
    assert note_id not in result.stdout
    assert "PRIVATE-ACTIONABLE-SUBJECT" not in result.stdout


def test_compact_coordinator_view_keeps_fresh_requests_and_expands_received_evidence(
    invoke: Invoke, store_root: Path
) -> None:
    for lane, state in (
        ("main", "active"),
        ("blocked", "blocked"),
        ("blocked-ask", "blocked"),
        ("fresh", "active"),
        ("old", "done"),
    ):
        put_card(store_root, "alpha", lane, state, "Synthetic card text", "2030-01-02T11:00:00Z")
    decision = put_note(
        store_root, "alpha", "fresh", "main", 1, needs="operator-decision", kind="question"
    )
    active_blocker = put_note(
        store_root, "alpha", "blocked", "main", 2, needs="reply", kind="blocker"
    )
    blocker_decision = put_note(
        store_root,
        "alpha",
        "blocked-ask",
        "main",
        4,
        needs="operator-decision",
        kind="blocker",
    )
    received = put_note(store_root, "alpha", "old", "main", 3, needs="reply", kind="handoff")
    Store(store_root).publish_ack("alpha", received, protocol.make_ack("main", NOW))
    before = tree(store_root)

    compact = invoke("status", "--project", "alpha", "--live", "--coordinator-attention")
    detailed = invoke(
        "status", "--project", "alpha", "--live", "--coordinator-attention", "--details"
    )
    structured = invoke(
        "status", "--project", "alpha", "--live", "--coordinator-attention", "--json"
    )
    assert compact.returncode == detailed.returncode == structured.returncode == 0
    assert "| Coordinator question | Evidence-backed answer |" in compact.stdout
    assert "| Running now? |" in compact.stdout
    assert "| Blocked / owner / unblock? |" in compact.stdout
    assert "| Operator review or approval now? |" in compact.stdout
    assert "| Visible windows? |" in compact.stdout
    assert "alpha/blocked: waits=unknown, owner=unknown, unblock=unknown" in compact.stdout
    assert decision in compact.stdout
    assert blocker_decision in compact.stdout
    assert active_blocker in compact.stdout
    assert "history: 1 acknowledged, unreplied (oldest 2030-01-02)" in compact.stdout
    assert received not in compact.stdout
    assert "unresolved evidence, expand with --details" in compact.stdout
    assert "PROJECT" not in compact.stdout
    assert received in detailed.stdout
    assert "PROJECT" in detailed.stdout
    assert received in structured.stdout
    assert tree(store_root) == before


def test_goal_brief_waits_unknown_progress_and_project_focus(
    invoke: Invoke, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from passing_notes import cli

    # The acknowledged decision from a live sender is history only once it is old (D84).
    monkeypatch.setattr(cli, "_now_utc", lambda: NOW + timedelta(hours=72))

    put_card(store_root, "alpha", "review", "waiting", "Synthetic review", "2030-01-02T11:00:00Z")
    put_card(store_root, "beta", "urgent", "blocked", "Synthetic urgency", "2030-01-02T11:00:00Z")
    store = Store(store_root)
    card = store.scan_cards("alpha")[0][0]
    store.publish_card(replace(card, waiting_on="lane:reviewer"))
    decision = put_note(
        store_root, "alpha", "review", "main", 1, needs="operator-decision", kind="question"
    )
    store.publish_ack("alpha", decision, protocol.make_ack("main", NOW))
    before = tree(store_root)
    result = invoke("status", "--project", "alpha", "--coordinator-attention")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("| Coordinator question | Evidence-backed answer |")
    expected = "alpha/review: waits=lane:reviewer, owner=reviewer, unblock=unknown"
    assert expected in result.stdout
    assert (
        "Host observations only; running work and useful progress are unverified." in result.stdout
    )
    assert "beta/urgent" not in result.stdout
    assert decision not in result.stdout
    assert "history: 1 acknowledged, unreplied (oldest 2030-01-02)" in result.stdout
    assert "not a gate preflight" in result.stdout
    assert tree(store_root) == before
    store.publish_card(replace(card, waiting_on=f"note:{decision}"))
    linked = invoke("status", "--project", "alpha", "--coordinator-attention")
    assert decision in linked.stdout
    assert "acknowledged-unreplied" in linked.stdout


def test_compact_table_makes_subject_inert_and_labels_non_blocking_action(
    invoke: Invoke, store_root: Path
) -> None:
    put_card(store_root, "alpha", "build", "active", "Synthetic build", "2030-01-02T11:00:00Z")
    subject = "Review [link](https://example.invalid) <img src=x> `tick` | pipe"
    note = put_note(
        store_root,
        "alpha",
        "build",
        "main",
        1,
        needs="reply",
        kind="handoff",
        subject=subject,
    )
    result = invoke("status", "--project", "alpha", "--coordinator-attention")
    assert result.returncode == 0, result.stderr
    assert "Coordinator next action (not blocker evidence): alpha/build" in result.stdout
    assert (
        "`` Review [link](https://example.invalid) <img src=x> `tick` \\| pipe ``" in result.stdout
    )
    assert note in result.stdout
    assert "alpha/build: waits=" not in result.stdout


@pytest.mark.parametrize("approval", [False, True])
def test_brief_idle_collapse_and_host_approval_owner(
    invoke: Invoke, store_root: Path, monkeypatch: pytest.MonkeyPatch, approval: bool
) -> None:
    from test_status import bind

    from passing_notes import cli, liveness

    put_card(store_root, "alpha", "main", "idle", "Synthetic idle", "2030-01-02T11:00:00Z")
    bind(store_root, "alpha", "main", SESSION)
    monkeypatch.setenv(cli.CLAUDE_SESSION_ENV, SESSION)
    evidence = liveness.Evidence(
        window_state="waiting" if approval else "idle",
        activity="pending-approval" if approval else "idle",
    )
    monkeypatch.setattr(liveness.Observer, "observe", lambda *args: evidence)
    result = invoke("status", "--mine", "--coordinator-attention")
    assert result.returncode == 0, result.stderr
    if approval:
        assert "owner=operator" in result.stdout
        assert "alpha/main: operator action indicated" in result.stdout
    else:
        assert "alpha/main:" not in result.stdout
        assert "Idle caller rows collapsed: 1" in result.stdout
        expanded = invoke("status", "--mine", "--coordinator-attention", "--expand-idle")
        assert "alpha/main:" in expanded.stdout
        assert "Idle caller rows collapsed:" not in expanded.stdout


def test_collapsed_idle_retains_operator_dependency_and_linked_request(
    invoke: Invoke, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from test_status import bind

    from passing_notes import cli, liveness

    put_card(store_root, "alpha", "main", "idle", "Synthetic idle", "2030-01-02T11:00:00Z")
    bind(store_root, "alpha", "main", SESSION)
    monkeypatch.setenv(cli.CLAUDE_SESSION_ENV, SESSION)
    monkeypatch.setattr(liveness.Observer, "observe", lambda *args: liveness.Evidence())
    store = Store(store_root)
    card = store.scan_cards("alpha")[0][0]
    store.publish_card(replace(card, waiting_on="operator"))
    result = invoke("status", "--mine", "--coordinator-attention")
    assert "Idle caller rows collapsed: 1" in result.stdout
    assert "owner=operator" in result.stdout
    assert "alpha/main: operator action indicated" in result.stdout
    note = put_note(
        store_root, "alpha", "main", "main", 1, needs="operator-decision", kind="question"
    )
    store.publish_ack("alpha", note, protocol.make_ack("main", NOW))
    store.publish_card(replace(card, waiting_on=f"note:{note}"))
    result = invoke("status", "--mine", "--coordinator-attention")
    assert note in result.stdout
    assert "| Operator review or approval now? |" in result.stdout


# --- D84: history on coordinator_attention entries ---------------------------------------

OPERATOR_ROW = "| Operator review or approval now? |"
UPDATED = "2030-01-02T11:00:00Z"


def _decision(store_root: Path, sender_state: str | None, *, acked: bool = True) -> str:
    """An operator decision from ``asker`` to main, posted at 10:00 (two hours before
    NOW); ``sender_state`` None leaves the asker without a card."""
    put_card(store_root, "alpha", "main", "active", "Synthetic coordination", UPDATED)
    if sender_state is not None:
        put_card(store_root, "alpha", "asker", sender_state, "Synthetic work", UPDATED)
    note_id = put_note(
        store_root, "alpha", "asker", "main", 0, needs="operator-decision", kind="question"
    )
    if acked:
        Store(store_root).publish_ack("alpha", note_id, protocol.make_ack("main", NOW))
    return note_id


def _at(monkeypatch: pytest.MonkeyPatch, hours_after_now: int) -> None:
    from passing_notes import cli

    monkeypatch.setattr(cli, "_now_utc", lambda: NOW + timedelta(hours=hours_after_now))


def _entries(invoke: Invoke) -> list[dict[str, object]]:
    result = invoke("status", "--project", "alpha", "--json")
    assert result.returncode == 0, result.stderr
    entries: list[dict[str, object]] = json.loads(result.stdout)["coordinator_attention"]
    return entries


def _operator_row(invoke: Invoke) -> tuple[str, str]:
    result = invoke("status", "--project", "alpha", "--coordinator-attention")
    assert result.returncode == 0, result.stderr
    (row,) = [line for line in result.stdout.splitlines() if line.startswith(OPERATOR_ROW)]
    return row, result.stdout


@pytest.mark.parametrize("sender_state", ["done", None])
def test_acknowledged_decision_from_done_or_absent_sender_is_history(
    invoke: Invoke, store_root: Path, monkeypatch: pytest.MonkeyPatch, sender_state: str | None
) -> None:
    _at(monkeypatch, 0)
    note_id = _decision(store_root, sender_state)
    before = tree(store_root)
    (entry,) = _entries(invoke)
    assert entry["evidence"] == note_id
    assert entry["delivery"] == "acknowledged-unreplied"
    assert entry["history"] is True
    row, text = _operator_row(invoke)
    assert note_id not in row
    assert "history: 1 acknowledged, unreplied (oldest 2030-01-02)" in text
    assert tree(store_root) == before


@pytest.mark.parametrize(("hours_after_now", "history"), [(0, False), (71, True)])
def test_acknowledged_decision_from_live_sender_becomes_history_after_72_hours(
    invoke: Invoke,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    hours_after_now: int,
    history: bool,
) -> None:
    """Two hours old: live, in the operator row (today's any-age split called it
    historical). Seventy-three hours old: history."""
    _at(monkeypatch, hours_after_now)
    note_id = _decision(store_root, "active")
    (entry,) = _entries(invoke)
    assert entry["history"] is history
    row, text = _operator_row(invoke)
    assert (note_id in row) is not history
    count = 1 if history else 0
    assert f"history: {count} acknowledged, unreplied" in text


@pytest.mark.parametrize("waiting_lane", ["main", "asker"])
def test_card_waiting_on_the_note_keeps_it_live(
    invoke: Invoke, store_root: Path, monkeypatch: pytest.MonkeyPatch, waiting_lane: str
) -> None:
    from dataclasses import replace

    _at(monkeypatch, 71)
    note_id = _decision(store_root, "done" if waiting_lane == "main" else "active")
    store = Store(store_root)
    (card,) = [c for c in store.scan_cards("alpha")[0] if c.lane == waiting_lane]
    store.publish_card(replace(card, waiting_on=f"note:{note_id}"))
    (entry,) = _entries(invoke)
    assert entry["history"] is False
    row, _ = _operator_row(invoke)
    assert note_id in row


def test_main_reply_with_reply_to_still_removes_the_entry(
    invoke: Invoke, store_root: Path, tmp_path: Path
) -> None:
    note_id = _decision(store_root, "done")
    assert [entry["evidence"] for entry in _entries(invoke)] == [note_id]
    body = tmp_path / "answer.txt"
    body.write_text("operator-said: synthetic answer\n", encoding="utf-8")
    posted = invoke(
        "post",
        "--project",
        "alpha",
        "--from",
        "main",
        "--to",
        "asker",
        "--subject",
        "Synthetic answer",
        "--kind",
        "answer",
        "--needs",
        "ack",
        "--reply-to",
        note_id,
        "--body-file",
        str(body),
    )
    assert posted.returncode == 0, posted.stderr
    assert "hint:" not in posted.stderr
    assert _entries(invoke) == []


def test_unacknowledged_delivery_is_never_history(
    invoke: Invoke, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _at(monkeypatch, 71)
    note_id = _decision(store_root, "done", acked=False)
    ring, _ = wake.ring_paths(Store(store_root), "alpha", note_id, "main", SESSION)
    for state in (None, wake.RING_QUEUED, wake.RING_DELIVERED):
        if state is not None:
            ring.write_text(
                json.dumps(
                    {
                        "note_id": note_id,
                        "lane": "main",
                        "session_id": SESSION,
                        "attempt_utc": "2030-01-02T10:00:01Z",
                        "state": state,
                    }
                ),
                encoding="utf-8",
            )
        (entry,) = _entries(invoke)
        assert entry["delivery"] != "acknowledged-unreplied"
        assert entry["history"] is False
        row, _ = _operator_row(invoke)
        assert note_id in row


def test_history_is_the_last_field_with_a_false_default() -> None:
    from passing_notes import status

    item = status.CoordinatorAttention(2, "operator-decision", "alpha", "asker", "x", "s", "d", "a")
    assert item.history is False
    assert status.JSON_KEYS["coordinator_attention"][-1] == "history"
    assert status.JSON_KEYS["coordinator_attention"][:-1] == (
        "priority",
        "kind",
        "project",
        "lane",
        "evidence",
        "summary",
        "delivery",
        "action",
    )
    assert status.history_line([]) == (
        "history: 0 acknowledged, unreplied; "
        "unresolved evidence, expand with --details (receipt is not resolution)"
    )
