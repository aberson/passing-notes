# coordinator-handover - Codex entry point

See [../core.md](../core.md) for the full specification.

## Codex invocation differences

- Load the core in full before acting and follow it verbatim. Use Codex's shell tool for every command. This wrapper only maps host abstractions; it never weakens a gate the core defines.
- Load and follow `lane-status`, `lane-close`, `lane-handoff` and `lane-open` as named skills. Never re-implement their steps.
- This window's identity is `$env:CODEX_THREAD_ID`. Every `card set` this window runs carries `--host codex --window $env:CODEX_THREAD_ID`, and every bind uses `--host codex --session $env:CODEX_THREAD_ID`.
- **Escalation** follows the named skills' Codex wrappers, and nothing more: `passing-notes wake send`, plus `lane-open`'s launch script and `claude agents --json`. Request each with its justification. Every other `passing-notes` command runs inside the sandbox, with the store root as a writable folder (`--add-dir "$env:LOCALAPPDATA\passing-notes"`).
- A busy Codex window cannot take doorbells: they queue until its turn ends. End the turn at every stage that waits on another window.
- Normalize verbosity: do not reveal hidden chain-of-thought. Return only required decisions, evidence, commands and the core's report.

## Known Codex limitations

- Claude Artifact actions, Claude session JSONL/scratchpad paths, the Claude Agent/Workflow tools, and Claude-only deep links are unavailable. This skill needs none of them.
- On timeout, rate limit, provider 5xx, parse failure, or a missing required adapter, preserve and report the actual error and stop. Do not retry implicitly, and never open or close a lane window outside the core's steps.

## Output normalization

Return only the core-defined operator-facing report. Preserve locked strings, paths and command ordering exactly; reject missing required fields instead of inferring success.
