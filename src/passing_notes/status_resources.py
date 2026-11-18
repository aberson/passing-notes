"""Cheap, read-only resource observations for the status view."""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from passing_notes import queue
from passing_notes.store import Diagnostic, Store, StoreError

MAX_QUEUE_ENTRIES = 1024


@dataclass(frozen=True)
class Holder:
    project: str
    resource: str
    lane: str
    deadline_utc: str


@dataclass(frozen=True)
class Resources:
    free_memory_bytes: int | None = None
    queue_holders: tuple[Holder, ...] = ()
    queues_complete: bool = True


class _MemoryStatus(ctypes.Structure):
    # Windows MEMORYSTATUSEX: two DWORDs followed by seven DWORDLONGs.
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("load", ctypes.c_uint32),
        ("total_phys", ctypes.c_uint64),
        ("avail_phys", ctypes.c_uint64),
        ("total_page_file", ctypes.c_uint64),
        ("avail_page_file", ctypes.c_uint64),
        ("total_virtual", ctypes.c_uint64),
        ("avail_virtual", ctypes.c_uint64),
        ("avail_extended_virtual", ctypes.c_uint64),
    ]


def _windows_free_memory() -> int | None:
    state = _MemoryStatus()
    state.length = ctypes.sizeof(state)
    query = ctypes.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx
    query.argtypes = [ctypes.POINTER(_MemoryStatus)]
    query.restype = ctypes.c_int
    return int(state.avail_phys) if query(ctypes.byref(state)) else None


def free_memory_bytes() -> int | None:
    """Available physical bytes, or unknown; no subprocess, retries or settings writes."""
    try:
        if sys.platform == "win32":
            return _windows_free_memory()
        if sys.platform.startswith("linux"):
            with Path("/proc/meminfo").open("rb") as stream:
                data = stream.read(8192)
            for line in data.splitlines():
                fields = line.split()
                if len(fields) == 3 and fields[0] == b"MemAvailable:" and fields[2] == b"kB":
                    value = int(fields[1])
                    return value * 1024 if value >= 0 else None
    except (OSError, AttributeError, ValueError, OverflowError):
        pass
    return None


def collect(store: Store, projects: Sequence[str]) -> tuple[Resources, list[Diagnostic]]:
    """Reuse the queue reader, retaining only holders in the selected projects."""
    holders: list[Holder] = []
    diagnostics: list[Diagnostic] = []
    for project in projects:
        try:
            view = queue.show(store, project, max_entries=MAX_QUEUE_ENTRIES)
        except StoreError:
            diagnostics.append(Diagnostic(f"queues/{project}", "unavailable; holders incomplete"))
            continue
        diagnostics.extend(view.diagnostics)
        holders.extend(
            Holder(project, record.resource, record.holder.lane, record.holder.deadline_utc)
            for record in view.records
            if record.holder is not None
        )
    return Resources(free_memory_bytes(), tuple(holders), not diagnostics), diagnostics
