---
name: lane-open
description: Claude provider entry point for lane-open; loads the canonical shared core.
user-invocable: true
---

# lane-open - Claude entry point

See [../core.md](../core.md) for the full specification.

## Provider-specific instructions

- Load the core in full before acting.
- Run the support script and the `passing-notes`/`claude agents` commands with the Bash or PowerShell tool as the real user. Claude Code runs them outside any sandbox, so no escalation step applies.
- The invoking window's own `CLAUDE*` variables must never reach the new lane. The support script removes them inside the new window; never launch the lane by any other route (no `claude --bg`, no Agent or Workflow tool, no direct `claude` call from the Bash tool).
- Seed work (core step 2) is done in this session with ordinary file reads. Do not delegate it to a sub-agent unless the brief is large enough to need one.
- The verification wait (core step 5) is one bounded loop of at most 180 seconds. Never use Monitor, ScheduleWakeup or repeated wake-ups for it.

## Output normalization

Return only the core's operator-facing report. Preserve its locked strings (`Result: OPENED`, `Result: NOT OPENED (<reason>)`, and the `window:`, `session:`, `card:` and `wake:` lines) exactly.

## Unsupported capabilities

If a required Claude host tool is unavailable, use the core's documented fallback; otherwise halt visibly with `required_tool_missing`. Never weaken a core gate.
