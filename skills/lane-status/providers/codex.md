# lane-status - Codex entry point

See [../core.md](../core.md) for the full specification.

## Codex invocation differences

- Load the core in full before acting and follow it verbatim. Use Codex's shell tool for the core's one command, `passing-notes status ... --json`, run once inside the sandbox. It only reads the store, so it needs no escalation. Run no other command.
- The CLI matches this window's `CODEX_THREAD_ID` (or `CODEX_SESSION_ID` fallback) against its live Codex bindings for `--mine` and visibility. Do not set, clear or fake any identity variable to change scope.
- Invoking this skill does not make the window a lane: set no card and bind no doorbell.
- Normalize verbosity: do not reveal hidden chain-of-thought. Return only the core's report.

## Known Codex limitations

- Claude Artifact actions, Claude session JSONL/scratchpad paths, the Claude Agent/Workflow tools, and Claude-only deep links are unavailable. This skill needs none of them.
- On timeout, rate limit, provider 5xx, parse failure, or a missing required adapter, preserve and report the actual error and stop. Do not retry implicitly.

## Output normalization

Return only the core-defined report. Preserve locked strings and a `status-out-of-scope` error line verbatim; reject output with a missing or unknown `schema` instead of inferring a table.
