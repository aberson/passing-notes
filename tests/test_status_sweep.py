"""``passing-notes status --sweep``: one read-only row per finding, with its action word
(plan D62-D64), through the production ``cli.main`` entry point."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import Invoke, Result, fake_status_sessions, note_text, place_note
from test_status import tree

from passing_notes import cli, protocol, status, status_resources, sweep, wake
from passing_notes.store import Store

NOW = datetime(2030, 1, 10, 12, 0, 0, tzinfo=UTC)
CANARY = "CANARY-SWEEP-BODY-5d1e"
S_IDLE = "aaaaaaaa-0000-4000-8000-000000000001"
S_BUSY = "aaaaaaaa-0000-4000-8000-000000000002"
S_DONE = "aaaaaaaa-0000-4000-8000-000000000003"
S_STAND = "aaaaaaaa-0000-4000-8000-000000000004"
CALLER = "aaaaaaaa-0000-4000-8000-000000000005"
GIB = 2**30


@pytest.fixture(autouse=True)
def fixed_clock_and_low_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_now_utc", lambda: NOW)
    monkeypatch.setattr(status_resources, "free_memory_bytes", lambda: 2 * GIB)


def card(root: Path, project: str, lane: str, state: str, age: timedelta) -> None:
    Store(root).publish_card(
        protocol.make_card(
            project=project,
            lane=lane,
            host="claude",
            item=f"Synthetic {lane}",
            state=state,
            updated=NOW - age,
        )
    )


def bind(root: Path, project: str, lane: str, session_id: str) -> None:
    wake.save_binding(
        Store(root),
        wake.Binding(
            project, lane, wake.HOST_CLAUDE, session_id, "default", "2030-01-01T00:00:00Z"
        ),
    )


def note(
    root: Path,
    project: str,
    to: str,
    age: timedelta,
    hex4: str,
    *,
    needs: str,
    kind: str = "fyi",
    sender: str = "worker",
) -> str:
    created = NOW - age
    stamp = created.strftime("%Y%m%dT%H%M%SZ")
    text = note_text(
        stamp=stamp,
        created_utc=protocol.format_utc(created),
        sender=sender,
        recipient=to,
        hex4=hex4,
        project=project,
        needs=needs,
        kind=kind,
        body=f"{CANARY} body text.",
    )
    note_id = f"{stamp}-{sender}-to-{to}-{hex4}"
    place_note(root, text, f"{note_id}.md", project)
    return note_id


class Ids:
    decision = ""
    oldest_fyi = ""
    direct = ""


def build_fixture(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Ids:
    """Step 19's fixture: one of each finding in project alpha, plus four stand-ins."""
    ids = Ids()
    card(root, "alpha", "windowless", "idle", timedelta(hours=30))
    card(root, "alpha", "idler", "idle", timedelta(hours=30))
    bind(root, "alpha", "idler", S_IDLE)
    card(root, "alpha", "stuck", "active", timedelta(hours=30))
    bind(root, "alpha", "stuck", S_BUSY)
    card(root, "alpha", "finished", "done", timedelta(hours=2))
    bind(root, "alpha", "finished", S_DONE)
    ids.decision = note(
        root, "alpha", "main", timedelta(days=3), "d001", needs="operator-decision", kind="question"
    )
    for i in range(50):
        # Ten of them are broadcasts, which count for main too.
        to = "all" if i % 5 == 0 else "main"
        note_id = note(
            root, "alpha", to, timedelta(hours=72, minutes=i), f"{0xA000 + i:04x}", needs="fyi"
        )
        if i == 49:
            ids.oldest_fyi = note_id
    note(root, "alpha", "main", timedelta(hours=1), "b001", needs="fyi")
    note(root, "alpha", "all", timedelta(hours=1), "b002", needs="fyi")
    ids.direct = note(
        root, "alpha", "main", timedelta(hours=1), "c001", needs="reply", kind="question"
    )
    for project in ("dev", "p1", "p2", "p3", "p4"):
        bind(root, project, "main", S_STAND)
    fake_status_sessions(
        tmp_path / "host",
        monkeypatch,
        [
            {"sessionId": S_IDLE, "status": "idle", "kind": "interactive"},
            {"sessionId": S_BUSY, "status": "busy", "kind": "interactive"},
            {"sessionId": S_DONE, "status": "idle", "kind": "interactive"},
        ],
    )
    return ids


def run(invoke: Invoke, root: Path, *args: str) -> Result:
    """``status`` through cli.main; the store snapshot must not change and no body leaks."""
    before = tree(root)
    result = invoke("status", *args)
    assert tree(root) == before
    assert CANARY not in result.stdout and CANARY not in result.stderr
    return result


def expected_rows(ids: Ids) -> list[dict[str, object]]:
    def row(
        kind: str, project: str, lane: str, evidence: str, age: int | None
    ) -> dict[str, object]:
        return {
            "action": sweep.SWEEP_ACTIONS[kind],
            "kind": kind,
            "project": project,
            "lane": lane,
            "evidence": evidence,
            "age_minutes": age,
        }

    return [
        row("memory-below-floor", "-", "-", "free 2.0 GiB; floor 4 GiB", None),
        row(
            "windowless-lane",
            "alpha",
            "windowless",
            "card idle; no live binding (no-binding)",
            1800,
        ),
        row("idle-lane", "alpha", "idler", "card idle; window wakeable", 1800),
        row("stale-card", "alpha", "stuck", "card active; older than 24h", 1800),
        row("done-window-open", "alpha", "finished", "card done; bound session still listed", 120),
        row("operator-decision-old", "alpha", "main", ids.decision, 3 * 24 * 60),
        row("backlog-bulk", "alpha", "main", f"50 notes; oldest {ids.oldest_fyi}", 72 * 60 + 49),
        row("backlog-direct", "alpha", "main", ids.direct, 60),
        row(
            "stand-ins-over-limit",
            "-",
            "main",
            "one session is main in 4 projects: p1, p2, p3, p4",
            None,
        ),
    ]


def test_sweep_json_has_one_row_per_finding_with_its_action(
    invoke: Invoke, store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = build_fixture(store_root, tmp_path, monkeypatch)
    result = run(invoke, store_root, "--all-projects", "--sweep", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert list(data)[-1] == status.SWEEP_KEY
    rows = data[status.SWEEP_KEY]
    assert rows == expected_rows(ids)
    assert [row["kind"] for row in rows] == list(sweep.SWEEP_KINDS)
    assert all(tuple(row) == status.JSON_KEYS["sweep"] for row in rows)
    # Session ids stay private; evidence names ids, counts and ages only.
    for session in (S_IDLE, S_BUSY, S_DONE, S_STAND):
        assert session not in result.stdout


EXPECTED_SWEEP_TEXT = (
    "sweep: 9 rows; read-only, nothing was changed\n"
    "  ACTION               KIND                   PROJECT  LANE        AGE  EVIDENCE\n"
    "  stop-process         memory-below-floor     -        -           -    "
    "free 2.0 GiB; floor 4 GiB\n"
    "  stand-in-close       windowless-lane        alpha    windowless  1d   "
    "card idle; no live binding (no-binding)\n"
    "  close-request        idle-lane              alpha    idler       1d   "
    "card idle; window wakeable\n"
    "  status-request       stale-card             alpha    stuck       1d   "
    "card active; older than 24h\n"
    "  safe-close           done-window-open       alpha    finished    2h   "
    "card done; bound session still listed\n"
    "  surface-to-operator  operator-decision-old  alpha    main        3d   "
    "20300107T120000Z-worker-to-main-d001\n"
    "  bulk-ack             backlog-bulk           alpha    main        3d   "
    "50 notes; oldest 20300107T111100Z-worker-to-main-a031\n"
    "  disposition          backlog-direct         alpha    main        1h   "
    "20300110T110000Z-worker-to-main-c001\n"
    "  open-project-main    stand-ins-over-limit   -        main        -    "
    "one session is main in 4 projects: p1, p2, p3, p4\n"
).splitlines()


def test_sweep_text_matches_the_pinned_table_after_the_status_view(
    invoke: Invoke, store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_fixture(store_root, tmp_path, monkeypatch)
    result = run(invoke, store_root, "--all-projects", "--sweep")
    assert result.returncode == 0, result.stderr
    lines = result.lines
    start = lines.index(EXPECTED_SWEEP_TEXT[0])
    assert lines[start : start + len(EXPECTED_SWEEP_TEXT)] == EXPECTED_SWEEP_TEXT
    # After the coordinator view's last line, before diagnostics and the summary.
    assert lines[start - 1].startswith("look here first: ")
    assert lines[start + len(EXPECTED_SWEEP_TEXT)].startswith("diagnostics: ")
    assert lines[-1].startswith("projects: ")
    compact = run(invoke, store_root, "--all-projects", "--sweep", "--coordinator-attention")
    lines = compact.lines
    start = lines.index(EXPECTED_SWEEP_TEXT[0])
    assert lines[start - 1].startswith("Full decisions, lanes, diagnostics")
    assert lines[start : start + len(EXPECTED_SWEEP_TEXT)] == EXPECTED_SWEEP_TEXT


@pytest.mark.parametrize(
    "extra", [(), ("--live",), ("--coordinator-attention",), ("--needs-attention",)]
)
def test_without_sweep_output_is_unchanged(
    invoke: Invoke,
    store_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: tuple[str, ...],
) -> None:
    """Additive: dropping the sweep key from --sweep JSON gives the plain JSON byte for
    byte, and the plain text is the --sweep text without the sweep block."""
    build_fixture(store_root, tmp_path, monkeypatch)
    plain = run(invoke, store_root, "--all-projects", "--json", *extra)
    swept = run(invoke, store_root, "--all-projects", "--json", "--sweep", *extra)
    assert status.SWEEP_KEY not in json.loads(plain.stdout)
    data = json.loads(swept.stdout)
    del data[status.SWEEP_KEY]
    assert json.dumps(data, indent=2, ensure_ascii=True) + "\n" == plain.stdout
    plain_text = run(invoke, store_root, "--all-projects", *extra).lines
    swept_text = run(invoke, store_root, "--all-projects", "--sweep", *extra).lines
    start = swept_text.index(EXPECTED_SWEEP_TEXT[0])
    del swept_text[start : start + len(EXPECTED_SWEEP_TEXT)]
    assert swept_text == plain_text
    assert not any(line.startswith("sweep: ") for line in plain_text)


def test_live_does_not_hide_the_done_window_row(
    invoke: Invoke, store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = build_fixture(store_root, tmp_path, monkeypatch)
    result = run(invoke, store_root, "--all-projects", "--live", "--sweep", "--json")
    assert json.loads(result.stdout)[status.SWEEP_KEY] == expected_rows(ids)


def test_a_project_main_sees_only_its_own_project_and_refusals_are_as_for_status(
    invoke: Invoke, store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_fixture(store_root, tmp_path, monkeypatch)
    card(store_root, "beta", "gone", "active", timedelta(hours=30))
    note(store_root, "beta", "main", timedelta(hours=1), "e001", needs="ack")
    bind(store_root, "alpha", "main", CALLER)
    monkeypatch.setenv(cli.CLAUDE_SESSION_ENV, CALLER)

    result = run(invoke, store_root, "--project", "alpha", "--sweep", "--json")
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)[status.SWEEP_KEY]
    assert {row["project"] for row in rows} == {"alpha", "-"}
    assert "stand-ins-over-limit" not in {row["kind"] for row in rows}
    assert "beta" not in result.stdout and "p1" not in result.stdout
    mine = run(invoke, store_root, "--mine", "--sweep", "--json")
    assert json.loads(mine.stdout)[status.SWEEP_KEY] == rows

    for args in (("--project", "beta"), ("--all-projects",)):
        plain = run(invoke, store_root, *args, "--json")
        swept = run(invoke, store_root, *args, "--json", "--sweep")
        assert swept.returncode == plain.returncode == cli.EXIT_USAGE
        assert swept.stdout == plain.stdout == ""
        assert swept.stderr == plain.stderr
        assert f"passing-notes: error: {status.STATUS_OUT_OF_SCOPE}: " in swept.stderr


def test_a_work_lane_gets_no_sweep_rows_and_the_workspace_main_gets_all(
    invoke: Invoke, store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = build_fixture(store_root, tmp_path, monkeypatch)
    card(store_root, "alpha", "helper", "active", timedelta(minutes=5))
    bind(store_root, "alpha", "helper", CALLER)
    monkeypatch.setenv(cli.CLAUDE_SESSION_ENV, CALLER)
    result = run(invoke, store_root, "--project", "alpha", "--sweep", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)[status.SWEEP_KEY] == []
    text = run(invoke, store_root, "--project", "alpha", "--sweep")
    assert "sweep: nothing to tidy observed; absence is not proof" in text.stdout

    monkeypatch.setenv(cli.CLAUDE_SESSION_ENV, S_STAND)  # also bound as dev/main
    result = run(invoke, store_root, "--all-projects", "--sweep", "--json")
    assert json.loads(result.stdout)[status.SWEEP_KEY] == expected_rows(ids)


def test_thresholds_floor_flag_and_unknown_memory(
    invoke: Invoke, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three stand-ins are within the limit; a 47 h fyi is not bulk; a 23 h decision is
    not old; free memory at or above the floor, or unknown, is no finding."""
    for project in ("q1", "q2", "q3"):
        bind(store_root, project, "main", S_STAND)
    note(store_root, "q1", "main", timedelta(hours=47), "f001", needs="fyi")
    note(store_root, "q1", "main", timedelta(hours=23), "f002", needs="operator-decision")
    card(store_root, "q1", "fresh", "idle", timedelta(hours=23))

    def kinds(*args: str) -> list[str]:
        result = run(invoke, store_root, "--all-projects", "--json", "--sweep", *args)
        assert result.returncode == 0, result.stderr
        return [row["kind"] for row in json.loads(result.stdout)[status.SWEEP_KEY]]

    assert kinds() == ["memory-below-floor"]
    assert kinds("--memory-floor-gib", "2") == []
    assert kinds("--memory-floor-gib", "2.5") == ["memory-below-floor"]
    assert kinds("--stale-after", "1") == ["memory-below-floor", "windowless-lane"]
    monkeypatch.setattr(status_resources, "free_memory_bytes", lambda: None)
    assert kinds("--memory-floor-gib", "4096") == []


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.", ".5", "1e3", "4097"])
def test_memory_floor_rejects_bad_values(invoke: Invoke, value: str) -> None:
    result = invoke("status", "--all-projects", "--sweep", "--memory-floor-gib", value)
    assert result.returncode == cli.EXIT_USAGE and result.stdout == ""
    assert "not a positive number of GiB" in result.stderr


def test_memory_floor_needs_sweep(invoke: Invoke, store_root: Path) -> None:
    result = run(invoke, store_root, "--all-projects", "--memory-floor-gib", "4")
    assert result.returncode == cli.EXIT_USAGE and result.stdout == ""
    assert "--memory-floor-gib needs --sweep" in result.stderr


def test_acked_and_answered_findings_drop_out(
    invoke: Invoke, store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = build_fixture(store_root, tmp_path, monkeypatch)
    store = Store(store_root)
    store.publish_ack("alpha", ids.direct, protocol.make_ack("main", NOW))
    store.publish_ack("alpha", ids.oldest_fyi, protocol.make_ack("main", NOW))
    store.publish_note(
        protocol.make_draft(
            project="alpha",
            from_lane="main",
            to="worker",
            subject="Synthetic decision answer",
            kind="answer",
            needs="fyi",
            reply_to=ids.decision,
        ),
        "Synthetic answer body.",
        now=lambda: NOW,
    )
    result = run(invoke, store_root, "--all-projects", "--sweep", "--json")
    rows = {row["kind"]: row for row in json.loads(result.stdout)[status.SWEEP_KEY]}
    assert "backlog-direct" not in rows and "operator-decision-old" not in rows
    assert rows["backlog-bulk"]["evidence"].startswith("49 notes; oldest ")


def test_status_uses_the_sweep_constants_themselves() -> None:
    """One source of truth: status re-exports sweep's objects, never copies."""
    assert status.SWEEP_ACTIONS is sweep.SWEEP_ACTIONS
    assert status.SWEEP_KINDS is sweep.SWEEP_KINDS
    assert status.MEMORY_FLOOR_DEFAULT_GIB is sweep.MEMORY_FLOOR_DEFAULT_GIB
    assert tuple(sweep.SWEEP_ACTIONS) == sweep.SWEEP_KINDS
    assert sweep.IDLE_STATE in protocol.CARD_STATES
    assert all(
        sweep.SweepRow(kind, "p", "l", "e", None).action is sweep.SWEEP_ACTIONS[kind]
        for kind in sweep.SWEEP_KINDS
    )
