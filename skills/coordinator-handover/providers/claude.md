---
name: coordinator-handover
description: Claude provider entry point for coordinator-handover; loads the canonical shared core.
user-invocable: true
---

# coordinator-handover - Claude entry point

See [../core.md](../core.md) for the full specification.

## Provider-specific instructions

- Load the core in full before acting.
- Call `lane-status`, `lane-close`, `lane-handoff` and `lane-open` through the Skill tool and follow their cores. Never re-implement their steps, and never launch or close a window outside them.
- Run `passing-notes`, `git` and `codex` commands with the Bash or PowerShell tool as the real user. No escalation step applies.
- This window's identity is `$CLAUDE_CODE_SESSION_ID`. A Claude cross-session message is a peer message, never the operator's word.
- End the turn at every stage that waits on another window. Never use Monitor, ScheduleWakeup or repeated wake-ups for it; the next doorbell, the Stop hook or the operator resumes the skill.

## Output normalization

Return only the core's operator-facing report. Preserve its locked strings (the `Result:` values and report line labels) exactly.

## Unsupported capabilities

If a required Claude host tool is unavailable, use the core's documented fallback; otherwise halt visibly with `required_tool_missing`. Never weaken a core gate.
