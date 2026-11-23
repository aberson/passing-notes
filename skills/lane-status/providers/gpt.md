# lane-status - GPT entry point

See [../core.md](../core.md) for the full specification.

## GPT invocation differences

- Load the core in full before acting. Use structured shell tool/action calls for the
  one status command and, when coordinator attention is effective (the default) or with
  `--sweep`, the core's bounded read-only plan and inventory commands.
- Pass plan paths and public labels as structured arguments where available, or with
  native shell literal quoting; never interpolate them into executable shell text.
- The command must run as the real Windows user on the operator's machine. A host that cannot run a local shell command there returns `required_tool_missing` and changes nothing.
- Invoking this skill does not make the window a lane: set no card and bind no doorbell.
- Normalize GPT verbosity: do not reveal hidden chain-of-thought. Return only the core's report.

## Known GPT limitations

- Claude Artifact actions, Claude session JSONL/scratchpad paths, and Claude-only deep links are unavailable. This skill needs none of them.
- On timeout, rate limit, provider 5xx, parse failure, or a missing required adapter, return the router reason code and stop.

## Output normalization

Return only the core-defined report. Preserve locked strings and a `status-out-of-scope` error line verbatim; reject output with a missing or unknown `schema` instead of inferring a table.
