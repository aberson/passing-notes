"""Read-only native memory adapters: bounded reads and unknown on failure."""

from __future__ import annotations

import ctypes
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from passing_notes import status_resources


@pytest.mark.parametrize("success", [True, False])
def test_windows_memory_layout_and_api_result(
    monkeypatch: pytest.MonkeyPatch, success: bool
) -> None:
    class Query:
        def __init__(self) -> None:
            self.argtypes: list[object] = []
            self.restype: object = None

        def __call__(self, pointer: Any) -> int:
            state = pointer._obj
            assert state.length == ctypes.sizeof(state) == 64
            state.avail_phys = 3 * 2**30
            return int(success)

    query = Query()
    monkeypatch.setattr(status_resources, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *args, **kwargs: SimpleNamespace(GlobalMemoryStatusEx=query),
        raising=False,
    )
    assert status_resources.free_memory_bytes() == (3 * 2**30 if success else None)
    assert query.argtypes == [ctypes.POINTER(status_resources._MemoryStatus)]
    assert query.restype is ctypes.c_int


@pytest.mark.parametrize(
    "content,expected",
    [
        (b"MemFree: 32 kB\nMemAvailable: 2048 kB\n", 2**21),
        (b"MemAvailable: 0 kB\n", 0),
        (b"MemAvailable: -1 kB\n", None),
        (b"MemAvailable: invalid kB\n", None),
        (b"MemFree: 2048 kB\n", None),
        (b"MemAvailable: 2048 bytes\n", None),
        (b"x" * 8192 + b"\nMemAvailable: 2048 kB\n", None),
    ],
)
def test_linux_memory_read_is_bounded_and_requires_available_metric(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    expected: int | None,
) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_bytes(content)
    monkeypatch.setattr(status_resources, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(status_resources, "Path", lambda path: meminfo)
    assert status_resources.free_memory_bytes() == expected


def test_memory_failure_or_unsupported_platform_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> int | None:
        raise OSError("synthetic unavailable API")

    monkeypatch.setattr(status_resources, "_windows_free_memory", unavailable)
    monkeypatch.setattr(status_resources, "sys", SimpleNamespace(platform="win32"))
    assert status_resources.free_memory_bytes() is None
    monkeypatch.setattr(status_resources, "sys", SimpleNamespace(platform="unsupported"))
    assert status_resources.free_memory_bytes() is None
