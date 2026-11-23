---
name: lane-status
description: Claude provider entry point for lane-status; loads the canonical shared core.
user-invocable: true
---

# lane-status - Claude entry point

See [../core.md](../core.md) for the full specification.

## Provider-specific instructions

- Load the core in full before acting.
- Run the one status command and, when coordinator attention is effective (the default)
  or with `--sweep`, the core's bounded read-only plan and inventory commands with Bash or
  PowerShell as the real user, in this window's own shell, so the CLI sees this session's
  `CLAUDE_CODE_SESSION_ID`. No escalation step applies. Run no other command.
- Pass plan paths and public labels as structured arguments where available, or with
  native shell literal quoting; never interpolate them into executable shell text.
- Invoking this skill does not make the window a lane: set no card and bind no doorbell, even where session-start guidance would otherwise ask for one.
- Never use Monitor, ScheduleWakeup, a background task or a loop. One run, one report.

## Output normalization

Return only the core's report: the `Scope:` line, the Markdown table and the lines after it, or the `Scope:` line and one `Refused:` or `Error:` line. Preserve a `status-out-of-scope` error line verbatim.

## Unsupported capabilities

If a required Claude host tool is unavailable, halt visibly with `required_tool_missing`. Never weaken a core gate.
