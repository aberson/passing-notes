"""The attention view is read-only and uses structured evidence, never card prose."""

import json
from pathlib import Path

from conftest import Invoke
from test_status import NOW, put_card, put_note, tree

from passing_notes import protocol
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
