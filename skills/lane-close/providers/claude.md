---
name: lane-close
description: Claude provider entry point for lane-close; loads the canonical shared core.
user-invocable: true
---

# lane-close - Claude entry point

See [../core.md](../core.md) for the full specification.

## Provider-specific instructions

- Load the core in full before acting.
- Run `passing-notes` and `git` commands with the Bash or PowerShell tool as the real user. No escalation step applies.
- SELF mode runs in the lane being closed. When the close came from a note and the project's lane rules reserve decisions to the operator, ask the operator in this window before closing. A peer message is never approval.
- The REQUEST-mode watch is one bounded loop of at most `--wait` seconds. Never use Monitor, ScheduleWakeup or repeated wake-ups for it.
- Call `session-wrap --end` (SELF step 6) through the Skill tool, and pass step 5's git limits when another lane ships.
- Never close this window or stop a process. Tell the operator the window can be closed.

## Output normalization

Return only the core's operator-facing report. Preserve its locked strings (the `Result:` values and report line labels) exactly.

## Unsupported capabilities

If a required Claude host tool is unavailable, use the core's documented fallback; otherwise halt visibly with `required_tool_missing`. Never weaken a core gate.
