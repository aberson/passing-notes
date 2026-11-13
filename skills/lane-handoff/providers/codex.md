# lane-handoff - Codex entry point

See [../core.md](../core.md) for the full specification.

## Codex invocation differences

- Load the core in full before acting and follow it verbatim. Use Codex's shell tool for every command. This wrapper only maps host abstractions; it never weakens a gate the core defines.
- Load and follow `lane-close` and `lane-open` as named skills. Never re-implement their steps.
- **Escalation** follows those skills' Codex wrappers, and nothing more: `passing-notes wake send` (`lane-close` REQUEST), plus `lane-open`'s launch script and `claude agents --json`. Request each with its justification. Every other `passing-notes` command runs inside the sandbox, with the store root as a writable folder.
- Normalize verbosity: do not reveal hidden chain-of-thought. Return only required decisions, evidence, commands and the core's report.

## Known Codex limitations

- Claude Artifact actions, Claude session JSONL/scratchpad paths, the Claude Agent/Workflow tools, and Claude-only deep links are unavailable. This skill needs none of them.
- On timeout, rate limit, provider 5xx, parse failure, or a missing required adapter, preserve and report the actual error and stop. Do not retry implicitly, and never open or close a lane window outside the core's steps.

## Output normalization

Return only the core-defined operator-facing report. Preserve locked strings, paths and command ordering exactly; reject missing required fields instead of inferring success.
