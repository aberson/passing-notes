"""The wake doorbell: binding, refusals, de-duplication and evidence reporting.

The Claude host is faked here; the live round trip is in tests/cross_host/run_wake.py.
The bridge parser is calibrated on synthetic stream-json shaped like Claude Code's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import PROJECT, Invoke, Result

from passing_notes import cli, protocol, wake
from passing_notes.store import Store, default_now

SID = "11111111-2222-4333-8444-555555555555"
OTHER_SID = "99999999-2222-4333-8444-555555555555"
LANE = "docs-lane"
SENDER = "central"
NAME = "example - docs lane"


@dataclass
class FakeHost:
    sessions: list[wake.Session] = field(default_factory=list)
    result: wake.SendResult = field(
        default_factory=lambda: wake.SendResult(True, "msg-1", "delivered")
    )
    sent: list[tuple[str, str, str]] = field(default_factory=list)
    on_send: object = None

    def list_sessions(self) -> list[wake.Session]:
        return list(self.sessions)

    def send(self, address: str, text: str, permission_mode: str) -> wake.SendResult:
        self.sent.append((address, text, permission_mode))
        if callable(self.on_send):
            self.on_send()
        return self.result


def session(sid: str = SID, name: str | None = NAME, status: str = "idle") -> wake.Session:
    return wake.Session(sid, name, status, "interactive", 1234)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    fake = FakeHost(sessions=[session()])
    monkeypatch.setattr(cli, "make_wake_host", lambda: fake)
    return fake


def post(invoke: Invoke, *extra: str, key: str = "k1", to: str = LANE) -> list[str]:
    args = [
        "post",
        "--project",
        PROJECT,
        "--from",
        SENDER,
        "--to",
        to,
        "--subject",
        "Test wake",
        "--kind",
        "question",
        "--needs",
        "reply",
        "--body-stdin",
    ]
    if key:
        args += ["--idempotency-key", key]
    result = invoke(*args, *extra, stdin=b"Synthetic body.\n")
    return [str(result.returncode), *result.lines, result.stderr]


def bind(invoke: Invoke, mode: str = "dontAsk", sid: str = SID) -> Result:
    return invoke(
        "wake",
        "bind",
        "--project",
        PROJECT,
        "--lane",
        LANE,
        "--host",
        "claude",
        "--session",
        sid,
        "--permission-mode",
        mode,
    )


def test_doorbell_text_is_exactly_the_contract() -> None:
    nid = "20300101T120000Z-central-to-docs-lane-1f57"
    assert wake.doorbell_text(nid) == (
        "New Passing Note 20300101T120000Z-central-to-docs-lane-1f57. "
        "Read it and respond through Passing Notes."
    )


def test_bind_records_exact_session_and_mode(invoke: Invoke, host: FakeHost, opened: Store) -> None:
    result = bind(invoke)
    assert result.returncode == 0, result.stderr
    binding = wake.load_binding(opened, PROJECT, LANE)
    assert (binding.session_id, binding.permission_mode, binding.host) == (SID, "dontAsk", "claude")
    shown = invoke("wake", "show", "--project", PROJECT)
    assert shown.lines[0].split("\t")[:4] == [LANE, "claude", SID, "dontAsk"]


def test_bind_refuses_a_session_that_is_not_live(invoke: Invoke, host: FakeHost) -> None:
    result = bind(invoke, sid=OTHER_SID)
    assert result.returncode == cli.EXIT_WAKE
    assert wake.WAKE_SESSION_UNAVAILABLE in result.stderr


def test_bind_refuses_a_title_instead_of_a_session_id(invoke: Invoke, host: FakeHost) -> None:
    result = invoke(
        "wake",
        "bind",
        "--project",
        PROJECT,
        "--lane",
        LANE,
        "--host",
        "claude",
        "--session",
        NAME,
        "--permission-mode",
        "dontAsk",
    )
    assert result.returncode == cli.EXIT_USAGE


def test_post_wake_delivers_and_reports_states_separately(invoke: Invoke, host: FakeHost) -> None:
    bind(invoke)
    out = post(invoke, "--wake")
    assert out[0] == "0", out
    note_id = out[1]
    assert out[2] == f"note: {note_id} is in the store"
    assert out[3].startswith(f"wake: delivered to session {SID} ({NAME}) inbox, message msg-1")
    assert out[4] == "ack: not observed"
    assert out[5] == "reply: not observed"
    assert host.sent == [(NAME, wake.doorbell_text(note_id), "dontAsk")]


def test_ack_and_reply_are_reported_only_when_their_files_exist(
    invoke: Invoke, host: FakeHost, opened: Store
) -> None:
    bind(invoke)
    posted = post(invoke)
    note_id = posted[1]

    def recipient_responds() -> None:  # what a woken lane does through the CLI
        opened.publish_ack(PROJECT, note_id, protocol.make_ack(LANE, default_now(), None))
        draft = protocol.make_draft(
            project=PROJECT,
            from_project=None,
            from_lane=LANE,
            to=SENDER,
            subject="Re: test",
            kind="answer",
            needs="fyi",
            work=None,
            revision=None,
            supersedes=None,
            reply_to=note_id,
            idempotency_key=None,
            refs=[],
        )
        opened.publish_note(draft, "ok\n")

    host.on_send = recipient_responds
    result = invoke("wake", "send", "--project", PROJECT, "--lane", LANE, "--wait", "5", note_id)
    assert result.returncode == 0, result.stderr
    assert result.lines[2].startswith("ack: observed, read_utc ")
    assert result.lines[3].startswith("reply: observed ")


def test_repeated_send_never_rings_twice(invoke: Invoke, host: FakeHost) -> None:
    bind(invoke)
    note_id = post(invoke, "--wake")[1]
    again = invoke("wake", "send", "--project", PROJECT, "--lane", LANE, note_id)
    assert again.returncode == 0
    assert again.lines[1].startswith(f"wake: not sent again: already delivered to session {SID}")
    assert len(host.sent) == 1


def test_retried_post_wake_posts_no_second_note_and_no_second_ring(
    invoke: Invoke, host: FakeHost, opened: Store
) -> None:
    bind(invoke)
    first = post(invoke, "--wake", key="same")
    second = post(invoke, "--wake", key="same")
    assert first[1] == second[1]
    notes, _ = opened.scan_notes(PROJECT)
    assert len(notes) == 1
    assert len(host.sent) == 1


def test_post_wake_needs_an_idempotency_key(invoke: Invoke, host: FakeHost, opened: Store) -> None:
    out = post(invoke, "--wake", key="")
    assert out[0] == str(cli.EXIT_USAGE)
    assert opened.scan_notes(PROJECT)[0] == []


@pytest.mark.parametrize("to", ["all", "operator"])
def test_post_wake_needs_one_lane(invoke: Invoke, host: FakeHost, to: str) -> None:
    assert post(invoke, "--wake", to=to)[0] == str(cli.EXIT_USAGE)


def test_unbound_lane_reports_failure_and_keeps_the_note(
    invoke: Invoke, host: FakeHost, opened: Store
) -> None:
    out = post(invoke, "--wake")
    assert out[0] == str(cli.EXIT_WAKE)
    assert out[2:4] == [f"note: {out[1]} is in the store", "wake: not delivered"]
    assert wake.WAKE_NO_BINDING in out[-1]
    assert len(opened.scan_notes(PROJECT)[0]) == 1
    assert host.sent == []


def test_closed_or_stale_session_is_unavailable(invoke: Invoke, host: FakeHost) -> None:
    bind(invoke)
    host.sessions = [session(sid=OTHER_SID)]
    out = post(invoke, "--wake")
    assert out[0] == str(cli.EXIT_WAKE)
    assert wake.WAKE_SESSION_UNAVAILABLE in out[-1]
    assert host.sent == []


def test_busy_session_is_not_interrupted(invoke: Invoke, host: FakeHost) -> None:
    bind(invoke)
    host.sessions = [session(status="busy")]
    out = post(invoke, "--wake")
    assert out[0] == str(cli.EXIT_WAKE)
    assert wake.WAKE_SESSION_BUSY in out[-1]
    assert host.sent == []


def test_duplicate_session_names_are_refused(invoke: Invoke, host: FakeHost) -> None:
    bind(invoke)
    host.sessions = [session(), session(sid=OTHER_SID)]
    out = post(invoke, "--wake")
    assert out[0] == str(cli.EXIT_WAKE)
    assert wake.WAKE_SESSION_AMBIGUOUS in out[-1]
    assert host.sent == []


def test_failed_delivery_can_be_retried_without_a_new_note(
    invoke: Invoke, host: FakeHost, opened: Store
) -> None:
    bind(invoke)
    host.result = wake.SendResult(False, None, "bridge made no SendMessage call")
    first = post(invoke, "--wake")
    assert first[0] == str(cli.EXIT_WAKE)
    assert wake.WAKE_BRIDGE_FAILED in first[-1]
    host.result = wake.SendResult(True, "msg-2", "delivered")
    retry = invoke("wake", "send", "--project", PROJECT, "--lane", LANE, first[1])
    assert retry.returncode == 0
    assert "message msg-2" in retry.lines[1]
    assert len(opened.scan_notes(PROJECT)[0]) == 1
    assert len(host.sent) == 2


def test_already_acknowledged_note_is_not_rung(
    invoke: Invoke, host: FakeHost, opened: Store
) -> None:
    bind(invoke)
    note_id = post(invoke)[1]
    opened.publish_ack(PROJECT, note_id, protocol.make_ack(LANE, default_now(), None))
    result = invoke("wake", "send", "--project", PROJECT, "--lane", LANE, note_id)
    assert result.returncode == 0
    assert result.lines[1] == "wake: not sent: the lane already acknowledged this note"
    assert host.sent == []


def test_note_for_another_lane_is_refused(invoke: Invoke, host: FakeHost) -> None:
    bind(invoke)
    note_id = post(invoke, to="other-lane")[1]
    result = invoke("wake", "send", "--project", PROJECT, "--lane", LANE, note_id)
    assert result.returncode == cli.EXIT_WAKE
    assert wake.WAKE_NOT_A_LANE in result.stderr


def test_rebinding_to_a_new_window_allows_one_new_ring(invoke: Invoke, host: FakeHost) -> None:
    bind(invoke)
    note_id = post(invoke, "--wake")[1]
    host.sessions = [session(sid=OTHER_SID)]
    bind(invoke, sid=OTHER_SID)
    again = invoke("wake", "send", "--project", PROJECT, "--lane", LANE, note_id)
    assert again.returncode == 0
    assert len(host.sent) == 2


def test_concurrent_sender_is_refused(invoke: Invoke, host: FakeHost, opened: Store) -> None:
    bind(invoke)
    note_id = post(invoke)[1]
    _, lock = wake.ring_paths(opened, PROJECT, note_id, LANE, SID)
    lock.write_text("held")
    result = invoke("wake", "send", "--project", PROJECT, "--lane", LANE, note_id)
    assert result.returncode == cli.EXIT_WAKE
    assert wake.WAKE_IN_PROGRESS in result.stderr
    assert host.sent == []


# --- The bridge is judged from its tool calls, never its words -------------------------


def _stream(listing: str, sends: list[tuple[str, str, str]]) -> list[str]:
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "t0", "name": "ListAgents", "input": {}}]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "t0", "content": listing}]
            },
        },
    ]
    for i, (to, text, result) in enumerate(sends, 1):
        events.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"t{i}",
                            "name": "SendMessage",
                            "input": {"to": to, "message": text},
                        }
                    ]
                },
            }
        )
        events.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"t{i}",
                            "content": [{"type": "text", "text": result}],
                        }
                    ]
                },
            }
        )
    events.append({"type": "result", "result": "SENT"})
    return [json.dumps(e) for e in events]


LISTING = (
    "This session is bridge [aaaaaa].\n\nPeer sessions (2):\n"
    f"  {NAME} [3f95a9]  ·  interactive  ·  idle\n"
    "  other [123456]  ·  interactive  ·  busy\n"
)
OK = json.dumps({"success": True, "message": "in that session's inbox", "msg_id": "m-9"})
TEXT = wake.doorbell_text("20300101T120000Z-central-to-docs-lane-1a2b")


def test_bridge_success_needs_exact_address_text_and_success() -> None:
    result = wake.parse_bridge_stream(_stream(LISTING, [(NAME, TEXT, OK)]), NAME, TEXT)
    assert result == wake.SendResult(True, "m-9", "delivered")


@pytest.mark.parametrize(
    ("listing", "sends", "reason"),
    [
        (LISTING, [], "no SendMessage"),
        (LISTING, [(NAME, TEXT + " Also do X.", OK)], "different address or text"),
        (LISTING, [("other", TEXT, OK)], "different address or text"),
        (LISTING, [(NAME, TEXT, OK), (NAME, TEXT, OK)], "more than one"),
        (LISTING, [(NAME, TEXT, json.dumps({"success": False, "error": "x"}))], "did not succeed"),
        (LISTING + f"  {NAME} [bbbbbb]  ·  interactive  ·  idle\n", [], "not unique"),
    ],
)
def test_bridge_failures_are_never_reported_as_delivered(
    listing: str, sends: list[tuple[str, str, str]], reason: str
) -> None:
    result = wake.parse_bridge_stream(_stream(listing, sends), NAME, TEXT)
    assert not result.delivered
    assert reason in result.detail


def test_parse_sessions_reads_claude_agents_json() -> None:
    rows = wake.parse_sessions(
        json.dumps(
            [
                {"pid": 1, "sessionId": SID, "name": NAME, "status": "idle", "kind": "interactive"},
                {"pid": 2, "status": "busy"},
            ]
        )
    )
    assert rows == [wake.Session(SID, NAME, "idle", "interactive", 1)]


def test_records_live_outside_the_protocol_folders(
    invoke: Invoke, host: FakeHost, store_root: Path
) -> None:
    bind(invoke)
    post(invoke, "--wake")
    wake_files = {p.relative_to(store_root).parts[0] for p in store_root.rglob("*") if p.is_file()}
    assert wake_files == {"messages", "wake"}


def test_host_processes_never_inherit_the_callers_claude_session() -> None:
    env = wake.host_env(
        {
            "PATH": "x",
            "CLAUDE_CODE_SESSION_ID": SID,
            "CLAUDECODE": "1",
            "Claude_Pid": "7",
            "CLAUDE_CONFIG_DIR": "cfg",
        }
    )
    assert env == {"PATH": "x", "CLAUDE_CONFIG_DIR": "cfg"}


def test_repeat_while_the_lane_is_busy_reports_the_earlier_delivery(
    invoke: Invoke, host: FakeHost
) -> None:
    bind(invoke)
    note_id = post(invoke, "--wake")[1]
    host.sessions = [session(status="busy")]
    again = invoke("wake", "send", "--project", PROJECT, "--lane", LANE, note_id)
    assert again.returncode == 0
    assert again.lines[1].startswith("wake: not sent again: already delivered")
    assert len(host.sent) == 1


# --- The configured permission mode -------------------------------------------------


def _settings(folder: Path, mode: str | None, name: str = "settings.json") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    body: dict[str, object] = {"permissions": {"defaultMode": mode}} if mode else {}
    (folder / name).write_text(json.dumps(body), encoding="utf-8")


def _env(tmp_path: Path) -> dict[str, str]:
    return {"CLAUDE_CONFIG_DIR": str(tmp_path / "user"), "ProgramFiles": str(tmp_path / "pf")}


def test_configured_mode_follows_claude_settings_precedence(tmp_path: Path) -> None:
    project = tmp_path / "project"
    env = _env(tmp_path)
    assert wake.configured_permission_mode(project, env)[0] == "default"
    _settings(tmp_path / "user", "bypassPermissions")
    assert wake.configured_permission_mode(project, env)[0] == "bypassPermissions"
    _settings(project / ".claude", "acceptEdits")
    assert wake.configured_permission_mode(project, env)[0] == "acceptEdits"
    _settings(project / ".claude", "plan", "settings.local.json")
    mode, source = wake.configured_permission_mode(project, env)
    assert (mode, source) == ("plan", str(project / ".claude" / "settings.local.json"))
    _settings(tmp_path / "pf" / "ClaudeCode", "default", "managed-settings.json")
    assert wake.configured_permission_mode(project, env)[0] == "default"


def test_unknown_or_unreadable_modes_are_skipped(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
    _settings(project / ".claude", "yolo", "settings.local.json")
    _settings(tmp_path / "user", "dontAsk")
    assert wake.configured_permission_mode(project, _env(tmp_path))[0] == "dontAsk"


def test_bind_without_a_mode_uses_the_windows_configured_mode(
    invoke: Invoke, host: FakeHost, opened: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    _settings(project / ".claude", "bypassPermissions")
    for key, value in _env(tmp_path).items():
        monkeypatch.setenv(key, value)
    host.sessions = [wake.Session(SID, NAME, "idle", "interactive", 1, str(project))]
    result = invoke(
        "wake", "bind", "--project", PROJECT, "--lane", LANE, "--host", "claude", "--session", SID
    )
    assert result.returncode == 0, result.stderr
    assert "in bypassPermissions mode (configured default, from " in result.lines[0]
    assert wake.load_binding(opened, PROJECT, LANE).permission_mode == "bypassPermissions"
