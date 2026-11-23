"""Answer-aware monitoring counts; receipt and inbox behavior remain separate."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import Invoke, child_env, note_id, note_text, place_note, run_cli
from test_status import NOW, SESSION, bind, put_card, tree

from passing_notes import cli, protocol, status
from passing_notes.store import Diagnostic, Store, StoreError

CANARY = "synthetic-foreign-private-content"
SUBJECT = "Synthetic actionable subject"


def note(
    store: Store,
    *,
    project: str = "alpha",
    sender: str = "worker",
    to: str = "main",
    needs: str = "operator-decision",
    reply: str = "none",
    supersedes: str | None = None,
    kind: str = "question",
    from_project: str | None = None,
) -> protocol.Note:
    return store.publish_note(
        protocol.make_draft(
            project=project,
            from_project=from_project,
            from_lane=sender,
            to=to,
            subject=SUBJECT,
            kind=kind,
            needs=needs,
            reply_to=reply,
            supersedes=supersedes,
        ),
        CANARY,
        now=lambda: NOW,
    )


@pytest.fixture
def monitored(store_root: Path) -> Store:
    for lane in ("main", "reviewer"):
        put_card(store_root, "alpha", lane, "active", "Synthetic work", "2030-01-02T10:00:00Z")
    return Store(store_root)


def report(invoke: Invoke, store: Store) -> dict[str, Any]:
    before = tree(store.root)
    result = invoke("status", "--project", "alpha", "--json")
    assert result.returncode == 0, result.stderr
    assert tree(store.root) == before
    assert CANARY not in result.stdout
    return cast(dict[str, Any], json.loads(result.stdout))


@pytest.mark.parametrize("project", ["alpha", "dev", "hidden-project"])
def test_answer_from_project_main_clears_counts_not_receipt(
    invoke: Invoke,
    monitored: Store,
    project: str,
) -> None:
    question = note(monitored)
    before = report(invoke, monitored)["projects"][0]
    assert before["operator_decisions"] == 1
    assert (before["lanes"][0]["open"], before["lanes"][0]["open_unblocking"]) == (1, 1)
    note(
        monitored,
        project=project,
        from_project="alpha",
        sender="main",
        to="worker",
        needs="fyi",
        reply=question.id,
    )
    after = report(invoke, monitored)
    block = after["projects"][0]
    assert block["operator_decisions"] == 0
    assert (block["lanes"][0]["open"], block["lanes"][0]["open_unblocking"]) == (0, 0)
    assert after["scope"]["projects"] == ["alpha"]
    assert question.id in {entry.note.id for entry in monitored.inbox("alpha", "main").entries}
    assert not monitored.ack_path("alpha", question.id, "main").exists()


@pytest.mark.parametrize(
    "recipient,sender",
    [("reviewer", "reviewer"), ("reviewer", "main"), ("all", "main"), ("operator", "main")],
)
def test_answered_requests_clear_each_recipient_count(
    invoke: Invoke,
    monitored: Store,
    recipient: str,
    sender: str,
) -> None:
    question = note(monitored, to=recipient)
    note(monitored, sender=sender, to="worker", needs="ack", reply=question.id)
    block = report(invoke, monitored)["projects"][0]
    assert block["operator_decisions"] == 0
    assert all(row["open"] == row["open_unblocking"] == 0 for row in block["lanes"])


@pytest.mark.parametrize("needs", ["reply", "operator-decision"])
@pytest.mark.parametrize("recipient", ["main", "reviewer", "all", "operator"])
@pytest.mark.parametrize(
    "sender_project,sender", [("alpha", "third-lane"), ("beta", "main"), ("beta", "reviewer")]
)
def test_uninvolved_sender_cannot_clear_request(
    invoke: Invoke,
    monitored: Store,
    needs: str,
    recipient: str,
    sender_project: str,
    sender: str,
) -> None:
    question = note(monitored, to=recipient, needs=needs)
    before = report(invoke, monitored)["projects"][0]
    note(
        monitored,
        project="hidden-project",
        from_project=sender_project,
        sender=sender,
        to="worker",
        needs="fyi",
        reply=question.id,
    )
    assert report(invoke, monitored)["projects"][0] == before


@pytest.mark.parametrize("recipient", ["all", "operator"])
def test_broadcast_requires_main_answer(
    invoke: Invoke,
    monitored: Store,
    recipient: str,
) -> None:
    question = note(monitored, to=recipient)
    before = report(invoke, monitored)["projects"][0]
    note(monitored, sender="reviewer", to="worker", needs="fyi", reply=question.id)
    assert report(invoke, monitored)["projects"][0] == before


@pytest.mark.parametrize("recipient", ["main", "reviewer", "all", "operator"])
@pytest.mark.parametrize(
    "sender_project,sender,answers",
    [
        ("alpha", "main", True),
        ("beta", "main", True),
        ("alpha", "reviewer", True),
        ("beta", "reviewer", False),
        ("gamma", "main", False),
    ],
)
def test_cross_project_request_accepts_both_mains_and_destination_addressee(
    invoke: Invoke,
    monitored: Store,
    recipient: str,
    sender_project: str,
    sender: str,
    answers: bool,
) -> None:
    question = note(monitored, from_project="beta", to=recipient)
    before = report(invoke, monitored)["projects"][0]
    note(
        monitored,
        project="hidden-project",
        from_project=sender_project,
        sender=sender,
        to="worker",
        needs="fyi",
        reply=question.id,
    )
    after = report(invoke, monitored)["projects"][0]
    if answers and (sender == "main" or recipient == "reviewer"):
        assert after["operator_decisions"] == 0
        assert all(row["open"] == row["open_unblocking"] == 0 for row in after["lanes"])
    else:
        assert after == before


def test_cross_project_requester_main_cannot_answer_own_request(
    invoke: Invoke,
    monitored: Store,
) -> None:
    question = note(monitored, from_project="beta", sender="main")
    note(monitored, project="beta", sender="main", needs="fyi", reply=question.id)
    assert report(invoke, monitored)["projects"][0]["operator_decisions"] == 1


@pytest.mark.parametrize("needs", ["ack", "fyi"])
def test_reply_does_not_clear_nonrequest_receipt_counts(
    invoke: Invoke,
    monitored: Store,
    needs: str,
) -> None:
    original = note(monitored, needs=needs)
    note(monitored, sender="main", to="worker", needs="fyi", reply=original.id)
    row = report(invoke, monitored)["projects"][0]["lanes"][0]
    assert (row["open"], row["open_unblocking"]) == (1, 0)


def test_reply_request_and_unrelated_question_are_counted_independently(
    invoke: Invoke,
    monitored: Store,
) -> None:
    answered = note(monitored, needs="reply", to="reviewer")
    note(monitored)  # still needs an operator decision
    note(monitored, sender="reviewer", to="main", needs="ack", reply=answered.id)
    block = report(invoke, monitored)["projects"][0]
    assert block["operator_decisions"] == 1
    assert (block["lanes"][0]["open"], block["lanes"][0]["open_unblocking"]) == (2, 1)
    assert block["lanes"][1]["open_unblocking"] == 0


def test_broadcast_remains_in_inbox_without_blocking_every_lane(
    invoke: Invoke,
    monitored: Store,
) -> None:
    for lane in ("main", "reviewer"):
        put_card(
            monitored.root,
            "alpha",
            lane,
            "idle",
            "Synthetic work",
            "2030-01-02T12:00:00Z",
        )
    broadcast = note(monitored, to="all", needs="reply")
    broadcast_only = report(invoke, monitored)
    by_lane = {row["lane"]: row for row in broadcast_only["projects"][0]["lanes"]}
    assert all((row["open"], row["open_unblocking"]) == (1, 0) for row in by_lane.values())
    assert broadcast_only["attention"] is None
    assert broadcast.id in {entry.note.id for entry in monitored.inbox("alpha", "reviewer").entries}

    direct = note(monitored, to="reviewer", needs="reply")
    with_direct = report(invoke, monitored)
    block = with_direct["projects"][0]
    by_lane = {row["lane"]: row for row in block["lanes"]}
    assert (by_lane["main"]["open"], by_lane["main"]["open_unblocking"]) == (1, 0)
    assert (by_lane["reviewer"]["open"], by_lane["reviewer"]["open_unblocking"]) == (2, 1)
    assert with_direct["attention"] == {
        "project": "alpha",
        "lane": "reviewer",
        "reason": "unblocking notes",
    }

    note(monitored, sender="main", to="worker", needs="fyi", reply=broadcast.id)
    block = report(invoke, monitored)["projects"][0]
    by_lane = {row["lane"]: row for row in block["lanes"]}
    assert (by_lane["reviewer"]["open"], by_lane["reviewer"]["open_unblocking"]) == (1, 1)

    note(monitored, sender="reviewer", to="worker", needs="fyi", reply=direct.id)
    after = report(invoke, monitored)
    block = after["projects"][0]
    assert all(row["open_unblocking"] == 0 for row in block["lanes"])
    assert after["attention"] is None


def test_supersedes_keeps_sender_rule_and_superseded_reply_still_counts(
    invoke: Invoke,
    monitored: Store,
) -> None:
    question = note(monitored)
    invalid = note(monitored, sender="reviewer", needs="fyi", supersedes=question.id)
    block = report(invoke, monitored)["projects"][0]
    assert block["operator_decisions"] == 1
    assert block["lanes"][0]["open_unblocking"] == 1
    replacement = note(monitored, needs="operator-decision", supersedes=question.id)
    answer = note(monitored, sender="main", to="worker", needs="fyi", reply=replacement.id)
    note(monitored, sender="main", to="worker", needs="fyi", supersedes=answer.id)
    after = report(invoke, monitored)
    assert after["projects"][0]["operator_decisions"] == 0
    assert after["projects"][0]["lanes"][0]["open_unblocking"] == 0
    assert invalid.id in " ".join(after["diagnostics"]["shown"])


@pytest.mark.parametrize("damage", ["malformed", "oversize", "temporary"])
def test_invalid_foreign_reply_never_resolves_or_leaks(
    invoke: Invoke,
    monitored: Store,
    damage: str,
) -> None:
    question = note(monitored)
    answer = note(
        monitored,
        project="hidden-project",
        from_project="alpha",
        sender="main",
        needs="fyi",
        reply=question.id,
    )
    path = monitored.note_path("hidden-project", answer.id)
    raw = path.read_bytes()
    if damage == "malformed":
        path.write_bytes(raw.replace(b"needs: fyi", b"needs: " + CANARY.encode()))
    elif damage == "oversize":
        path.write_bytes(raw + b"x" * protocol.NOTE_MAX_BYTES)
    else:
        path.rename(path.with_suffix(".tmp"))
    data = report(invoke, monitored)
    assert data["projects"][0]["operator_decisions"] == 1
    assert data["diagnostics"] == {
        "shown": ["status: reply evidence incomplete; unanswered counts may be high"],
        "total": 1,
    }
    assert "hidden-project" not in repr(data) and answer.id not in repr(data)


def test_unreadable_foreign_project_keeps_counts_and_scope(
    invoke: Invoke,
    monitored: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = note(monitored)
    note(
        monitored,
        project="hidden-project",
        from_project="alpha",
        sender="main",
        needs="fyi",
        reply=question.id,
    )
    original = Store.scan_notes

    def read(
        store: Store, project: str, *, max_entries: int | None = None
    ) -> tuple[list[protocol.Note], list[Diagnostic]]:
        if project == "hidden-project":
            raise StoreError("store-unusable", CANARY)
        return original(store, project, max_entries=max_entries)

    monkeypatch.setattr(Store, "scan_notes", read)
    bind(monitored.root, "alpha", "main", SESSION)
    monkeypatch.setenv(cli.CLAUDE_SESSION_ENV, SESSION)
    data = report(invoke, monitored)
    assert data["projects"][0]["operator_decisions"] == 1
    assert data["scope"]["caller"] == ["alpha/main"]
    assert data["diagnostics"]["total"] == 1
    refused = invoke("status", "--all-projects")
    assert refused.returncode == 2 and "status-out-of-scope" in refused.stderr


@pytest.mark.parametrize("all_projects", [False, True])
def test_notes_scanned_once_per_project_and_not_per_lane(
    monitored: Store,
    monkeypatch: pytest.MonkeyPatch,
    all_projects: bool,
) -> None:
    question = note(monitored)
    note(
        monitored,
        project="beta",
        from_project="alpha",
        sender="main",
        needs="fyi",
        reply=question.id,
    )
    calls: Counter[str] = Counter()
    original = Store.scan_notes

    def read(
        store: Store, project: str, *, max_entries: int | None = None
    ) -> tuple[list[protocol.Note], list[Diagnostic]]:
        calls[project] += 1
        return original(store, project, max_entries=max_entries)

    monkeypatch.setattr(Store, "scan_notes", read)
    collected = status.collect(
        monitored, status.Scope(()), None if all_projects else ["alpha"], now=NOW
    )
    assert collected.projects[0].operator_decisions == 0
    assert calls == {"alpha": 1, "beta": 1}
    assert CANARY not in repr(collected)


def test_real_cli_cross_project_answer_resolves_status_only(store_root: Path) -> None:
    put_card(store_root, "alpha", "main", "active", "Synthetic work", "2030-01-02T10:00:00Z")
    ask = run_cli(
        [
            "post",
            "--project",
            "alpha",
            "--from",
            "worker",
            "--to",
            "main",
            "--subject",
            "Synthetic question",
            "--kind",
            "question",
            "--needs",
            "operator-decision",
            "--body-stdin",
        ],
        root=store_root,
        stdin=b"Synthetic question body.",
    )
    assert ask.returncode == 0, ask.stderr
    question = ask.stdout.strip()
    # An unrelated project's main is not the addressee or either request project's main.
    unrelated = run_cli(
        [
            "post",
            "--project",
            "dev",
            "--from",
            "main",
            "--to",
            "main",
            "--subject",
            "Synthetic unrelated follow-up",
            "--kind",
            "answer",
            "--needs",
            "fyi",
            "--reply-to",
            question,
            "--body-stdin",
        ],
        root=store_root,
        stdin=CANARY.encode(),
    )
    assert unrelated.returncode == 0, unrelated.stderr
    pending = run_cli(["status", "--project", "alpha", "--json"], root=store_root)
    assert pending.returncode == 0, pending.stderr
    assert json.loads(pending.stdout)["projects"][0]["operator_decisions"] == 1
    answer = run_cli(
        [
            "post",
            "--project",
            "dev",
            "--from-project",
            "alpha",
            "--from",
            "main",
            "--to",
            "main",
            "--subject",
            "Synthetic linked answer",
            "--kind",
            "answer",
            "--needs",
            "fyi",
            "--reply-to",
            question,
            "--body-stdin",
        ],
        root=store_root,
        stdin=CANARY.encode(),
    )
    assert answer.returncode == 0, answer.stderr
    bind(store_root, "alpha", "main", SESSION)
    env = child_env(store_root, {cli.CLAUDE_SESSION_ENV: SESSION})
    before = tree(store_root)
    result = run_cli(["status", "--project", "alpha", "--json"], root=store_root, env=env)
    assert result.returncode == 0, result.stderr
    assert tree(store_root) == before
    data = json.loads(result.stdout)
    assert data["schema"] == "passing-notes.status/1"
    assert data["projects"][0]["operator_decisions"] == 0
    assert data["projects"][0]["lanes"][0]["open_unblocking"] == 0
    assert CANARY not in result.stdout and '"dev"' not in result.stdout
    inbox = run_cli(["inbox", "--project", "alpha", "--lane", "main"], root=store_root)
    assert question in inbox.stdout


def test_self_reference_is_not_an_answer(invoke: Invoke, monitored: Store) -> None:
    question = note(monitored)
    path = monitored.note_path("alpha", question.id)
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw.replace("reply_to: none", f"reply_to: {question.id}"), encoding="utf-8")
    assert report(invoke, monitored)["projects"][0]["operator_decisions"] == 1


def test_bare_reply_id_resolves_each_matching_request_with_eligible_sender(
    invoke: Invoke,
    monitored: Store,
) -> None:
    question = note(monitored, from_project="gamma")
    note(monitored, project="beta", needs="fyi")  # create the synthetic destination
    raw = monitored.note_path("alpha", question.id).read_bytes()
    monitored.note_path("beta", question.id).write_bytes(
        raw.replace(b"project: alpha\n", b"project: beta\n")
    )
    before = invoke("status", "--all-projects", "--json")
    assert before.returncode == 0, before.stderr
    assert {
        block["project"]: block["operator_decisions"]
        for block in json.loads(before.stdout)["projects"]
    } == {"alpha": 1, "beta": 1}
    note(monitored, project="gamma", sender="main", needs="fyi", reply=question.id)
    result = invoke("status", "--all-projects", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["diagnostics"]["total"] == 0
    assert all(block["operator_decisions"] == 0 for block in data["projects"])


def test_no_foreign_scan_when_local_evidence_resolves_every_request(
    invoke: Invoke,
    monitored: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = note(monitored)
    note(monitored, sender="main", to="worker", needs="fyi", reply=question.id)
    note(monitored, project="hidden-project")
    original = Store.scan_notes

    def read(
        store: Store, project: str, *, max_entries: int | None = None
    ) -> tuple[list[protocol.Note], list[Diagnostic]]:
        assert project == "alpha", "no unresolved request needs a foreign scan"
        return original(store, project, max_entries=max_entries)

    monkeypatch.setattr(Store, "scan_notes", read)
    assert report(invoke, monitored)["projects"][0]["operator_decisions"] == 0


@pytest.mark.parametrize("sender", ["worker", "main"])
@pytest.mark.parametrize("destination", ["alpha", "dev"])
def test_requester_followup_cannot_clear_decision(
    invoke: Invoke,
    monitored: Store,
    sender: str,
    destination: str,
) -> None:
    question = note(monitored, sender=sender)
    note(
        monitored,
        project=destination,
        sender=sender,
        from_project="alpha" if destination != "alpha" else None,
        to="worker",
        needs="fyi",
        reply=question.id,
    )
    block = report(invoke, monitored)["projects"][0]
    assert block["operator_decisions"] == 1
    assert block["lanes"][0]["open_unblocking"] == 1


def test_unrelated_foreign_reply_does_not_resolve_selected_request(
    invoke: Invoke,
    monitored: Store,
) -> None:
    note(monitored)
    elsewhere = note(monitored, project="beta")
    note(monitored, project="dev", sender="main", needs="fyi", reply=elsewhere.id)
    assert report(invoke, monitored)["projects"][0]["operator_decisions"] == 1


def test_mine_uses_foreign_answer_lookup(
    invoke: Invoke,
    monitored: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = note(monitored)
    note(
        monitored,
        project="dev",
        from_project="alpha",
        sender="main",
        needs="fyi",
        reply=question.id,
    )
    bind(monitored.root, "alpha", "main", SESSION)
    monkeypatch.setenv(cli.CLAUDE_SESSION_ENV, SESSION)
    result = invoke("status", "--mine", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["scope"]["projects"] == ["alpha"]
    assert data["projects"][0]["operator_decisions"] == 0


@pytest.mark.parametrize("bound", ["MAX_REPLY_PROJECTS", "MAX_REPLY_ENTRIES"])
def test_foreign_scan_limits_leave_requests_open_with_generic_diagnostic(
    invoke: Invoke,
    monitored: Store,
    monkeypatch: pytest.MonkeyPatch,
    bound: str,
) -> None:
    question = note(monitored)
    note(
        monitored,
        project="hidden-project",
        from_project="alpha",
        sender="main",
        needs="fyi",
        reply=question.id,
    )
    monkeypatch.setattr(status, bound, 0)
    data = report(invoke, monitored)
    assert data["projects"][0]["operator_decisions"] == 1
    assert data["diagnostics"]["total"] == 1
    assert "reply evidence incomplete" in data["diagnostics"]["shown"][0]
    assert "hidden-project" not in repr(data)


def test_foreign_note_listing_is_capped_before_parsing(monitored: Store) -> None:
    for _ in range(3):
        note(monitored, project="beta", sender="main", needs="fyi")
    notes, diagnostics = monitored.scan_notes("beta", max_entries=1)
    assert len(notes) == 1
    assert [str(diagnostic) for diagnostic in diagnostics] == [
        "messages/beta: entry limit reached; scan incomplete"
    ]


def test_foreign_entry_budget_is_shared_across_projects(
    invoke: Invoke,
    monitored: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = note(monitored)
    note(monitored, project="beta", sender="main", needs="fyi")
    note(
        monitored,
        project="gamma",
        from_project="alpha",
        sender="main",
        needs="fyi",
        reply=question.id,
    )
    monkeypatch.setattr(status, "MAX_REPLY_ENTRIES", 1)
    data = report(invoke, monitored)
    assert data["projects"][0]["operator_decisions"] == 1
    assert data["diagnostics"]["total"] == 1


def _foreign_notes(root: Path, count: int) -> None:
    """``count`` unrelated hand-written notes in project ``beta`` (one folder)."""
    for index in range(count):
        hex4 = f"{index:04x}"
        text = note_text(
            stamp="20300102T100000Z",
            created_utc="2030-01-02T10:00:00Z",
            sender="main",
            recipient="lane-b",
            hex4=hex4,
            project="beta",
        )
        place_note(root, text, f"{note_id('20300102T100000Z', 'main', 'lane-b', hex4)}.md", "beta")


def test_reply_scan_covers_a_store_of_5000_foreign_notes(
    invoke: Invoke, monitored: Store, store_root: Path
) -> None:
    """D87: the bound is 32768 entries, so a store above the old 4096 is read whole."""
    assert status.MAX_REPLY_ENTRIES == 32768
    note(monitored)
    _foreign_notes(store_root, 5000)
    data = report(invoke, monitored)
    assert data["projects"][0]["operator_decisions"] == 1
    assert data["diagnostics"]["total"] == 0, data["diagnostics"]


def test_reply_scan_over_the_bound_still_reports_incomplete_evidence(
    invoke: Invoke, monitored: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note(monitored)
    _foreign_notes(store_root, 11)
    monkeypatch.setattr(status, "MAX_REPLY_ENTRIES", 10)
    data = report(invoke, monitored)
    assert data["projects"][0]["operator_decisions"] == 1
    assert data["diagnostics"]["shown"] == [
        "status: reply evidence incomplete; unanswered counts may be high"
    ]
