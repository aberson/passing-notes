# lane-open - Codex entry point

See [../core.md](../core.md) for the full specification.

## Codex invocation differences

- Load the core in full before acting and follow it verbatim. Use Codex's shell tool for every command. This wrapper only maps host abstractions; it never weakens a gate the core defines.
- **Escalation is required for two things, and only these.** Codex's sandbox user cannot start a window as the operator and cannot see Claude sessions. So request escalated permissions, with that justification, for:
  - the support-script launch (core step 4);
  - `claude agents --json` (core steps 1 and 5).

  Run every `passing-notes` command (`card show`, `inbox`, `post`, `wake show`) inside the sandbox as usual; the store root is a writable folder there (`--add-dir`). Never widen the sandbox for anything else. If escalation is denied, report `Result: NOT OPENED (escalation denied)` and launch nothing.
- A Codex lane opened by this skill gets `--add-dir <store root>` from the script, so it can write its card and notes from inside its own sandbox.
- Waking applies to Claude lanes only. A Codex lane's report shows `session:` and `wake:` as `not applicable (codex lane)`.
- Normalize verbosity: do not reveal hidden chain-of-thought. Return only required decisions, evidence, commands and the core's report.

## Known Codex limitations

- Claude Artifact actions, Claude session JSONL/scratchpad paths, the Claude Agent/Workflow tools, and Claude-only deep links are unavailable. This skill needs none of them.
- On timeout, rate limit, provider 5xx, parse failure, or a missing required adapter, preserve and report the actual error and stop. Do not retry implicitly, and never relaunch a lane window.

## Output normalization

Return only the core-defined operator-facing report. Preserve locked strings, paths and command ordering exactly; reject missing required fields instead of inferring success.
