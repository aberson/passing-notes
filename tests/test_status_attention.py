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
