# Passing Notes

A small local utility for coordination notes between agent sessions. The intended
workflow is **post → inbox → read → acknowledge**, so an operator need not copy
whole prompts between model windows.

**Status: project scaffold and approved scope; CLI and session integration are
not implemented yet.** See the [plan](docs/plans/master_plan.md) and GitHub issues.

## Public tools, private notes

This repository contains code, documentation and synthetic examples only.
Actual notes live outside the repository:

```text
%LOCALAPPDATA%/passing-notes/
  messages/  # one immutable file per note
  acks/      # separate receipt acknowledgements
```

The initial Windows setup restricts that folder to its owner and SYSTEM. It is
not encrypted storage or an isolation boundary between agents running as the same
user. Never copy the runtime store into Git; ignore rules are a backup, not the
privacy boundary. No cloud service is required.

## Planned stack and setup

Python standard library, local files and a CLI. Implementation will document the
supported Python version and exact installation commands. There is no runnable
installation yet.

1. Clone this public repository for development.
2. Read [the approved scope](docs/plans/master_plan.md).
3. Keep all real runtime data outside the checkout; use temporary directories and
   synthetic messages for tests.

## Boundaries

- Reading a note and acknowledging it are separate actions.
- Acknowledgement means receipt, not agreement or permission to execute.
- Agents check at startup, checkpoints and handoff; no background polling or wakeups.
- Canonical project plans remain authoritative. Notes link to sources and revisions.
- No dashboard, agent launcher, network server or execution queue in v1.

Reuse sibling tools: [heads-up](https://github.com/aberson/heads-up) for work claims,
[find-again](https://github.com/aberson/find-again) for retrieval,
[paper-trail](https://github.com/aberson/paper-trail) for decisions and
[same-page](https://github.com/aberson/same-page) for status contradictions.
