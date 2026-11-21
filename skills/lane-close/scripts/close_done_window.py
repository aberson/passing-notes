"""Inspect and close one operator-authorized, finished Codex lane launcher on Windows.

This is a lane-close support script, not a generic process killer. Listing is read-only;
closing requires the exact identity returned by a prior listing and rechecks it.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import ntpath
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

try:
    from passing_notes import protocol
    from passing_notes.store import CARDS_DIR, Store, StoreError, is_link_or_reparse_point
except ModuleNotFoundError as exc:
    if exc.name != "passing_notes":
        raise
    # Installed skills run under the default Python, while the CLI lives in a uv tool.
    # Re-executing its interpreter also loads editable installs through their .pth files.
    try:
        result = subprocess.run(
            ["uv", "tool", "dir"], capture_output=True, text=True, timeout=10, check=False
        )
    except OSError as lookup_error:
        raise RuntimeError("cannot locate the installed passing-notes tool") from lookup_error
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("cannot locate the installed passing-notes tool") from exc
    tool_python = Path(result.stdout.strip()) / "passing-notes" / "Scripts" / "python.exe"
    if not tool_python.is_file() or Path(sys.executable).resolve() == tool_python.resolve():
        raise RuntimeError("passing-notes is not installed in the uv tool environment") from exc
    if __name__ != "__main__":
        raise RuntimeError("import this helper with the passing-notes tool environment") from exc
    raise SystemExit(
        subprocess.call([str(tool_python), str(Path(__file__).resolve()), *sys.argv[1:]])
    )


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    name: str
    command: str
    created: str


class Refused(Exception):
    pass


def _windows_args(command: str) -> list[str]:
    if os.name != "nt":
        raise Refused("Windows process inspection is required")
    count = ctypes.c_int()
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    pointer = parse(command, ctypes.byref(count))
    if not pointer:
        raise Refused("cannot parse process command line")
    try:
        return [pointer[index] for index in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(pointer)


def _snapshot() -> list[Process]:
    if os.name != "nt":
        raise Refused("Windows process inspection is required")
    script = (
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
        "$ErrorActionPreference='Stop'; "
        "@(Get-CimInstance Win32_Process | ForEach-Object { "
        "[pscustomobject]@{pid=$_.ProcessId; parent=$_.ParentProcessId; "
        "name=$_.Name; command=$_.CommandLine; "
        "created=$_.CreationDate.ToUniversalTime().ToString('o')} "
        "}) | ConvertTo-Json -Compress -Depth 3"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise Refused("cannot obtain a complete Windows process snapshot")
    try:
        rows = json.loads(result.stdout)
        if isinstance(rows, dict):
            rows = [rows]
        return [
            Process(
                int(row["pid"]),
                int(row["parent"]),
                str(row["name"]),
                str(row["command"] or ""),
                str(row["created"]),
            )
            for row in rows
        ]
    except (ValueError, TypeError, KeyError) as exc:
        raise Refused("invalid Windows process snapshot") from exc


def _utc(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError("missing timezone")
        return result.astimezone(UTC)
    except ValueError as exc:
        raise Refused("invalid process or card timestamp") from exc


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _launcher_arg(process: Process) -> str | None:
    if process.name.lower() not in {"powershell.exe", "pwsh.exe"} or not process.command:
        return None
    args = _windows_args(process.command)
    lower = [arg.lower() for arg in args]
    if (
        len(args) != 6
        or "-noexit" not in lower
        or "-executionpolicy" not in lower
        or "-file" not in lower
    ):
        return None
    if lower.index("-executionpolicy") + 1 >= len(args):
        return None
    if lower[lower.index("-executionpolicy") + 1] != "bypass":
        return None
    file_index = lower.index("-file")
    if file_index != len(args) - 2:
        return None
    return args[-1]


def _legacy_launcher_arg(process: Process) -> str | None:
    """Recognize the narrow PowerShell -File shape used by older manual launchers."""
    if process.name.lower() not in {"powershell.exe", "pwsh.exe"} or not process.command:
        return None
    args = _windows_args(process.command)
    lower = [arg.lower() for arg in args]
    if not args or ntpath.basename(args[0]).lower() != process.name.lower():
        return None
    if len(args) < 3 or lower[-2] != "-file":
        return None
    options = lower[1:-2]
    if "-noexit" in options:
        options.remove("-noexit")
    if "-noprofile" in options:
        options.remove("-noprofile")
    if options == ["-executionpolicy", "bypass"] or not options:
        return args[-1]
    return None


_LEGACY_FOLDER = re.compile(r"full-access-\d{8}\Z")
_SESSION_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_CHECKOUT_LINE = re.compile(r"^Set-Location -LiteralPath '((?:''|[^'])*)'\r?$", re.MULTILINE)
_RESUME_LINE = re.compile(r"^& codex resume '([0-9a-f-]{36})'(?:\s|$)[^\r\n]*\r?$", re.MULTILINE)


def _legacy_assess(
    store: Store,
    state_root: Path,
    project: str,
    lane: str,
    launcher: Path,
    root: Process,
    processes: list[Process],
) -> None:
    """Verify a live legacy Codex session for inventory only; never grant close authority."""
    updated, window = _cards_safe(store, project, lane)
    card = store.load_card(project, lane)
    if card.state != "done" or card.window != window or card.updated_utc != updated:
        raise Refused("legacy card changed during inspection")
    checkout = card.checkout
    if not checkout or not _SESSION_ID.fullmatch(window):
        raise Refused("legacy card lacks an exact checkout or session identity")
    if (
        not launcher.is_absolute()
        or not _LEGACY_FOLDER.fullmatch(launcher.parent.name)
        or not _same_path(launcher.parent.parent, state_root)
        or launcher.name != f"{project}-{lane}.ps1"
        or is_link_or_reparse_point(state_root)
        or is_link_or_reparse_point(launcher.parent)
        or is_link_or_reparse_point(launcher)
        or not launcher.is_file()
        or launcher.stat().st_size > 65536
    ):
        raise Refused("legacy launcher path is missing, linked or unexpected")
    source = launcher.read_text(encoding="utf-8-sig")
    checkouts = _CHECKOUT_LINE.findall(source)
    sessions = _RESUME_LINE.findall(source)
    if (
        len(checkouts) != 1
        or len(sessions) != 1
        or not _same_path(checkouts[0].replace("''", "'"), checkout)
        or sessions[0] != window
    ):
        raise Refused("legacy launcher does not match the done card")
    matches = [
        p for p in processes if (arg := _legacy_launcher_arg(p)) and _same_path(arg, launcher)
    ]
    if len(matches) != 1 or matches[0] != root:
        raise Refused("legacy launcher process is missing or ambiguous")
    by_pid = {p.pid: p for p in processes}
    if len(by_pid) != len(processes):
        raise Refused("ambiguous process snapshot")
    descendants: set[int] = {root.pid}
    for _ in range(16):
        children = {
            p.pid for p in processes if p.parent in descendants and p.pid not in descendants
        }
        if not children:
            break
        descendants.update(children)
        if len(descendants) > 64:
            raise Refused("legacy process tree exceeds inventory limit")
    else:
        raise Refused("legacy process tree exceeds depth limit")
    resume_pattern = re.compile(
        r"(?i)(?:^|\s)resume\s+['\"]?" + re.escape(window) + r"(?:['\"]|\s|$)"
    )

    def ordered_ancestry(candidate: Process) -> bool:
        current = candidate
        seen: set[int] = set()
        while current.pid != root.pid:
            parent = by_pid.get(current.parent)
            if parent is None or parent.pid in seen:
                return False
            if not (_utc(root.created) <= _utc(parent.created) <= _utc(current.created)):
                return False
            seen.add(parent.pid)
            current = parent
        return True

    if not any(
        p.pid != root.pid
        and p.pid in descendants
        and p.name.lower() in {"node.exe", "codex.exe"}
        and p.command
        and (p.name.lower() == "codex.exe" or "codex" in p.command.lower())
        and resume_pattern.search(p.command)
        and ordered_ancestry(p)
        for p in processes
    ):
        raise Refused("no live Codex descendant resumes the card session")


def _public_inventory(
    store: Store, state_root: Path, project: str, processes: list[Process], lane_filter: str | None
) -> list[dict[str, str | bool]]:
    """Public, read-only projection of standard closeable and legacy live windows."""
    if len(processes) > 8192:
        raise Refused("process inventory limit exceeded")
    rows: list[dict[str, str | bool]] = []
    candidates = 0
    for process in processes:
        launcher_arg = _legacy_launcher_arg(process)
        if not launcher_arg:
            continue
        launcher = Path(launcher_arg)
        standard = re.fullmatch(r"([a-z0-9]+(?:-[a-z0-9]+)*)-\d{8}T\d{6}Z\.ps1", launcher.name)
        if standard and _same_path(launcher.parent, state_root / project):
            lane = standard.group(1)
            if lane_filter and lane_filter != lane:
                continue
            candidates += 1
            if candidates > 256:
                raise Refused("launcher inventory limit exceeded")
            try:
                card = store.load_card(project, lane)
            except (StoreError, protocol.ProtocolError):
                continue
            if card.host != "codex" or card.state != "done":
                continue
            try:
                assess(store, state_root, project, lane, launcher, process, processes)
            except (Refused, OSError, UnicodeError, protocol.ProtocolError, StoreError):
                try:
                    _orphan_shell_assess(
                        store, state_root, project, lane, launcher, process, processes
                    )
                except (Refused, OSError, UnicodeError, protocol.ProtocolError, StoreError) as exc:
                    rows.append(
                        {
                            "project": project,
                            "lane": lane,
                            "kind": "launcher-unverified",
                            "closeable": False,
                            "reason": (
                                "launcher-file-unverified"
                                if (
                                    isinstance(exc, UnicodeError)
                                    or not launcher.is_file()
                                    or is_link_or_reparse_point(launcher)
                                )
                                else "window-id-missing"
                                if not card.window
                                else "close-check-refused"
                            ),
                        }
                    )
                else:
                    rows.append(
                        {
                            "project": project,
                            "lane": lane,
                            "kind": "launcher-shell-unverified",
                            "closeable": False,
                        }
                    )
            else:
                rows.append(
                    {
                        "project": project,
                        "lane": lane,
                        "kind": "standard-closeable",
                        "closeable": True,
                    }
                )
            continue
        prefix = f"{project}-"
        if (
            not launcher.name.startswith(prefix)
            or not launcher.name.endswith(".ps1")
            or not _LEGACY_FOLDER.fullmatch(launcher.parent.name)
            or not _same_path(launcher.parent.parent, state_root)
        ):
            continue
        lane = launcher.name[len(prefix) : -4]
        if lane_filter and lane_filter != lane:
            continue
        candidates += 1
        if candidates > 256:
            raise Refused("launcher inventory limit exceeded")
        try:
            protocol.check_lane_id(lane, "lane")
            card = store.load_card(project, lane)
        except (StoreError, protocol.ProtocolError):
            continue
        if card.host != "codex" or card.state != "done":
            continue
        try:
            _legacy_assess(store, state_root, project, lane, launcher, process, processes)
        except (Refused, OSError, UnicodeError, protocol.ProtocolError, StoreError) as exc:
            reason = (
                "codex-child-not-observed"
                if isinstance(exc, Refused) and "no live Codex descendant" in str(exc)
                else "legacy-identity-unverified"
            )
            rows.append(
                {
                    "project": project,
                    "lane": lane,
                    "kind": "legacy-unverified",
                    "closeable": False,
                    "reason": reason,
                }
            )
        else:
            rows.append(
                {"project": project, "lane": lane, "kind": "legacy-live", "closeable": False}
            )
    cards, diagnostics = store.scan_cards(project)
    if diagnostics or len(cards) > 4096:
        raise Refused("done-card inventory is incomplete")
    reported_lanes = {str(row["lane"]) for row in rows}
    for card in cards:
        if (
            card.host != "codex"
            or card.state != "done"
            or not card.window
            or not _SESSION_ID.fullmatch(card.window)
            or card.lane in reported_lanes
            or (lane_filter and card.lane != lane_filter)
        ):
            continue
        pattern = re.compile(
            r"(?i)(?:^|\s)resume\s+['\"]?" + re.escape(card.window) + r"(?:['\"]|\s|$)"
        )
        if any(
            process.name.lower() in {"node.exe", "codex.exe"}
            and process.command
            and (process.name.lower() == "codex.exe" or "codex" in process.command.lower())
            and pattern.search(process.command)
            for process in processes
        ):
            rows.append(
                {
                    "project": project,
                    "lane": card.lane,
                    "kind": "session-process-unverified",
                    "closeable": False,
                    "reason": "no-verified-launcher",
                }
            )
    unique = {(str(row["lane"]), str(row["kind"])): row for row in rows}
    result: list[dict[str, str | bool]] = []
    for key in sorted(unique):
        try:
            current_card = store.load_card(project, key[0])
        except (StoreError, protocol.ProtocolError):
            continue
        if current_card.host == "codex" and current_card.state == "done":
            result.append(unique[key])
    return result


def _cards_safe(store: Store, project: str, lane: str) -> tuple[str, str]:
    card = store.load_card(project, lane)
    if (
        card.host != "codex"
        or card.state != "done"
        or not card.window
        or not _SESSION_ID.fullmatch(card.window)
    ):
        raise Refused("target card must be done, Codex, and have an exact window identity")
    cards_root = store.root / CARDS_DIR
    if is_link_or_reparse_point(cards_root):
        raise Refused("card directory is linked")
    if cards_root.exists():
        for folder in cards_root.iterdir():
            if not folder.is_dir() or is_link_or_reparse_point(folder):
                raise Refused("cannot inspect all project cards")
            try:
                protocol.check_project_id(folder.name, "project")
            except protocol.ProtocolError as exc:
                raise Refused("invalid project card directory") from exc
            cards, diagnostics = store.scan_cards(folder.name)
            if diagnostics:
                raise Refused("incomplete project card evidence")
            for other in cards:
                if (
                    (other.project, other.lane) != (project, lane)
                    and other.window == card.window
                    and other.state != "done"
                ):
                    raise Refused("another lane shares this window identity")
    return card.updated_utc, card.window


def _orphan_shell_assess(
    store: Store,
    state_root: Path,
    project: str,
    lane: str,
    launcher: Path,
    root: Process,
    processes: list[Process],
) -> None:
    """Inventory a generated launcher shell left by a done card with no window id."""
    card = store.load_card(project, lane)
    if card.host != "codex" or card.state != "done" or card.window:
        raise Refused("shell card is not a done Codex card without a window identity")
    if (
        not launcher.is_absolute()
        or not _same_path(launcher.parent, state_root / project)
        or not re.fullmatch(re.escape(lane) + r"-\d{8}T\d{6}Z\.ps1", launcher.name)
        or is_link_or_reparse_point(state_root)
        or is_link_or_reparse_point(launcher.parent)
        or is_link_or_reparse_point(launcher)
        or not launcher.is_file()
        or launcher.stat().st_size > 65536
    ):
        raise Refused("generated launcher path is missing, linked or unexpected")
    source = launcher.read_text(encoding="utf-8-sig")
    if not source.startswith("# Generated by the lane-open skill.") or "& codex -C " not in source:
        raise Refused("shell does not use a generated Codex launcher")
    if _utc(root.created) > _utc(card.updated_utc):
        raise Refused("shell started after card was marked done")
    matches = [p for p in processes if (arg := _launcher_arg(p)) and _same_path(arg, launcher)]
    if len(matches) != 1 or matches[0] != root:
        raise Refused("launcher shell process is missing or ambiguous")
    if len({p.pid for p in processes}) != len(processes):
        raise Refused("ambiguous process snapshot")
    children = [p for p in processes if p.parent == root.pid]
    if any(
        p.name.lower() != "conhost.exe"
        or _utc(p.created) < _utc(root.created)
        or any(other.parent == p.pid for other in processes)
        for p in children
    ):
        raise Refused("shell has an unexpected child process")
    if len(children) > 1:
        raise Refused("shell has multiple console hosts")


def assess(
    store: Store,
    state_root: Path,
    project: str,
    lane: str,
    launcher: Path,
    root: Process,
    processes: list[Process],
) -> list[Process]:
    """Return the exact process tree only when every close precondition is proved."""
    updated, _window = _cards_safe(store, project, lane)
    closed_at = _utc(updated)
    expected_folder = state_root / project
    if (
        not launcher.is_absolute()
        or os.path.normcase(str(launcher.parent)) != os.path.normcase(str(expected_folder))
        or not re.fullmatch(re.escape(lane) + r"-\d{8}T\d{6}Z\.ps1", launcher.name)
    ):
        raise Refused("launcher path does not identify this project and lane")
    if (
        is_link_or_reparse_point(state_root)
        or is_link_or_reparse_point(expected_folder)
        or is_link_or_reparse_point(launcher)
        or not launcher.is_file()
    ):
        raise Refused("launcher is missing or linked")
    if launcher.stat().st_size > 65536:
        raise Refused("launcher is unexpectedly large")
    launcher_text = launcher.read_text(encoding="utf-8-sig")
    if (
        not launcher_text.startswith("# Generated by the lane-open skill.")
        or "& codex -C " not in launcher_text
    ):
        raise Refused("launcher does not identify a generated Codex lane")
    matches = [p for p in processes if (arg := _launcher_arg(p)) and _same_path(arg, launcher)]
    if len(matches) != 1 or matches[0] != root:
        raise Refused("launcher process identity is missing or ambiguous")
    if closed_at < _utc(root.created):
        raise Refused("card was marked done before this launcher started")
    by_pid = {p.pid: p for p in processes}
    if len(by_pid) != len(processes):
        raise Refused("ambiguous process snapshot")
    ancestor = os.getpid()
    ancestors_seen: set[int] = set()
    while ancestor in by_pid:
        if ancestor in ancestors_seen:
            raise Refused("ambiguous invoking process ancestry")
        ancestors_seen.add(ancestor)
        if ancestor == root.pid:
            raise Refused("cannot close the invoking window")
        parent = by_pid[ancestor].parent
        ancestor = parent
    tree = [root]
    seen = {root.pid}
    host_children: dict[int, int] = {}
    console_children: dict[int, int] = {}
    while True:
        children = [p for p in processes if p.parent in seen and p.pid not in seen]
        if not children:
            break
        for child in children:
            parent_process = by_pid[child.parent]
            # The launcher and Codex code-mode host each have one leaf console host.
            if child.name.lower() == "conhost.exe" and (
                parent_process.pid == root.pid
                or parent_process.name.lower() == "codex-code-mode-host.exe"
            ):
                console_children[child.parent] = console_children.get(child.parent, 0) + 1
                if console_children[child.parent] > 1:
                    raise Refused("multiple console hosts share this process")
                if any(p.parent == child.pid for p in processes):
                    raise Refused("console host has an unexpected child process")
            elif child.name.lower() == "codex-code-mode-host.exe":
                executable = child.command.strip().strip('"')
                if (
                    parent_process.name.lower() != "codex.exe"
                    or not ntpath.isabs(executable)
                    or ntpath.basename(executable).lower() != "codex-code-mode-host.exe"
                ):
                    raise Refused("unknown or shared child process in launcher tree")
                host_children[child.parent] = host_children.get(child.parent, 0) + 1
                if host_children[child.parent] > 1:
                    raise Refused("multiple host processes share this launcher")
            elif child.name.lower() not in {"cmd.exe", "node.exe", "codex.exe"}:
                raise Refused("unknown or shared child process in launcher tree")
            elif not child.command or "codex" not in child.command.lower():
                raise Refused("child process is not identified as Codex")
            else:
                host_children[child.parent] = host_children.get(child.parent, 0) + 1
                if host_children[child.parent] > 1:
                    raise Refused("multiple host processes share this launcher")
            if _utc(child.created) < _utc(root.created):
                raise Refused("child process predates launcher")
            if _utc(child.created) > closed_at:
                raise Refused("host process started after the lane was marked done")
            tree.append(child)
            seen.add(child.pid)
    return tree


def _terminate_checked(tree: list[Process]) -> None:
    """Use process handles, so PID reuse between validation and termination is refused."""
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    handles: list[tuple[Process, int]] = []
    try:
        for process in tree:
            handle = kernel.OpenProcess(0x0001 | 0x1000, 0, process.pid)
            if not handle:
                raise Refused("process exited or cannot be opened; nothing closed")
            handles.append((process, handle))
            created = ctypes.c_ulonglong()
            exited = ctypes.c_ulonglong()
            kernel_time = ctypes.c_ulonglong()
            user_time = ctypes.c_ulonglong()
            if not kernel.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                raise Refused("cannot verify process creation time; nothing closed")
            unix_100ns = created.value - 116444736000000000
            actual = datetime.fromtimestamp(unix_100ns / 10_000_000, UTC)
            if abs((actual - _utc(process.created)).total_seconds()) > 0.001:
                raise Refused("process identity changed; nothing closed")
        for process, handle in reversed(handles):
            if not kernel.TerminateProcess(handle, 0):
                raise Refused(f"failed to close process {process.pid}; tree may be partly closed")
    finally:
        for _, handle in handles:
            kernel.CloseHandle(ctypes.c_void_p(handle))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--lane")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--public-list", action="store_true")
    parser.add_argument("--public-inventory", action="store_true")
    parser.add_argument("--close", action="store_true")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--created")
    parser.add_argument("--launcher", type=Path)
    args = parser.parse_args(argv)
    if sum((args.list, args.public_list, args.public_inventory, args.close)) != 1 or (
        args.close and not all((args.lane, args.pid, args.created, args.launcher))
    ):
        parser.error(
            "choose --list, --public-list, --public-inventory, or --close "
            "with lane, pid, created and launcher"
        )
    try:
        protocol.check_project_id(args.project, "project")
        if args.lane:
            protocol.check_lane_id(args.lane, "lane")
        store = Store.open()
        state_root = Path(os.environ["LOCALAPPDATA"]) / "passing-notes-lanes"
        processes = _snapshot()
        if args.public_inventory:
            for row in _public_inventory(store, state_root, args.project, processes, args.lane):
                print(json.dumps(row))
            return 0
        if args.list or args.public_list:
            folder = state_root / args.project
            if is_link_or_reparse_point(state_root) or is_link_or_reparse_point(folder):
                raise Refused("launcher directory is linked")
            for process in processes:
                launcher_arg = _launcher_arg(process)
                if not launcher_arg:
                    continue
                launcher = Path(launcher_arg)
                match = re.fullmatch(r"([a-z0-9]+(?:-[a-z0-9]+)*)-\d{8}T\d{6}Z\.ps1", launcher.name)
                if not match or not _same_path(launcher.parent, folder):
                    continue
                lane = match.group(1)
                if args.lane and lane != args.lane:
                    continue
                try:
                    tree = assess(
                        store, state_root, args.project, lane, launcher, process, processes
                    )
                    if args.public_list:
                        print(
                            json.dumps(
                                {
                                    "project": args.project,
                                    "lane": lane,
                                    "closeable": True,
                                }
                            )
                        )
                    else:
                        print(
                            json.dumps(
                                {
                                    "lane": lane,
                                    "pid": process.pid,
                                    "created": process.created,
                                    "launcher": str(launcher),
                                    "processes": len(tree),
                                    "closeable": True,
                                }
                            )
                        )
                except (Refused, OSError, UnicodeError) as exc:
                    if not args.public_list:
                        print(
                            json.dumps(
                                {
                                    "lane": lane,
                                    "pid": process.pid,
                                    "created": process.created,
                                    "launcher": str(launcher),
                                    "closeable": False,
                                    "reason": str(exc),
                                }
                            )
                        )
            return 0
        root = next((p for p in processes if p.pid == args.pid and p.created == args.created), None)
        if root is None:
            raise Refused("process PID and creation time no longer match")
        tree = assess(store, state_root, args.project, args.lane, args.launcher, root, processes)
        # A second snapshot catches a card/process change after the first inspection.
        fresh = _snapshot()
        current = next((p for p in fresh if p.pid == root.pid and p.created == root.created), None)
        if current is None or [
            (p.pid, p.created)
            for p in assess(
                store, state_root, args.project, args.lane, args.launcher, current, fresh
            )
        ] != [(p.pid, p.created) for p in tree]:
            raise Refused("process tree changed; nothing closed")
        _terminate_checked(tree)
        print(f"closed {args.project}/{args.lane} launcher pid {root.pid} ({len(tree)} processes)")
        return 0
    except (Refused, protocol.ProtocolError, StoreError, OSError, UnicodeError, KeyError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
