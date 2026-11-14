"""Read Codex launch configuration through its supported app-server interface.

No model turn, trust mutation, approval response, or transcript read. Run before
opening a window; this report is configuration evidence, not a sandbox write test.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


def read_config(cwd: Path, overrides: list[str]) -> dict[str, Any]:
    executable = shutil.which("codex")
    if not executable:
        raise ValueError("codex is not on PATH")
    argv = [executable, "app-server", "--listen", "stdio://"]
    for override in overrides:
        argv.extend(["-c", override])
    with subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        cwd=cwd,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        incoming: queue.Queue[str | None] = queue.Queue()

        def receive() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                incoming.put(line)
            incoming.put(None)

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()

        def request(ident: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert process.stdin is not None
            payload = {"id": ident, "method": method, "params": params}
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + 20
            while True:
                line = incoming.get(timeout=max(0.001, deadline - time.monotonic()))
                if line is None:
                    raise ValueError("Codex app-server exited before replying")
                result = json.loads(line)
                if result.get("id") == ident:
                    if "error" in result:
                        raise ValueError(f"{method} failed: {result['error']}")
                    value = result.get("result")
                    if not isinstance(value, dict):
                        raise ValueError(f"{method} returned no result object")
                    return value
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{method} timed out")

        try:
            request(1, "initialize", {"clientInfo": {"name": "pn_preflight", "version": "1"}})
            process.stdin.write('{"method":"initialized","params":{}}\n')
            process.stdin.flush()
            result = request(2, "config/read", {"cwd": str(cwd), "includeLayers": False})
            config = result.get("config")
            if not isinstance(config, dict):
                raise TypeError("config/read returned no config object")
            return config
        finally:
            # EOF shuts down the server behind npm's Windows launcher too;
            # terminating only the shim leaves its child holding stdout open.
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=2)


def summarize(config: dict[str, Any], cwd: Path, project_root: Path) -> dict[str, Any]:
    """Expose only launch-relevant fields; never dump a user's full configuration."""
    projects = config.get("projects") or {}
    trust = "unknown"
    # Exact working directory and repository root only; do not assume that trust
    # in an arbitrary parent implies trust in this repository.
    for candidate in (cwd, project_root):
        for name, entry in projects.items():
            if os.path.normcase(os.path.abspath(name)) == os.path.normcase(str(candidate)):
                trust = entry.get("trust_level", "unknown")
                break
        if trust != "unknown":
            break
    sandbox = config.get("sandbox_mode")
    policy = config.get("approval_policy")
    reviewer = config.get("approvals_reviewer")
    return {
        "workspace_trust": trust,
        "sandbox_mode": sandbox or "host-default (unverified)",
        "approval_policy": policy or "host-default (unverified)",
        "approvals_reviewer": reviewer or "host-default (unverified)",
        "permission_profile": config.get("default_permissions"),
        "writable_roots": (config.get("sandbox_workspace_write") or {}).get("writable_roots", []),
        "native_approval_forwarding": "not provided by codex queue",
        "runtime_write_verified": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("-c", "--config", action="append", default=[])
    args = parser.parse_args()
    cwd = args.cwd.resolve()
    try:
        root = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        project_root = Path(root.stdout.strip()).resolve() if root.returncode == 0 else cwd
        print(json.dumps(summarize(read_config(cwd, args.config), cwd, project_root)))
        return 0
    except (OSError, ValueError, TypeError, queue.Empty, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"preflight": "failed", "reason": str(exc) or "response timed out"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
