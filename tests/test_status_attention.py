"""The attention view is read-only and uses structured evidence, never card prose."""

import json
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
    assert "Operator decision notes: 2 recorded" in compact.stdout
    assert "alpha/blocked: card=blocked" in compact.stdout
    assert decision in compact.stdout
    assert blocker_decision in compact.stdout
    assert active_blocker in compact.stdout
    assert "Historical/unlinked received requests: 1" in compact.stdout
    assert received not in compact.stdout
    assert "unresolved evidence, expand with --details" in compact.stdout
    assert "PROJECT" not in compact.stdout
    assert received in detailed.stdout
    assert "PROJECT" in detailed.stdout
    assert received in structured.stdout
    assert tree(store_root) == before


def test_goal_brief_waits_unknown_progress_and_project_focus(
    invoke: Invoke, store_root: Path
) -> None:
    from dataclasses import replace

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
    assert result.stdout.startswith("Goal: unknown")
    assert "Progress since last brief: unknown" in result.stdout
    assert "Next proof: unknown" in result.stdout
    expected = "alpha/review: card=waiting; waits=lane:reviewer; next-action owner=reviewer"
    assert expected in result.stdout
    assert "last verified progress=unknown" in result.stdout
    assert "beta/urgent" not in result.stdout
    assert decision not in result.stdout
    assert "Historical/unlinked received requests: 1" in result.stdout
    assert "not a gate preflight" in result.stdout
    assert tree(store_root) == before
    store.publish_card(replace(card, waiting_on=f"note:{decision}"))
    linked = invoke("status", "--project", "alpha", "--coordinator-attention")
    assert decision in linked.stdout
    assert "acknowledged-unreplied" in linked.stdout


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
        assert "next-action owner=operator" in result.stdout
        assert "Operator waits/host approvals: 1 recorded" in result.stdout
        assert "Operator decision notes: 0 recorded" in result.stdout
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
    assert "next-action owner=operator" in result.stdout
    assert "Operator waits/host approvals: 1 recorded" in result.stdout
    note = put_note(
        store_root, "alpha", "main", "main", 1, needs="operator-decision", kind="question"
    )
    store.publish_ack("alpha", note, protocol.make_ack("main", NOW))
    store.publish_card(replace(card, waiting_on=f"note:{note}"))
    result = invoke("status", "--mine", "--coordinator-attention")
    assert note in result.stdout
    assert "Operator decision notes: 1 recorded" in result.stdout
