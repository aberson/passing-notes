---
name: lane-handoff
description: Claude provider entry point for lane-handoff; loads the canonical shared core.
user-invocable: true
---

# lane-handoff - Claude entry point

See [../core.md](../core.md) for the full specification.

## Provider-specific instructions

- Load the core in full before acting.
- Call `lane-close` and `lane-open` through the Skill tool and follow their cores. Never re-implement their steps, and never launch a window except through `lane-open`'s support script.
- Run `passing-notes` commands with the Bash or PowerShell tool as the real user. No escalation step applies.
- Each wait is one bounded loop. Never use Monitor, ScheduleWakeup or repeated wake-ups for it.

## Output normalization

Return only the core's operator-facing report. Preserve its locked strings (the `Result:` values and report line labels) exactly.

## Unsupported capabilities

If a required Claude host tool is unavailable, use the core's documented fallback; otherwise halt visibly with `required_tool_missing`. Never weaken a core gate.
