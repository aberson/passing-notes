# lane-open - GPT entry point

See [../core.md](../core.md) for the full specification.

## GPT invocation differences

- Load the core in full before acting. Use structured tool/action calls for filesystem and shell operations.
- The launch must run as the real Windows user on the operator's machine. A host that cannot run a local shell command there returns `required_tool_missing` and opens nothing.
- Normalize GPT verbosity: do not reveal hidden chain-of-thought. Return only required decisions, evidence, commands and the core's report.

## Known GPT limitations

- Claude Artifact actions, Claude session JSONL/scratchpad paths, and Claude-only deep links are unavailable. This skill needs none of them.
- On timeout, rate limit, provider 5xx, parse failure, or a missing required adapter, return the router reason code and stop. Never relaunch a lane window.

## Output normalization

Return only the core-defined operator-facing report. Preserve locked strings, paths and command ordering exactly; reject missing required fields instead of inferring success.
