"""The Claude Stop hook: ``hook stop`` (D41, D42, D46) and ``hook install`` (D43).

The acceptance tests drive the real console script with the Stop-hook JSON on standard
input exactly as the Claude host writes it. Every settings file is a temporary one:
CLAUDE_CONFIG_DIR, HOME and USERPROFILE point into a temporary folder for every test,
so neither a test nor a child process can reach the real user settings.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Final

import pytest
from conftest import (
    PROJECT,
    ZWSP,
    Invoke,
    Result,
    card_set_args,
    child_env,
    console_script,
    note_id,
    note_text,
    place_note,
    run_cli,
)

from passing_notes import cli, hook, wake
from passing_notes.store import Store

SESSION = "fb0c7c54-8df8-476c-b73a-8f1b8663e44c"
OTHER_SESSION = "0a0b0c0d-1111-4222-8333-944445555666"
OTHER_PROJECT = "other-project"
SECRET = "SECRET-BODY-TEXT-must-never-print"


@pytest.fixture(autouse=True)
def no_real_settings(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """The user settings folder, and home, for this test: a temporary folder."""
    home = tmp_path_factory.mktemp("home")
    config = home / ".claude"
    config.mkdir()
    monkeypatch.setenv(wake.CLAUDE_CONFIG_ENV, str(config))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return config


def stop_stdin(session_id: str = SESSION, *, active: bool = False, **extra: object) -> bytes:
    """The Stop-hook stdin JSON as the host writes it (hooks reference, Stop input)."""
    payload: dict[str, object] = {
        "session_id": session_id,
        "transcript_path": "C:/Users/example/.claude/projects/example/transcript.jsonl",
        "cwd": "C:/work/example-project",
        "permission_mode": "default",
        "hook_event_name": "Stop",
        "stop_hook_active": active,
    }
    payload.update(extra)
    return json.dumps(payload).encode("utf-8")


def bind(root: Path, lane: str = "main", project: str = PROJECT, session_id: str = SESSION) -> None:
    wake.save_binding(
        Store(root),
        wake.Binding(
            project, lane, wake.HOST_CLAUDE, session_id, "default", "2030-01-01T12:00:00Z"
        ),
    )


def put(
    root: Path,
    *,
    stamp: str,
    hex4: str,
    kind: str,
    needs: str,
    recipient: str = "main",
    sender: str = "lane-a",
    project: str = PROJECT,
    subject: str = "Synthetic subject",
) -> str:
    s = stamp
    text = note_text(
        stamp=stamp,
        created_utc=f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[9:11]}:{s[11:13]}:{s[13:15]}Z",
        sender=sender,
        recipient=recipient,
        hex4=hex4,
        kind=kind,
        needs=needs,
        project=project,
        subject=subject,
        body=SECRET,
    )
    nid = note_id(stamp, sender, recipient, hex4)
    place_note(root, text, f"{nid}.md", project)
    return nid


def surfaced(root: Path, project: str = PROJECT, session_id: str = SESSION) -> list[str]:
    path = root / "wake" / project / "surfaced" / f"{session_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = data["note_ids"]
    assert isinstance(ids, list)
    return [str(i) for i in ids]


def reason_lines(result: Result) -> list[str]:
    assert result.returncode == 0 and result.stderr == ""
    assert len(result.lines) == 1, result.stdout
    reply = json.loads(result.lines[0])
    assert set(reply) == {"decision", "reason"} and reply["decision"] == "block"
    reason = reply["reason"]
    assert isinstance(reason, str)
    lines = reason.split("\n")
    assert lines[-1] == hook.REASON_TAIL
    return lines[:-1]


def run_stop(root: Path, stdin: bytes) -> Result:
    return run_cli(["hook", "stop"], root=root, stdin=stdin)


# --- hook stop through the console script -------------------------------------------


def test_stop_blocks_once_for_the_decision_note_through_the_console_script(
    store_root: Path,
) -> None:
    """D41/D42: a session bound as main, one operator-decision question and one fyi:
    the block reply names only the question, with no body, and records it surfaced;
    a second stop prints nothing."""
    bind(store_root)
    asked = put(
        store_root,
        stamp="20300101T120000Z",
        hex4="1a1a",
        kind="question",
        needs="operator-decision",
    )
    fyi = put(store_root, stamp="20300101T120100Z", hex4="2b2b", kind="fyi", needs="fyi")

    first = run_stop(store_root, stop_stdin())

    assert reason_lines(first) == [
        f"{asked} question needs operator-decision from lane-a: Synthetic subject"
    ]
    listed = run_cli(["inbox", "--project", PROJECT, "--lane", "main"], root=store_root)
    assert asked in listed.stdout and fyi in listed.stdout  # both notes are valid
    assert fyi not in first.stdout and SECRET not in first.stdout
    assert first.stdout.isascii()
    assert surfaced(store_root) == [asked]

    second = run_stop(store_root, stop_stdin())
    assert (second.returncode, second.stdout, second.stderr) == (0, "", "")
    assert surfaced(store_root) == [asked]


def test_docs_cite_the_hosts_stop_decision_contract() -> None:
    """D42: the block reply's shape (checked by ``reason_lines``) is the host's documented
    Stop decision control, and docs/cli.md cites it."""
    doc = (Path(__file__).resolve().parents[1] / "docs" / "cli.md").read_text(encoding="utf-8")
    assert "https://code.claude.com/docs/en/hooks#stop-decision-control" in doc
    assert '{"decision": "block", "reason": "<text>"}' in doc


QUIET_STDIN = {
    "unbound session": stop_stdin(OTHER_SESSION),
    "stop_hook_active": stop_stdin(active=True),
    "empty stdin": b"",
    "not JSON": b"{not json",
    "a JSON list": b"[]",
    "session id not a string": json.dumps({"session_id": 5, "stop_hook_active": False}).encode(),
    "invalid session id": stop_stdin("..\\..\\escape"),
    "upper-case session id": stop_stdin(SESSION.upper()),
    "stop_hook_active not a boolean": stop_stdin(stop_hook_active="false"),
    "another hook event": stop_stdin(hook_event_name="PreToolUse"),
    "not UTF-8": b"\xff\xfe{",
    "nested too deeply": b'{"session_id": ' + b"[" * 100_000 + b"]" * 100_000 + b"}",
}


@pytest.mark.parametrize("stdin", QUIET_STDIN.values(), ids=QUIET_STDIN.keys())
def test_stop_is_quiet_for_unbound_active_or_malformed_input(
    store_root: Path, stdin: bytes
) -> None:
    """D41/D46: unbound, already continuing, or not the host's Stop shape: exit 0, no
    output, and nothing recorded, so the note still surfaces at the next real stop."""
    bind(store_root)
    asked = put(store_root, stamp="20300101T120000Z", hex4="1a1a", kind="question", needs="reply")
    result = run_stop(store_root, stdin)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert not (store_root / "wake" / PROJECT / "surfaced").exists()
    later = reason_lines(run_stop(store_root, stop_stdin()))
    assert [line.split(" ")[0] for line in later] == [asked]


def test_stop_is_quiet_for_an_empty_inbox(store_root: Path) -> None:
    bind(store_root)
    result = run_stop(store_root, stop_stdin())
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def _root_is_a_file(root: Path) -> dict[str, str]:
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_bytes(b"not a folder")
    return {}


def _wake_is_a_file(root: Path) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "wake").write_bytes(b"not a folder")
    return {}


def _relative_root(root: Path) -> dict[str, str]:
    return {"PASSING_NOTES_ROOT": "relative\\store"}


def _root_in_a_repository(root: Path) -> dict[str, str]:
    # Only resolved and refused (store-in-repo), never created or written.
    inside = Path(__file__).resolve().parents[1] / "not-a-store"
    return {"PASSING_NOTES_ROOT": str(inside)}


def _damaged_binding_project(root: Path) -> dict[str, str]:
    bind(root)
    messages = root / "messages" / PROJECT
    messages.mkdir(parents=True)
    (messages / "x.md").write_bytes(b"\x00garbage")
    (root / "acks").mkdir()
    (root / "acks" / PROJECT).write_bytes(b"acks should be a folder")
    return {}


UNUSABLE = {
    "root is a file": _root_is_a_file,
    "wake is a file": _wake_is_a_file,
    "relative root": _relative_root,
    "root inside a repository": _root_in_a_repository,
    "acks folder is a file": _damaged_binding_project,
}


@pytest.mark.parametrize("make", UNUSABLE.values(), ids=UNUSABLE.keys())
def test_stop_is_quiet_when_the_store_is_unusable(store_root: Path, make: object) -> None:
    """D46: a store in an unusable state exits 0 with no output, never an error line."""
    assert callable(make)
    extra = make(store_root)
    result = run_cli(
        ["hook", "stop"], root=store_root, stdin=stop_stdin(), env=child_env(store_root, extra)
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert not (Path(__file__).resolve().parents[1] / "not-a-store").exists()


# --- hook stop selection, in process -------------------------------------------------


def test_every_binding_of_the_session_merges_oldest_first_capped_at_five(
    store_root: Path, invoke: Invoke
) -> None:
    """One session bound to lanes in two projects: their unblocking notes are merged
    oldest first, at most 5 per stop; the rest surface at the next stop."""
    bind(store_root, "main")
    bind(store_root, "maintainer", OTHER_PROJECT)
    ids = []
    for minute in range(4):
        ids.append(
            put(
                store_root,
                stamp=f"20300101T12{minute:02d}00Z",
                hex4=f"a{minute}a{minute}",
                kind="question",
                needs="reply",
            )
        )
        ids.append(
            put(
                store_root,
                stamp=f"20300101T12{minute:02d}30Z",
                hex4=f"b{minute}b{minute}",
                kind="handoff",
                needs="ack",
                recipient="maintainer",
                project=OTHER_PROJECT,
            )
        )
    first = invoke("hook", "stop", stdin=stop_stdin())
    shown = [line.split(" ")[0] for line in reason_lines(first)]
    assert shown == sorted(ids)[:5]
    second = invoke("hook", "stop", stdin=stop_stdin())
    assert [line.split(" ")[0] for line in reason_lines(second)] == sorted(ids)[5:]
    assert invoke("hook", "stop", stdin=stop_stdin()).stdout == ""
    assert sorted(surfaced(store_root) + surfaced(store_root, OTHER_PROJECT)) == sorted(ids)


def test_only_notes_that_may_ring_and_are_unread_surface(store_root: Path, invoke: Invoke) -> None:
    """The ring rule (wake.ring_reason) decides: needs reply or operator-decision, a
    handoff, an operator relay whatever its needs, a to-all question, and the answer
    to a note that needed a reply. Not: fyi, ack-only, acknowledged notes or notes to
    another lane."""
    bind(store_root)
    want = [
        put(store_root, stamp="20300101T120000Z", hex4="0001", kind="question", needs="reply"),
        put(store_root, stamp="20300101T120001Z", hex4="0002", kind="handoff", needs="ack"),
        put(store_root, stamp="20300101T120002Z", hex4="0003", kind="operator-relay", needs="ack"),
        put(
            store_root,
            stamp="20300101T120003Z",
            hex4="0004",
            kind="question",
            needs="reply",
            recipient="all",
        ),
    ]
    put(store_root, stamp="20300101T120004Z", hex4="0005", kind="fyi", needs="fyi")
    put(store_root, stamp="20300101T120005Z", hex4="0006", kind="answer", needs="ack")
    put(
        store_root,
        stamp="20300101T120006Z",
        hex4="0007",
        kind="question",
        needs="reply",
        recipient="lane-c",
    )
    acked = put(store_root, stamp="20300101T120007Z", hex4="0008", kind="question", needs="reply")
    assert invoke("ack", "--project", PROJECT, "--lane", "main", acked).returncode == 0
    result = invoke("hook", "stop", stdin=stop_stdin())
    assert [line.split(" ")[0] for line in reason_lines(result)] == want


def test_the_answer_to_a_note_that_needed_a_reply_surfaces(
    store_root: Path, invoke: Invoke
) -> None:
    bind(store_root)
    asked = note_id("20300101T115900Z", "main", "lane-a", "9999")
    place_note(
        store_root,
        note_text(
            stamp="20300101T115900Z",
            sender="main",
            recipient="lane-a",
            hex4="9999",
            kind="question",
            needs="reply",
        ),
        f"{asked}.md",
    )
    text = note_text(
        stamp="20300101T120000Z", recipient="main", hex4="1a1a", kind="answer", needs="ack"
    ).replace("reply_to: none", f"reply_to: {asked}")
    answer = note_id("20300101T120000Z", "lane-a", "main", "1a1a")
    place_note(store_root, text, f"{answer}.md")
    result = invoke("hook", "stop", stdin=stop_stdin())
    assert [line.split(" ")[0] for line in reason_lines(result)] == [answer]


def test_a_lane_whose_card_is_done_is_not_surfaced(store_root: Path, invoke: Invoke) -> None:
    bind(store_root)
    put(store_root, stamp="20300101T120000Z", hex4="1a1a", kind="question", needs="reply")
    assert invoke(*card_set_args("main", state="done")).returncode == 0
    assert invoke("hook", "stop", stdin=stop_stdin()).stdout == ""
    assert invoke(*card_set_args("main", state="active")).returncode == 0
    assert invoke("hook", "stop", stdin=stop_stdin()).stdout != ""


def test_a_card_naming_another_window_makes_the_binding_stale(
    store_root: Path, invoke: Invoke
) -> None:
    """The card checks ``wake send`` makes: a card naming another Claude session, or
    another host, means this binding no longer routes the lane's notes to this window."""
    bind(store_root)
    put(store_root, stamp="20300101T120000Z", hex4="1a1a", kind="question", needs="reply")
    assert invoke(*card_set_args("main", host="codex")).returncode == 0
    assert invoke("hook", "stop", stdin=stop_stdin()).stdout == ""
    moved = card_set_args("main", "Synthetic item", "--window", OTHER_SESSION)
    assert invoke(*moved).returncode == 0
    assert invoke("hook", "stop", stdin=stop_stdin()).stdout == ""
    here = card_set_args("main", "Synthetic item", "--window", SESSION)
    assert invoke(*here).returncode == 0
    assert invoke("hook", "stop", stdin=stop_stdin()).stdout != ""


class NoSessionsHost:
    """A host that must never be asked: a stale binding is refused before any session lookup."""

    def list_sessions(self) -> list[wake.Session]:
        raise AssertionError("a stale binding reached the session lookup")

    def send(self, address: str, text: str, permission_mode: str) -> wake.SendResult:
        raise AssertionError("a stale binding was rung")


def test_the_hook_and_wake_send_share_one_staleness_rule(
    store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One owner (``wake.binding_stale_reason``): replace it, and both the Stop hook and
    ``wake send`` follow, the hook surfacing nothing and send refusing with its reason."""
    bind(store_root)
    nid = put(store_root, stamp="20300101T120000Z", hex4="1a1a", kind="question", needs="reply")
    assert hook.stop_reply(stop_stdin().decode("utf-8")) is not None  # live by default
    seen: list[tuple[str, str, wake.Binding]] = []

    def stale(store: Store, project: str, lane: str, binding: wake.Binding) -> str | None:
        seen.append((project, lane, binding))
        return "synthetic stale reason"

    monkeypatch.setattr(wake, "binding_stale_reason", stale)
    # Forget the first surfacing, so only the staleness rule can keep the note back.
    (store_root / "wake" / PROJECT / "surfaced" / f"{SESSION}.json").unlink()
    assert hook.stop_reply(stop_stdin().decode("utf-8")) is None
    with pytest.raises(wake.WakeError) as refused:
        wake.send(Store(store_root), NoSessionsHost(), PROJECT, "main", nid)
    assert refused.value.code == wake.WAKE_SESSION_UNAVAILABLE
    assert refused.value.message == "synthetic stale reason"
    binding = wake.load_binding(Store(store_root), PROJECT, "main")
    assert seen == [(PROJECT, "main", binding), (PROJECT, "main", binding)]


def test_a_codex_binding_with_the_same_id_is_not_this_window(
    store_root: Path, invoke: Invoke
) -> None:
    wake.save_binding(
        Store(store_root),
        wake.Binding(PROJECT, "main", wake.HOST_CODEX, SESSION, "queue", "2030-01-01T12:00:00Z"),
    )
    put(store_root, stamp="20300101T120000Z", hex4="1a1a", kind="question", needs="reply")
    assert invoke("hook", "stop", stdin=stop_stdin()).stdout == ""


def test_an_unbound_run_scans_no_notes(store_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cost of an unbound window: the binding listing only, never an inbox scan.
    ``stop_reply`` is not fail-open, so a scan would raise here."""
    bind(store_root, session_id=OTHER_SESSION)
    put(store_root, stamp="20300101T120000Z", hex4="1a1a", kind="question", needs="reply")

    def no_scan(*_: object, **__: object) -> object:
        raise AssertionError("an unbound run scanned the store")

    monkeypatch.setattr(Store, "inbox", no_scan)
    monkeypatch.setattr(Store, "scan_notes", no_scan)
    assert hook.stop_reply(stop_stdin().decode("utf-8")) is None


def test_reason_lines_are_escaped_like_inbox(store_root: Path) -> None:
    """A valid note cannot hold an unsafe character, but the reason line escapes one
    anyway, with the rule every inbox line is printed by."""
    nid = put(store_root, stamp="20300101T120000Z", hex4="1a1a", kind="question", needs="reply")
    note = dataclasses.replace(Store.open().load_note(PROJECT, nid), subject=f"Hidden{ZWSP}text")
    assert hook.reason_line(note) == f"{nid} question needs reply from lane-a: Hidden\\u200btext"
    reply = hook.block_reply([note])
    assert ZWSP not in reply and reply.isascii()


def test_the_surfaced_list_is_pruned_to_unread_notes(store_root: Path, invoke: Invoke) -> None:
    bind(store_root)
    first = put(store_root, stamp="20300101T120000Z", hex4="1a1a", kind="question", needs="reply")
    invoke("hook", "stop", stdin=stop_stdin())
    assert invoke("ack", "--project", PROJECT, "--lane", "main", first).returncode == 0
    later = put(store_root, stamp="20300101T120100Z", hex4="2b2b", kind="question", needs="reply")
    invoke("hook", "stop", stdin=stop_stdin())
    assert surfaced(store_root) == [later]


def test_a_damaged_surfaced_list_surfaces_again_once(store_root: Path, invoke: Invoke) -> None:
    bind(store_root)
    asked = put(store_root, stamp="20300101T120000Z", hex4="1a1a", kind="question", needs="reply")
    invoke("hook", "stop", stdin=stop_stdin())
    path = store_root / "wake" / PROJECT / "surfaced" / f"{SESSION}.json"
    path.write_bytes(b"{damaged")
    assert asked in invoke("hook", "stop", stdin=stop_stdin()).stdout
    assert invoke("hook", "stop", stdin=stop_stdin()).stdout == ""


def test_a_closed_stdout_still_exits_zero(
    store_root: Path, invoke: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind(store_root)
    put(store_root, stamp="20300101T120000Z", hex4="1a1a", kind="question", needs="reply")

    def closed(line: str, keep: str = "") -> None:
        raise cli.OutputError(BrokenPipeError(32, "Broken pipe"))

    monkeypatch.setattr(cli, "_out", closed)
    monkeypatch.setattr(cli, "_silence_stdout", lambda: None)
    result = invoke("hook", "stop", stdin=stop_stdin())
    assert (result.returncode, result.stderr) == (0, "")


# --- hook install --------------------------------------------------------------------

EXISTING = (
    "{\n"
    '  "permissions": {\n'
    '    "defaultMode": "default",\n'
    '    "allow": ["Bash(git status)"]\n'
    "  },\n"
    '  "hooks": {\n'
    '    "PreToolUse": [\n'
    "      {\n"
    '        "matcher": "Bash",\n'
    '        "hooks": [ { "type": "command", "command": "echo  pre" } ]\n'
    "      }\n"
    "    ],\n"
    '    "SessionStart": [{"hooks": [{"type": "command", "command": "echo start"}]}]\n'
    "  },\n"
    '  "model": "opus"\n'
    "}\n"
)


def only_inserted(before: str, after: str) -> bool:
    """True when ``after`` is ``before`` with one run of text inserted somewhere."""
    if len(after) < len(before):
        return False
    prefix = 0
    while prefix < len(before) and before[prefix] == after[prefix]:
        prefix += 1
    tail = len(after) - len(before)
    return any(after[k + tail :] == before[k:] for k in range(prefix + 1))


def stop_groups(path: Path) -> list[object]:
    data = json.loads(path.read_bytes().decode("utf-8-sig"))
    groups = data["hooks"]["Stop"]
    assert isinstance(groups, list)
    return groups


def test_install_merges_one_stop_entry_through_the_console_script(tmp_path: Path) -> None:
    """D43: other hooks and keys keep their exact bytes, one Stop entry is added, a
    .bak of the old bytes is written first, and a second run writes nothing."""
    settings = tmp_path / "settings.json"
    settings.write_bytes(EXISTING.encode("utf-8"))
    backup = tmp_path / "settings.json.bak"
    root = tmp_path / "store"

    first = run_cli(["hook", "install", "--settings", str(settings)], root=root)

    assert (first.returncode, first.stderr) == (0, "")
    assert first.lines == [f"installed: {settings} (backup {backup})"]
    assert backup.read_bytes() == EXISTING.encode("utf-8")
    after = settings.read_bytes().decode("utf-8")
    assert only_inserted(EXISTING, after)
    old = json.loads(EXISTING)
    new = json.loads(after)
    assert {k: v for k, v in new.items() if k != "hooks"} == {
        k: v for k, v in old.items() if k != "hooks"
    }
    assert {k: v for k, v in new["hooks"].items() if k != "Stop"} == old["hooks"]
    assert new["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "passing-notes hook stop", "timeout": 10}]}
    ]

    written = settings.read_bytes()
    stamp = settings.stat().st_mtime_ns
    second = run_cli(["hook", "install", "--settings", str(settings)], root=root)
    assert (second.returncode, second.lines, second.stderr) == (
        0,
        [f"already installed: {settings}"],
        "",
    )
    assert settings.read_bytes() == written and settings.stat().st_mtime_ns == stamp
    assert backup.read_bytes() == EXISTING.encode("utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json", "settings.json.bak"]


def test_check_reports_and_writes_nothing_through_the_console_script(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_bytes(EXISTING.encode("utf-8"))
    root = tmp_path / "store"
    check = ["hook", "install", "--settings", str(settings), "--check"]

    before = run_cli(check, root=root)
    assert (before.returncode, before.lines) == (0, [f"missing: {settings}"])
    assert settings.read_bytes() == EXISTING.encode("utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json"]

    assert run_cli(["hook", "install", "--settings", str(settings)], root=root).returncode == 0
    written = settings.read_bytes()
    after = run_cli(check, root=root)
    assert (after.returncode, after.lines) == (0, [f"installed: {settings}"])
    assert settings.read_bytes() == written

    absent = tmp_path / "absent.json"
    missing = run_cli(["hook", "install", "--settings", str(absent), "--check"], root=root)
    assert (missing.returncode, missing.lines) == (0, [f"missing: {absent}"])
    assert not absent.exists()


@pytest.mark.parametrize("check", [False, True], ids=["install", "check"])
def test_invalid_json_exits_3_with_the_file_unchanged(tmp_path: Path, check: bool) -> None:
    settings = tmp_path / "settings.json"
    broken = b'{"hooks": {"Stop": [ ,]}'
    settings.write_bytes(broken)
    args = ["hook", "install", "--settings", str(settings), *(["--check"] if check else [])]
    result = run_cli(args, root=tmp_path / "store")
    assert result.returncode == 3 and result.stdout == ""
    assert result.stderr.startswith("passing-notes: error: settings-invalid: ")
    assert settings.read_bytes() == broken
    assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json"]


DEEP: Final = b'{"x": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"

HOSTILE = {
    "nested too deeply": DEEP,
    "bom then garbage": b"\xef\xbb\xbf{",
    "bom then nothing": b"\xef\xbb\xbf",
    "not UTF-8": b'{"x": "\xff\xfe"}',
    "zero bytes": b"",
    "only whitespace": b" \r\n\t",
    "top-level list": b"[]",
    "top-level string": b'"settings"',
    "top-level number": b"42",
    "top-level null": b"null",
    "hooks null": b'{\n  "hooks": null\n}\n',
    "hooks a list": b'{"hooks": []}',
    "hooks a string": b'{"hooks": "none"}',
    "Stop null": b'{"hooks": {"Stop": null}}',
    "Stop an object": b'{"hooks": {"Stop": {}}}',
    "Stop a string": b'{"hooks": {"Stop": "passing-notes hook stop"}}',
    "Stop a number": b'{"hooks": {"Stop": 1}}',
}


def assert_refused(result: Result, settings: Path, before: bytes, code: int = 3) -> None:
    """One coded error line and no traceback; the file unchanged and no backup."""
    name = "settings-invalid" if code == 3 else "settings-unwritable"
    assert (result.returncode, result.stdout) == (code, "")
    assert result.stderr.startswith(f"passing-notes: error: {name}: ")
    assert len(result.stderr.splitlines()) == 1 and "Traceback" not in result.stderr
    assert settings.read_bytes() == before
    assert sorted(p.name for p in settings.parent.iterdir()) == [settings.name]


@pytest.mark.parametrize("check", [False, True], ids=["install", "check"])
@pytest.mark.parametrize("raw", HOSTILE.values(), ids=HOSTILE.keys())
def test_a_hostile_settings_file_is_refused_never_a_traceback(
    tmp_path: Path, raw: bytes, check: bool
) -> None:
    """Whatever the file holds, install and --check end in exit 3 ``settings-invalid``
    through the console script. A zero-byte or blank file is not valid JSON, so it is
    refused like any other rather than replaced."""
    settings = tmp_path / "settings.json"
    settings.write_bytes(raw)
    args = ["hook", "install", "--settings", str(settings), *(["--check"] if check else [])]
    assert_refused(run_cli(args, root=tmp_path / "store"), settings, raw)


def test_deeply_nested_settings_are_refused_through_the_console_script(tmp_path: Path) -> None:
    """Round-2 regression: nesting past the recursion limit was a RecursionError
    traceback; now it is the coded refusal, with the reason named."""
    settings = tmp_path / "settings.json"
    settings.write_bytes(DEEP)
    args = ["hook", "install", "--settings", str(settings)]
    result = run_cli(args, root=tmp_path / "store", command=console_script())
    assert_refused(result, settings, DEEP)
    assert "nested too deeply" in result.stderr


BOUNDARY = {
    "RecursionError": (RecursionError("deep"), 3),
    "MemoryError": (MemoryError(), 3),
    "KeyError": (KeyError("hooks"), 3),
    "IndexError": (IndexError("span"), 3),
    "PermissionError": (PermissionError(13, "denied"), 4),
}


@pytest.mark.parametrize(("error", "code"), BOUNDARY.values(), ids=BOUNDARY.keys())
def test_the_installer_boundary_turns_any_merge_failure_into_a_coded_refusal(
    tmp_path: Path, invoke: Invoke, monkeypatch: pytest.MonkeyPatch, error: Exception, code: int
) -> None:
    """The one boundary (``hook._guarded``): an error the explicit checks did not
    foresee, raised anywhere in the merge, still exits with the installer's code."""
    settings = tmp_path / "settings.json"
    settings.write_bytes(EXISTING.encode("utf-8"))

    def broken(*_: object) -> str:
        raise error

    monkeypatch.setattr(hook, "merged_text", broken)
    result = invoke("hook", "install", "--settings", str(settings))
    assert_refused(result, settings, EXISTING.encode("utf-8"), code)


LAYOUTS = {
    "empty object": "{}",
    "empty object, pretty": "{\n}\n",
    "minified": '{"model":"opus","hooks":{"Notification":[{"hooks":[]}]}}',
    "hooks without Stop": '{\n  "hooks": {\n    "Notification": []\n  }\n}\n',
    "empty hooks": '{\n  "hooks": {}\n}\n',
    "other Stop entry": (
        '{\n  "hooks": {\n    "Stop": [\n      {"hooks": [{"type": "command", '
        '"command": "echo bye"}]}\n    ]\n  }\n}\n'
    ),
    "empty Stop list": '{\n  "hooks": {\n    "Stop": []\n  }\n}\n',
    "crlf, four spaces": '{\r\n    "model": "opus",\r\n    "hooks": {\r\n    }\r\n}\r\n',
    "tabs": '{\n\t"model": "opus"\n}\n',
    "duplicate hooks key": '{"hooks": {"Stop": 1}, "hooks": {"Notification": []}}',
    "unicode and escapes": '{\n  "name": "caf\\u00e9 \u00e9",\n  "x": "\\"}"\n}\n',
}


@pytest.mark.parametrize("text", LAYOUTS.values(), ids=LAYOUTS.keys())
def test_install_only_inserts_into_any_layout(tmp_path: Path, invoke: Invoke, text: str) -> None:
    settings = tmp_path / "settings.json"
    settings.write_bytes(text.encode("utf-8"))
    result = invoke("hook", "install", "--settings", str(settings))
    assert result.returncode == 0, result.stderr
    after = settings.read_bytes().decode("utf-8")
    assert only_inserted(text, after)
    old = json.loads(text)
    new = json.loads(after)
    hooks = old.get("hooks", {})
    assert stop_groups(settings) == [*hooks.get("Stop", []), hook.hook_entry()]
    assert {k: v for k, v in new.items() if k != "hooks"} == {
        k: v for k, v in old.items() if k != "hooks"
    }
    assert (tmp_path / "settings.json.bak").read_bytes() == text.encode("utf-8")
    assert invoke("hook", "install", "--settings", str(settings)).lines == [
        f"already installed: {settings}"
    ]


def test_install_keeps_a_byte_order_mark(tmp_path: Path, invoke: Invoke) -> None:
    settings = tmp_path / "settings.json"
    original = b"\xef\xbb\xbf" + EXISTING.encode("utf-8")
    settings.write_bytes(original)
    assert invoke("hook", "install", "--settings", str(settings)).returncode == 0
    after = settings.read_bytes()
    assert after.startswith(b"\xef\xbb\xbf")
    assert only_inserted(EXISTING, after[3:].decode("utf-8"))
    assert (tmp_path / "settings.json.bak").read_bytes() == original


def test_install_creates_a_missing_file_but_not_a_missing_folder(
    tmp_path: Path, invoke: Invoke
) -> None:
    settings = tmp_path / "settings.json"
    result = invoke("hook", "install", "--settings", str(settings))
    assert result.lines == [f"installed: {settings} (new file, nothing to back up)"]
    assert stop_groups(settings) == [hook.hook_entry()]
    assert not (tmp_path / "settings.json.bak").exists()

    nowhere = tmp_path / "no-such-folder" / "settings.json"
    refused = invoke("hook", "install", "--settings", str(nowhere))
    assert refused.returncode == 4 and "settings-unwritable" in refused.stderr
    assert not nowhere.parent.exists()


def test_an_entry_already_present_by_hand_counts_as_installed(
    tmp_path: Path, invoke: Invoke
) -> None:
    settings = tmp_path / "settings.json"
    text = (
        '{"hooks": {"Stop": [{"hooks": '
        '[{"type": "command", "command": "passing-notes hook stop"}]}]}}'
    )
    settings.write_bytes(text.encode("utf-8"))
    assert invoke("hook", "install", "--settings", str(settings), "--check").lines == [
        f"installed: {settings}"
    ]
    assert invoke("hook", "install", "--settings", str(settings)).lines == [
        f"already installed: {settings}"
    ]
    assert settings.read_bytes() == text.encode("utf-8")


def test_default_settings_path_is_the_user_settings_resolver(
    no_real_settings: Path, store_root: Path
) -> None:
    """Without --settings, install writes CLAUDE_CONFIG_DIR's settings.json (the one
    resolver configured_permission_mode reads), here a temporary folder."""
    target = no_real_settings / "settings.json"
    assert wake.user_settings_path() == target
    target.write_bytes(b'{\n  "permissions": {"defaultMode": "plan"}\n}\n')
    assert wake.configured_permission_mode(None)[1] == str(target)
    result = run_cli(["hook", "install"], root=store_root)
    assert result.lines == [f"installed: {target} (backup {target}.bak)"]
    assert stop_groups(target) == [hook.hook_entry()]
    assert wake.configured_permission_mode(None) == ("plan", str(target))


def test_user_settings_path_falls_back_to_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without CLAUDE_CONFIG_DIR the resolver uses this user's home folder."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert wake.user_settings_path({}) == tmp_path / ".claude" / "settings.json"


def test_the_installed_command_runs_the_hook_as_the_host_would(
    store_root: Path, tmp_path: Path
) -> None:
    """End to end: install, take the command the settings now hold, run it with the
    host's stdin, and get the block reply."""
    settings = tmp_path / "settings.json"
    assert (
        run_cli(["hook", "install", "--settings", str(settings)], root=store_root).returncode == 0
    )
    (group,) = stop_groups(settings)
    assert isinstance(group, dict)
    (entry,) = group["hooks"]
    assert entry["timeout"] == hook.HOOK_TIMEOUT_S == 10
    program, *args = str(entry["command"]).split()
    assert program == cli.PROG
    bind(store_root)
    asked = put(
        store_root,
        stamp="20300101T120000Z",
        hex4="1a1a",
        kind="question",
        needs="operator-decision",
    )
    result = run_cli(args, root=store_root, stdin=stop_stdin(), command=console_script())
    assert [line.split(" ")[0] for line in reason_lines(result)] == [asked]


# --- hook uninstall (CC-D14) ------------------------------------------------------------

OURS: Final = '{"type": "command", "command": "passing-notes hook stop", "timeout": 10}'

# Layouts whose round trip drops an empty "Stop": [] or "hooks": {} that was there before
# install: install's output is the same with or without it (documented in hook.py).
DROPS_EMPTY: Final = frozenset({"empty hooks", "empty Stop list", "crlf, four spaces"})


def uninstall_args(settings: Path) -> list[str]:
    return ["hook", "uninstall", "--settings", str(settings)]


def test_uninstall_gives_back_the_pre_install_bytes_through_the_console_script(
    tmp_path: Path,
) -> None:
    """CC-D14: install then uninstall is byte-identical to the file before install, a
    .bak of the installed bytes is written first, and a second run writes nothing."""
    settings = tmp_path / "settings.json"
    original = EXISTING.encode("utf-8")
    settings.write_bytes(original)
    backup = tmp_path / "settings.json.bak"
    root = tmp_path / "store"
    assert run_cli(["hook", "install", "--settings", str(settings)], root=root).returncode == 0
    installed = settings.read_bytes()
    assert installed != original

    first = run_cli(uninstall_args(settings), root=root, command=console_script())

    assert (first.returncode, first.stderr) == (0, "")
    assert first.lines == [f"uninstalled: {settings} (backup {backup})"]
    assert settings.read_bytes() == original
    assert backup.read_bytes() == installed
    check = run_cli(["hook", "install", "--settings", str(settings), "--check"], root=root)
    assert check.lines == [f"missing: {settings}"]

    stamp = settings.stat().st_mtime_ns
    second = run_cli(uninstall_args(settings), root=root)
    assert (second.returncode, second.lines, second.stderr) == (
        0,
        [f"not installed: {settings}"],
        "",
    )
    assert settings.read_bytes() == original and settings.stat().st_mtime_ns == stamp
    assert backup.read_bytes() == installed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json", "settings.json.bak"]


@pytest.mark.parametrize(("name", "text"), LAYOUTS.items(), ids=LAYOUTS.keys())
def test_install_then_uninstall_round_trips_any_layout(
    tmp_path: Path, invoke: Invoke, name: str, text: str
) -> None:
    """Every layout comes back byte for byte, hooks.Stop or hooks absent before install
    included. An empty Stop list or hooks object that was there before is the one thing
    not restored: the result is the old text with that one run cut out."""
    settings = tmp_path / "settings.json"
    settings.write_bytes(text.encode("utf-8"))
    assert invoke("hook", "install", "--settings", str(settings)).returncode == 0
    result = invoke(*uninstall_args(settings))
    assert result.returncode == 0, result.stderr
    after = settings.read_bytes().decode("utf-8")
    if name not in DROPS_EMPTY:
        assert after == text
        return
    assert only_inserted(after, text) and after != text
    old = json.loads(text)
    old["hooks"].pop("Stop", None)
    if not old["hooks"]:
        del old["hooks"]
    assert json.loads(after) == old


def test_uninstall_keeps_a_byte_order_mark(tmp_path: Path, invoke: Invoke) -> None:
    settings = tmp_path / "settings.json"
    original = b"\xef\xbb\xbf" + EXISTING.encode("utf-8")
    settings.write_bytes(original)
    assert invoke("hook", "install", "--settings", str(settings)).returncode == 0
    assert invoke(*uninstall_args(settings)).returncode == 0
    assert settings.read_bytes() == original


def test_uninstall_leaves_an_empty_object_where_install_created_the_file(
    tmp_path: Path, invoke: Invoke
) -> None:
    """The settings file is never deleted: a file install created ends as ``{}``."""
    settings = tmp_path / "settings.json"
    assert invoke("hook", "install", "--settings", str(settings)).returncode == 0
    result = invoke(*uninstall_args(settings))
    assert result.lines == [f"uninstalled: {settings} (backup {settings}.bak)"]
    assert settings.read_bytes() == b"{\n}\n"


def test_uninstall_without_the_entry_writes_nothing(tmp_path: Path, invoke: Invoke) -> None:
    settings = tmp_path / "settings.json"
    settings.write_bytes(EXISTING.encode("utf-8"))
    result = invoke(*uninstall_args(settings))
    assert (result.returncode, result.lines, result.stderr) == (
        0,
        [f"not installed: {settings}"],
        "",
    )
    assert settings.read_bytes() == EXISTING.encode("utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json"]

    absent = tmp_path / "absent.json"
    missing = invoke(*uninstall_args(absent))
    assert (missing.returncode, missing.lines) == (0, [f"not installed: {absent}"])
    assert not absent.exists()


# Red-on-garbage anchors: near misses of the command that uninstall must leave in place.
ANCHORS: Final = (
    '{"type": "command", "command": "passing-notes hook stop --verbose", "timeout": 10}',
    '{"type": "command", "command": "passing-notes  hook stop"}',
    '{"type": "command", "command": "passing-notes hook stopper"}',
    '{"type": "prompt", "command": "passing-notes hook stop"}',
)


def test_uninstall_keeps_unrelated_stop_hooks_and_near_miss_commands(
    tmp_path: Path, invoke: Invoke
) -> None:
    """Only the exact command goes: an unrelated hook in the same group and every
    anchor (a different command or type) keep their exact bytes."""
    head = (
        '{\n  "hooks": {\n    "Stop": [\n'
        '      {"hooks": [{"type": "command", "command": "echo bye"}'
    )
    tail = (
        "]},\n"
        + "".join(f'      {{"hooks": [{anchor}]}},\n' for anchor in ANCHORS)
        + '      {"matcher": "", "hooks": [{"type": "command", "command": "echo two"}]}\n'
        "    ]\n  }\n}\n"
    )
    settings = tmp_path / "settings.json"
    settings.write_bytes((head + ", " + OURS + tail).encode("utf-8"))

    result = invoke(*uninstall_args(settings))

    assert result.returncode == 0, result.stderr
    assert settings.read_bytes() == (head + tail).encode("utf-8")
    groups = stop_groups(settings)
    assert len(groups) == 2 + len(ANCHORS)
    assert [group["hooks"] for group in groups[1:-1]] == [  # type: ignore[index]
        [json.loads(anchor)] for anchor in ANCHORS
    ]


def test_a_hand_edited_entry_is_recognised_by_its_exact_command(
    tmp_path: Path, invoke: Invoke
) -> None:
    """A different timeout, an extra key, reordered keys and a matcher wrapper: the
    command string alone identifies the entry, and its emptied group goes with it."""
    head = (
        "{\n"
        '  "hooks": {\n'
        '    "Stop": [\n'
        '      {"hooks": [{"type": "command", "command": "echo bye"}]}'
    )
    ours = (
        ",\n"
        "      {\n"
        '        "matcher": "",\n'
        '        "hooks": [\n'
        '          {"timeout": 30, "statusMessage": "notes", '
        '"command": "passing-notes hook stop", "type": "command"}\n'
        "        ]\n"
        "      }"
    )
    tail = '\n    ],\n    "Notification": []\n  }\n}\n'
    settings = tmp_path / "settings.json"
    settings.write_bytes((head + ours + tail).encode("utf-8"))
    assert invoke("hook", "install", "--settings", str(settings), "--check").lines == [
        f"installed: {settings}"
    ]

    result = invoke(*uninstall_args(settings))

    assert result.lines == [f"uninstalled: {settings} (backup {settings}.bak)"]
    assert settings.read_bytes() == (head + tail).encode("utf-8")
    assert invoke("hook", "install", "--settings", str(settings), "--check").lines == [
        f"missing: {settings}"
    ]


def test_uninstall_removes_every_copy_and_keeps_a_group_with_other_keys(
    tmp_path: Path, invoke: Invoke
) -> None:
    settings = tmp_path / "settings.json"
    text = (
        f'{{"hooks": {{"Stop": [{{"hooks": [{OURS}], "note": "mine"}}, {{"hooks": [{OURS}]}}]}}}}'
    )
    settings.write_bytes(text.encode("utf-8"))
    assert invoke(*uninstall_args(settings)).returncode == 0
    assert settings.read_bytes() == b'{"hooks": {"Stop": [{"hooks": [], "note": "mine"}]}}'


def test_uninstall_keeps_the_meaning_of_duplicate_keys(tmp_path: Path, invoke: Invoke) -> None:
    """Cutting the whole last ``hooks`` would bring the earlier duplicate back into
    force, so the next shallower cut is taken: the settings still mean ``hooks: {}``."""
    settings = tmp_path / "settings.json"
    text = f'{{"hooks": {{"Stop": [1]}}, "hooks": {{"Stop": [{{"hooks": [{OURS}]}}]}}}}'
    settings.write_bytes(text.encode("utf-8"))
    assert invoke(*uninstall_args(settings)).returncode == 0
    assert settings.read_bytes() == b'{"hooks": {"Stop": [1]}, "hooks": {}}'


def test_invalid_json_is_refused_by_uninstall_with_the_file_unchanged(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    broken = b'{"hooks": {"Stop": [ ,]}'
    settings.write_bytes(broken)
    result = run_cli(uninstall_args(settings), root=tmp_path / "store")
    assert_refused(result, settings, broken)


@pytest.mark.parametrize("raw", HOSTILE.values(), ids=HOSTILE.keys())
def test_a_hostile_settings_file_is_refused_by_uninstall(
    tmp_path: Path, invoke: Invoke, raw: bytes
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_bytes(raw)
    assert_refused(invoke(*uninstall_args(settings)), settings, raw)


@pytest.mark.parametrize(("error", "code"), BOUNDARY.values(), ids=BOUNDARY.keys())
def test_the_uninstall_boundary_turns_any_failure_into_a_coded_refusal(
    tmp_path: Path, invoke: Invoke, monkeypatch: pytest.MonkeyPatch, error: Exception, code: int
) -> None:
    settings = tmp_path / "settings.json"
    settings.write_bytes(EXISTING.encode("utf-8"))
    assert invoke("hook", "install", "--settings", str(settings)).returncode == 0
    installed = settings.read_bytes()
    (tmp_path / "settings.json.bak").unlink()

    def broken(*_: object) -> str:
        raise error

    monkeypatch.setattr(hook, "_cut", broken)
    assert_refused(invoke(*uninstall_args(settings)), settings, installed, code)


def test_uninstall_refuses_when_no_cut_keeps_the_other_settings(
    tmp_path: Path, invoke: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every cut is checked to parse to the old settings minus the entry; when none
    does, nothing is written, not even a .bak."""
    settings = tmp_path / "settings.json"
    text = f'{{"hooks": {{"Stop": [{{"hooks": [{OURS}]}}]}}}}'
    settings.write_bytes(text.encode("utf-8"))
    monkeypatch.setattr(hook, "_cut", lambda text, *_: text)
    result = invoke(*uninstall_args(settings))
    assert_refused(result, settings, text.encode("utf-8"))
    assert "could not remove the Stop entry" in result.stderr


def test_uninstall_defaults_to_the_user_settings_file(
    no_real_settings: Path, store_root: Path
) -> None:
    target = no_real_settings / "settings.json"
    original = b'{\n  "permissions": {"defaultMode": "plan"}\n}\n'
    target.write_bytes(original)
    assert run_cli(["hook", "install"], root=store_root).returncode == 0
    result = run_cli(["hook", "uninstall"], root=store_root)
    assert result.lines == [f"uninstalled: {target} (backup {target}.bak)"]
    assert target.read_bytes() == original
