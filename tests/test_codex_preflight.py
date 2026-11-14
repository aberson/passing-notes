"""Launch evidence must not turn missing configuration into a permissions claim."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "codex_preflight",
    Path(__file__).resolve().parents[1] / "skills/lane-open/scripts/codex-preflight.py",
)
assert spec is not None and spec.loader is not None
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)
summarize = preflight.summarize


def test_unknown_defaults_and_parent_trust_are_not_assumed(tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    result = summarize({"projects": {str(tmp_path): {"trust_level": "trusted"}}}, cwd, cwd)
    assert result["workspace_trust"] == "unknown"
    assert result["sandbox_mode"] == "host-default (unverified)"
    assert result["runtime_write_verified"] is False


def test_report_keeps_modes_distinct_and_excludes_unrelated_config(tmp_path: Path) -> None:
    result = summarize(
        {
            "projects": {str(tmp_path): {"trust_level": "trusted"}},
            "sandbox_mode": "workspace-write",
            "approval_policy": "on-request",
            "approvals_reviewer": "auto_review",
            "sandbox_workspace_write": {"writable_roots": [str(tmp_path / "store")]},
            "mcp_servers": {"private": {"secret": "synthetic-secret"}},
        },
        tmp_path,
        tmp_path,
    )
    assert result["workspace_trust"] == "trusted"
    assert result["approval_policy"] == "on-request"
    assert result["approvals_reviewer"] == "auto_review"
    assert "synthetic-secret" not in str(result)
    assert result["writable_roots"] == [str(tmp_path / "store")]
