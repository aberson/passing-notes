"""``passing-notes status``: caller scope, the D47 table, the D54 JSON, read-only."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import Invoke, child_env, note_text, place_ack, place_note, run_cli

from passing_notes import cli, protocol, status, wake
from passing_notes.store import Store

SESSION = "fb0c7c54-8df8-476c-b73a-8f1b8663e44c"
SECOND = "11111111-2222-4333-8444-555555555555"
OTHER = "0a0b0c0d-1111-4222-8333-944445555666"
NOW = datetime(2030, 1, 2, 12, 0, 0, tzinfo=UTC)
CANARY_BODY = "CANARY-BODY-9c2e"
CANARY_SUBJECT = "CANARY-SUBJECT-7f3a"
LONG_ITEM = "Waiting on review of a very long item description that is cut"
ODD_ITEM = "Idle caf" + chr(0xE9) + " " + chr(0x4E2D)

Snapshot = dict[str, tuple[bool, int, int]]


def at(text: str) -> datetime:
    return protocol.parse_utc(text, "updated_utc")


def put_card(
    root: Path,
    project: str,
    lane: str,
    state: str,
    item: str,
    updated: str,
    *,
    host: str = "claude",
    window: str | None = None,
) -> None:
    card = protocol.make_card(
        project=project,
        lane=lane,
        host=host,
        item=item,
        state=state,
        updated=at(updated),
        window=window,
    )
    Store(root).publish_card(card)


def put_note(root: Path, project: str, sender: str, to: str, minute: int, **fields: object) -> str:
    stamp = f"20300102T10{minute:02d}00Z"
    hex4 = f"{minute:04x}"
    text = note_text(
        stamp=stamp,
        created_utc=f"2030-01-02T10:{minute:02d}:00Z",
        sender=sender,
        recipient=to,
        hex4=hex4,
        project=project,
        **fields,  # type: ignore[arg-type]
    )
    note_id = f"{stamp}-{sender}-to-{to}-{hex4}"
    place_note(root, text, f"{note_id}.md", project)
    return note_id


def ack(root: Path, project: str, note_id: str, reader: str) -> None:
    data = protocol.render_ack(protocol.make_ack(reader, NOW)).encode("utf-8")
    place_ack(root, data, protocol.ack_file_name(note_id, reader), project)


def bind(root: Path, project: str, lane: str, session_id: str) -> None:
    wake.save_binding(
        Store(root),
        wake.Binding(
            project, lane, wake.HOST_CLAUDE, session_id, "default", "2030-01-01T12:00:00Z"
        ),
    )


def build_fixture(root: Path) -> None:
    """Projects dev, alpha and beta; beta has cards but no messages folder."""
    put_card(root, "dev", "main", "active", "Linking projects", "2030-01-02T11:56:00Z")
    put_note(root, "dev", "main", "operator", 1, needs="operator-decision", kind="question")

    put_card(root, "alpha", "main", "active", "Coordinating alpha", "2030-01-02T11:00:00Z")
    put_card(root, "alpha", "lane-x", "waiting", LONG_ITEM, "2029-12-31T12:00:00Z")
    put_card(root, "alpha", "lane-y", "idle", ODD_ITEM, "2030-01-02T09:00:00Z", host="codex")
    put_card(root, "alpha", "a-done", "done", "Shipped step 1", "2030-01-01T12:00:00Z")
    put_note(
        root,
        "alpha",
        "main",
        "lane-x",
        2,
        needs="reply",
        kind="question",
        subject=CANARY_SUBJECT,
        body=CANARY_BODY,
    )
    put_note(root, "alpha", "lane-x", "main", 3, needs="operator-decision", kind="question")
    to_all = put_note(root, "alpha", "lane-y", "all", 4, needs="fyi")
    ack(root, "alpha", to_all, "lane-x")
    first = put_note(root, "alpha", "main", "lane-y", 5, needs="ack")
    put_note(root, "alpha", "main", "lane-y", 6, needs="ack", extra=[("supersedes", first)])
    bind(root, "alpha", "main", OTHER)

    put_card(root, "beta", "x", "blocked", "Blocked on alpha", "2030-01-02T11:30:00Z")
    (root / "cards" / "Bad_Name").mkdir()


EXPECTED_TEXT = """\
PROJECT  LANE    STATE    HOST    WINDOW     UPDATED  OPEN  WORKING ON
alpha    main    active   claude  bound      1h       2/1r  Coordinating alpha
alpha    lane-x  waiting  claude  not-bound  2d       1/1r  Waiting on review of a very long item...
alpha    lane-y  idle     codex   not-bound  3h       1     Idle caf\\xe9 \\u4e2d
alpha    a-done  done     claude  not-bound  1d       1     Shipped step 1
operator: 1 decision
flags: alpha/lane-x stale, no-doorbell
beta     x       blocked  claude  not-bound  30m      0     Blocked on alpha
flags: beta/x no-doorbell
dev      main    active   claude  not-bound  4m       0     Linking projects
operator: 1 decision
flags: dev/main no-doorbell
diagnostic: cards/Bad_Name: ignored: not a valid project id
diagnostics: 1
projects: 3  lanes: 6  diagnostics: 1
"""


@pytest.fixture
def fixture_store(store_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    build_fixture(store_root)
    monkeypatch.setattr(cli, "_now_utc", lambda: NOW)
    return store_root


def tree(root: Path) -> Snapshot:
    """Every folder and file under ``root``: (is folder, size, mtime_ns) by relative path."""
    found: Snapshot = {}
    if not root.exists():
        return found
    for folder, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            path = Path(folder) / name
            info = path.lstat()
            found[path.relative_to(root).as_posix()] = (
                path.is_dir(),
                info.st_size,
                info.st_mtime_ns,
            )
    return found


def as_session(monkeypatch: pytest.MonkeyPatch, session_id: str | None) -> None:
    if session_id is None:
        monkeypatch.delenv(cli.CLAUDE_SESSION_ENV, raising=False)
    else:
        monkeypatch.setenv(cli.CLAUDE_SESSION_ENV, session_id)


def status_run(invoke: Invoke, root: Path, *args: str) -> tuple[int, str, str]:
    """Run ``status`` and prove the store is unchanged by it."""
    before = tree(root)
    result = invoke("status", *args)
    assert tree(root) == before
    return result.returncode, result.stdout, result.stderr


def projects_in(stdout: str) -> set[str]:
    rows = stdout.splitlines()[1:]
    return {line.split()[0] for line in rows if line.split() and line.split()[1] in LANES}


LANES = {"main", "lane-x", "lane-y", "a-done", "x"}


def assert_allowed(invoke: Invoke, root: Path, args: tuple[str, ...], shown: set[str]) -> None:
    code, out, err = status_run(invoke, root, *args)
    assert (code, err) == (0, "")
    assert projects_in(out) == shown


def assert_refused(invoke: Invoke, root: Path, args: tuple[str, ...]) -> None:
    code, out, err = status_run(invoke, root, *args)
    assert code == cli.EXIT_USAGE
    assert out == ""
    lines = err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"passing-notes: error: {status.STATUS_OUT_OF_SCOPE}: ")
    assert status.TOPOLOGY_REF in lines[0]


ALL = ("--all-projects",)
ALPHA = ("--project", "alpha")
BETA = ("--project", "beta")
DEV = ("--project", "dev")
EVERY = {"dev", "alpha", "beta"}


# --- Scope ------------------------------------------------------------------------------


def test_the_workspace_main_sees_every_project(
    invoke: Invoke, fixture_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind(fixture_store, "dev", "main", SESSION)
    as_session(monkeypatch, SESSION)
    assert_allowed(invoke, fixture_store, ALL, EVERY)
    assert_allowed(invoke, fixture_store, BETA, {"beta"})


@pytest.mark.parametrize("lane", ["main", "lane-x"])
def test_a_project_lane_sees_only_its_project(
    invoke: Invoke, fixture_store: Path, monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    bind(fixture_store, "alpha", lane, SESSION)
    as_session(monkeypatch, SESSION)
    assert_allowed(invoke, fixture_store, ALPHA, {"alpha"})
    assert_refused(invoke, fixture_store, ALL)
    assert_refused(invoke, fixture_store, BETA)
    assert_refused(invoke, fixture_store, DEV)


def test_several_bindings_see_the_union(
    invoke: Invoke, fixture_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind(fixture_store, "dev", "main", SESSION)
    bind(fixture_store, "alpha", "main", SESSION)
    as_session(monkeypatch, SESSION)
    assert_allowed(invoke, fixture_store, ALL, EVERY)

    bind(fixture_store, "alpha", "main", SECOND)
    bind(fixture_store, "beta", "x", SECOND)
    as_session(monkeypatch, SECOND)
    assert_allowed(invoke, fixture_store, BETA, {"beta"})
    assert_allowed(invoke, fixture_store, ALPHA, {"alpha"})
    assert_refused(invoke, fixture_store, ALL)
    assert_refused(invoke, fixture_store, DEV)


@pytest.mark.parametrize("session_id", [None, "", "not-a-session-id", SESSION])
def test_an_unbound_caller_may_ask_for_any_project_or_all(
    invoke: Invoke, fixture_store: Path, monkeypatch: pytest.MonkeyPatch, session_id: str | None
) -> None:
    # SESSION has no binding in the fixture, so it is unbound too.
    as_session(monkeypatch, session_id)
    assert_allowed(invoke, fixture_store, ALL, EVERY)
    for project in ("dev", "alpha", "beta"):
        assert_allowed(invoke, fixture_store, ("--project", project), {project})


@pytest.mark.parametrize("stale", ["done", "other-session", "other-host"])
def test_a_stale_binding_grants_no_scope(
    invoke: Invoke, fixture_store: Path, monkeypatch: pytest.MonkeyPatch, stale: str
) -> None:
    # Live as alpha/main, stale as dev/main: the stale binding must not open every project.
    bind(fixture_store, "alpha", "main", SESSION)
    bind(fixture_store, "dev", "main", SESSION)
    updated = "2030-01-02T11:56:00Z"
    if stale == "done":
        put_card(fixture_store, "dev", "main", "done", "Closed", updated)
    elif stale == "other-session":
        put_card(fixture_store, "dev", "main", "active", "Moved", updated, window=OTHER)
    else:
        put_card(fixture_store, "dev", "main", "active", "Moved", updated, host="codex")
    store = Store(fixture_store)
    dev_binding = wake.load_binding(store, "dev", "main")
    assert wake.binding_stale_reason(store, "dev", "main", dev_binding) is not None
    as_session(monkeypatch, SESSION)
    assert_refused(invoke, fixture_store, ALL)
    assert_refused(invoke, fixture_store, DEV)
    assert_allowed(invoke, fixture_store, ALPHA, {"alpha"})


def test_the_only_binding_stale_leaves_the_caller_unbound(
    invoke: Invoke, fixture_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind(fixture_store, "alpha", "main", SESSION)
    put_card(fixture_store, "alpha", "main", "done", "Closed", "2030-01-02T11:00:00Z")
    as_session(monkeypatch, SESSION)
    code, out, _ = status_run(invoke, fixture_store, "--json", "--project", "beta")
    assert code == 0
    assert json.loads(out)["scope"]["caller"] == []


def test_the_json_scope_names_the_live_bindings(
    invoke: Invoke, fixture_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind(fixture_store, "dev", "main", SESSION)
    bind(fixture_store, "alpha", "lane-x", SESSION)
    as_session(monkeypatch, SESSION)
    code, out, _ = status_run(invoke, fixture_store, "--json", "--all-projects")
    assert code == 0
    scope = json.loads(out)["scope"]
    assert scope == {"caller": ["alpha/lane-x", "dev/main"], "projects": ["alpha", "beta", "dev"]}


@pytest.mark.parametrize(
    "args",
    [(), ("--project", "alpha", "--all-projects"), ("--json",), ("--stale-after", "3")],
)
def test_exactly_one_of_project_and_all_projects(
    invoke: Invoke, fixture_store: Path, args: tuple[str, ...]
) -> None:
    code, out, err = status_run(invoke, fixture_store, *args)
    assert code == cli.EXIT_USAGE and out == ""
    assert err.startswith(f"passing-notes: error: {cli.INVALID_ARGUMENT}: ")
    assert len(err.splitlines()) == 1


@pytest.mark.parametrize(
    "args", [("--project", "Bad_Name"), ("--all-projects", "--stale-after", "0")]
)
def test_bad_values_are_usage_errors(
    invoke: Invoke, fixture_store: Path, args: tuple[str, ...]
) -> None:
    code, out, _ = status_run(invoke, fixture_store, *args)
    assert code == cli.EXIT_USAGE and out == ""


# --- Text and JSON ----------------------------------------------------------------------


def test_text_output_matches_the_pinned_table(invoke: Invoke, fixture_store: Path) -> None:
    code, out, err = status_run(invoke, fixture_store, "--all-projects")
    assert (code, err) == (0, "")
    assert out == EXPECTED_TEXT
    assert out.isascii()


def test_one_project_prints_its_block_only(invoke: Invoke, fixture_store: Path) -> None:
    code, out, _ = status_run(invoke, fixture_store, "--project", "beta")
    assert code == 0
    assert out == (
        "PROJECT  LANE  STATE    HOST    WINDOW     UPDATED  OPEN  WORKING ON\n"
        "beta     x     blocked  claude  not-bound  30m      0     Blocked on alpha\n"
        "flags: beta/x no-doorbell\n"
        "diagnostics: 0\n"
        "projects: 1  lanes: 1  diagnostics: 0\n"
    )


def test_a_project_with_no_cards_still_has_a_block(invoke: Invoke, store_root: Path) -> None:
    code, out, _ = status_run(invoke, store_root, "--project", "empty")
    assert code == 0
    assert out.splitlines()[1:] == [
        "empty: no lane cards",
        "diagnostics: 0",
        "projects: 1  lanes: 0  diagnostics: 0",
    ]


def test_stale_after_changes_the_stale_flag(invoke: Invoke, fixture_store: Path) -> None:
    code, out, _ = status_run(invoke, fixture_store, "--project", "beta", "--stale-after", "100")
    assert code == 0 and "stale" not in out
    code, out, _ = status_run(invoke, fixture_store, "--project", "alpha", "--stale-after", "100")
    assert "flags: alpha/lane-x no-doorbell" in out.splitlines()
    huge = str(10**30)
    code, out, _ = status_run(invoke, fixture_store, "--project", "alpha", "--stale-after", huge)
    assert code == 0 and "stale," not in out


def test_json_carries_every_d54_key(invoke: Invoke, fixture_store: Path) -> None:
    code, out, err = status_run(invoke, fixture_store, "--all-projects", "--json")
    assert (code, err) == (0, "")
    assert out.isascii()
    data = json.loads(out)
    keys = status.JSON_KEYS
    assert tuple(data) == keys["status"]
    assert data["schema"] == status.SCHEMA == "passing-notes.status/1"
    assert data["generated_utc"] == "2030-01-02T12:00:00Z"
    assert tuple(data["scope"]) == keys["scope"]
    assert data["scope"] == {"caller": [], "projects": ["alpha", "beta", "dev"]}
    assert tuple(data["diagnostics"]) == keys["diagnostics"]
    assert data["diagnostics"] == {
        "shown": ["cards/Bad_Name: ignored: not a valid project id"],
        "total": 1,
    }
    for block in data["projects"]:
        assert tuple(block) == keys["project"]
        for lane in block["lanes"]:
            assert tuple(lane) == keys["lane"]
    alpha = data["projects"][0]
    assert (alpha["project"], alpha["done_count"], alpha["operator_decisions"]) == ("alpha", 1, 1)
    assert [lane["lane"] for lane in alpha["lanes"]] == ["main", "lane-x", "lane-y", "a-done"]
    lane_x = alpha["lanes"][1]
    assert lane_x == {
        "lane": "lane-x",
        "host": "claude",
        "state": "waiting",
        "item": LONG_ITEM,
        "updated_utc": "2029-12-31T12:00:00Z",
        "age_minutes": 2 * 24 * 60,
        "window": "not-bound",
        "open": 1,
        "open_unblocking": 1,
        "flags": ["stale", "no-doorbell"],
    }
    assert alpha["lanes"][2]["item"] == ODD_ITEM
    assert alpha["lanes"][0]["window"] == "bound"


def test_diagnostics_are_capped_in_both_outputs(invoke: Invoke, fixture_store: Path) -> None:
    for n in range(protocol.DIAGNOSTIC_LINES + 5):
        (fixture_store / "cards" / f"Bad{n}").mkdir()
    total = protocol.DIAGNOSTIC_LINES + 6
    _, out, _ = status_run(invoke, fixture_store, "--all-projects")
    lines = out.splitlines()
    assert sum(line.startswith("diagnostic: ") for line in lines) == protocol.DIAGNOSTIC_LINES
    assert lines[-1] == f"projects: 3  lanes: 6  diagnostics: {total}"
    _, out, _ = status_run(invoke, fixture_store, "--all-projects", "--json")
    diagnostics = json.loads(out)["diagnostics"]
    assert len(diagnostics["shown"]) == protocol.DIAGNOSTIC_LINES
    assert diagnostics["total"] == total


@pytest.mark.parametrize("args", [("--all-projects",), ("--all-projects", "--json")])
def test_no_note_body_or_subject_reaches_the_output(
    invoke: Invoke, fixture_store: Path, monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]
) -> None:
    for session_id in (None, SESSION):
        if session_id is not None:
            bind(fixture_store, "dev", "main", session_id)
        as_session(monkeypatch, session_id)
        code, out, err = status_run(invoke, fixture_store, *args)
        assert code == 0
        for canary in (CANARY_BODY, CANARY_SUBJECT):
            assert canary not in out and canary not in err
    report = status.collect(
        Store(fixture_store), status.Scope(()), None, now=NOW, stale_after_hours=24
    )
    assert CANARY_BODY not in repr(report) and CANARY_SUBJECT not in repr(report)


# --- Read-only --------------------------------------------------------------------------


@pytest.mark.parametrize("session_id", [None, SESSION])
@pytest.mark.parametrize("args", [ALL, ALPHA, ("--json", "--all-projects")])
def test_a_missing_store_root_stays_missing(
    invoke: Invoke,
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_id: str | None,
    args: tuple[str, ...],
) -> None:
    as_session(monkeypatch, session_id)
    assert not store_root.exists()
    result = invoke("status", *args)
    assert result.returncode == 0
    assert not store_root.exists()
    assert result.stdout.splitlines()[-1].startswith(("projects: ", "}"))


def test_a_missing_wake_folder_stays_missing(
    invoke: Invoke, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_card(store_root, "alpha", "main", "active", "Working", "2030-01-02T11:00:00Z")
    as_session(monkeypatch, SESSION)
    before = tree(store_root)
    for args in (ALL, ALPHA, ("--json", "--project", "alpha")):
        assert invoke("status", *args).returncode == 0
    assert not (store_root / wake.WAKE_DIR).exists()
    assert tree(store_root) == before


# --- Neighbours -------------------------------------------------------------------------


CARD_SHOW = (
    "lane-x\tclaude\twaiting\t" + LONG_ITEM + "\t2029-12-31T12:00:00Z\n"
    "lane-y\tcodex\tidle\t" + ODD_ITEM + "\t2030-01-02T09:00:00Z\n"
    "main\tclaude\tactive\tCoordinating alpha\t2030-01-02T11:00:00Z\n"
    "diagnostics: 0\n"
)
INBOX = (
    "20300102T100300Z-lane-x-to-main-0003\tquestion\toperator-decision\tlane-x\t"
    "Synthetic subject\n"
    "20300102T100400Z-lane-y-to-all-0004\tfyi\tfyi\tlane-y\tSynthetic subject\n"
    "diagnostics: 0\n"
)


def test_card_show_and_inbox_output_is_unchanged(invoke: Invoke, fixture_store: Path) -> None:
    def outputs() -> tuple[str, str]:
        shown = invoke("card", "show", "--project", "alpha").stdout
        inbox = invoke("inbox", "--project", "alpha", "--lane", "main").stdout
        return shown, inbox

    before = outputs()
    shown, inbox = before
    assert shown == "a-done\tclaude\tdone\tShipped step 1\t2030-01-01T12:00:00Z\n" + CARD_SHOW
    assert inbox == INBOX
    status_run(invoke, fixture_store, "--all-projects")
    status_run(invoke, fixture_store, "--all-projects", "--json")
    assert outputs() == before


def test_the_console_script_prints_non_cp1252_items_on_a_cp1252_stream(
    store_root: Path,
) -> None:
    item = "Review " + chr(0x4E2D) + chr(0x6587) + " " + chr(0x416) + " notes"
    put_card(store_root, "alpha", "main", "active", item, "2030-01-02T11:00:00Z")
    env = child_env(store_root, {"PYTHONIOENCODING": "cp1252"})
    env.pop(cli.CLAUDE_SESSION_ENV, None)
    text = run_cli(["status", "--project", "alpha"], root=store_root, env=env)
    assert (text.returncode, text.stderr) == (0, "")
    assert "Review \\u4e2d\\u6587 \\u0416 notes" in text.stdout
    data = run_cli(["status", "--project", "alpha", "--json"], root=store_root, env=env)
    assert (data.returncode, data.stderr) == (0, "")
    assert json.loads(data.stdout)["projects"][0]["lanes"][0]["item"] == item


def test_ascii_cell_never_splits_an_escape() -> None:
    text = "a" * 36 + chr(0x4E2D) + "tail"
    cell = status.ascii_cell(text, 40)
    assert cell == "a" * 36 + "..."
    assert status.ascii_cell("short", 40) == "short"
    assert len(status.ascii_cell("b" * 60, 40)) == 40
