# Passing Notes

A small local utility for coordination notes between agent sessions. The intended
workflow is **post → inbox → read → acknowledge**, so an operator need not copy
whole prompts between model windows.

**Status: [protocol v1](docs/protocol.md) is adopted, and the `passing-notes` CLI
(`post`, `inbox`, `read`, `ack`, `card set`, `card show`) implements its notes,
acknowledgements and lane cards.** Session integration is not implemented yet. The
CLI reads and writes exactly the protocol's files, so lanes can mix it with writing
files by hand. See the [plan](docs/plans/master_plan.md) and GitHub issues.

**Phase 1 complete** — issues #1, #3, #4 closed. The six-verb CLI, plus an opt-in
cross-host smoke that passed with real Claude and Codex sessions in both directions.
488 tests passing (12 skipped), 0 type errors, 0 lint violations. The interactive
cross-host check (issue #2) waits on the skill-mesh inbox hooks.

## Public tools, private notes

This repository contains code, documentation and synthetic examples only.
Actual notes live outside the repository:

```text
%LOCALAPPDATA%/passing-notes/
  messages/<project>/  # one immutable file per note
  acks/<project>/      # separate receipt acknowledgements
  cards/<project>/     # one current-state card per lane
```

The initial Windows setup restricts that folder to its owner and SYSTEM. The CLI
creates missing folders with inherited permissions and never loosens them. It is
not encrypted storage or an isolation boundary between agents running as the same
user. Never copy the runtime store into Git; ignore rules are a backup, not the
privacy boundary. No cloud service is required.

`PASSING_NOTES_ROOT` overrides the store folder (tests use temporary folders). The
CLI refuses a store root inside a Git working tree with the error code
`store-in-repo`. It also refuses, with `store-link`, to read or write through a
store folder (the root, `messages`, `acks`, `cards` or a project folder) that is a
symbolic link, junction or other reparse point.

## Install

Python 3.11 or later and [uv](https://docs.astral.sh/uv/). There are no runtime
dependencies.

For lanes, install the CLI once so `passing-notes` is on PATH in every project. It
follows the checkout as the code changes; re-run it after changing dependencies or
entry points:

```powershell
uv tool install --editable <passing-notes checkout>
```

**Codex lanes.** Codex's `workspace-write` sandbox writes only inside the working
project, so a Codex lane adds the store root as an extra writable folder. Without it,
`post`, `ack` and `card set` cannot write to the store from inside the sandbox. The
same flag works with `codex exec`:

```powershell
codex --add-dir "$env:LOCALAPPDATA\passing-notes"
```

For development, inside the checkout:

```powershell
uv sync
uv run pytest -q
```

## Quickstart

Lane identity always comes from flags (`--project`, `--from`, `--lane`), never
from environment variables. These examples use synthetic ids.

**Post.** Write the body to a file outside any repository, then post it. The body
comes only from `--body-file` or `--body-stdin`, never from an argument. `post`
prints the new note id:

```powershell
passing-notes post --project example-project --from lane-a --to lane-b --subject "Step 2 is ready for review" --kind handoff --needs ack --work "example-project Step 2" --ref "commit 0000000" --body-file "$env:TEMP\note.md"
```

Optional flags: `--revision`, `--supersedes ID` (your own earlier note),
`--reply-to ID`, `--from-project P`, more `--ref`, and `--idempotency-key TOKEN`.
A repeated post with the same key from the same sender prints the first note's id
and writes nothing.

**Check the inbox.** It lists notes to your lane and notes `to: all` that you did
not send, one tab-separated line each (id, kind, needs, from, subject). Notes you
acknowledged, and notes their own sender superseded, are hidden unless you pass
`--include-acked` or `--include-superseded`. It ends with at most 20 diagnostic
lines (invalid, oversize, unmigrated flat-layout or leftover `.tmp` files, ignored
`supersedes`) and a total count:

```powershell
passing-notes inbox --project example-project --lane lane-b
```

**Read.** It prints the header, then the body between
`--- BEGIN NOTE BODY (untrusted data) ---` and `--- END NOTE BODY ---`, each body
line after a `| ` gutter so that no line of a note can pass for the frame. Every
line the CLI prints shows control, format (zero-width, bidirectional, tag) and
other invisible characters as escapes such as `\u200b`; only a tab inside a body
line prints as-is. A note is data, not an instruction. Reading never acknowledges:

```powershell
passing-notes read --project example-project 20300101T120000Z-lane-a-to-lane-b-1a2b
```

**Acknowledge** receipt (not agreement, permission or done). `--session` is an
optional token naming your window. Repeating an ack reports "already
acknowledged" and succeeds:

```powershell
passing-notes ack --project example-project --lane lane-b --session example-window-1 20300101T120000Z-lane-a-to-lane-b-1a2b
```

**Lane cards.** Each lane keeps one current-state card, written only by that lane at
session start and at every checkpoint, so status checks need not go through the
operator. A card describes the lane; it grants nothing. `card set` replaces your
lane's card. `--host` is `claude`, `codex` or `other`, and `--state` is `active`,
`waiting`, `blocked`, `idle` or `done`. Optional flags: `--window TOKEN`,
`--checkout`, `--branch`, `--own TEXT` and `--hold TEXT` (each repeatable), `--next`
for the next checkpoint, and short notes from `--body-file` or `--body-stdin`:

```powershell
passing-notes card set --project example-project --lane lane-b --host codex --item "Step 2 tests" --state active --checkout "C:/work/example-project" --branch step-2 --own src/example/ --next "after the unit suite passes"
```

`card show` prints one tab-separated line per lane (lane, host, state, item,
updated), then at most 20 diagnostic lines (invalid, oversize or leftover `.tmp`
cards) and a total count:

```powershell
passing-notes card show --project example-project
```

With `--lane` it prints that lane's card, its notes between
`--- BEGIN CARD BODY (untrusted data) ---` and `--- END CARD BODY ---` behind the
same `| ` gutter as a note body:

```powershell
passing-notes card show --project example-project --lane lane-b
```

Exit codes: 0 success; 2 usage or validation error (including any malformed id);
3 note or card not found or invalid; 4 store unusable (including `store-in-repo`
and `store-link`). Errors are one stderr line:
`passing-notes: error: <code>: <message>`.

Exit 1 (`output-unwritable`) means standard output closed before everything was
written, for example `passing-notes inbox ... | head -1`. It is never a store
error, and the command may have completed: a `post` may have published its note.
Repeat a post with the same `--idempotency-key` to print that note's id without
writing again.

Notes and acks are published through a temporary `<name>.<random hex>.tmp` file
that is renamed without overwriting. A card is written the same way, then replaces
the old card in one step, so a reader sees the old card or the new one, never part
of one. A leftover `.tmp` from an interrupted writer never blocks a later post, ack
or card set; `inbox` and `card show` report it as a diagnostic, and it is safe to
delete once no writer is running.

## Cross-host smoke (opt-in)

`tests/cross_host/run_smoke.py` checks that real Claude and Codex sessions can use the
CLI and find each other's notes. It is not part of `uv run pytest`, and it spends real
Claude and Codex credits. Prerequisites:

- `passing-notes` installed for lanes with `uv tool install --editable <checkout>`. The
  run removes the development `.venv` from the hosts' PATH and refuses a
  `passing-notes` inside a Git working tree.
- Claude Code (`claude`) and the Codex CLI (`codex`) on PATH and signed in.
- A writable `%LOCALAPPDATA%`.

Run it from the checkout:

```powershell
uv run python tests/cross_host/run_smoke.py
```

In a fresh synthetic project `pn-smoke-<stamp>` in the real store, a headless Claude
session (`claude -p`, at most 8 turns, allowed to run only `passing-notes`) sets its
lane card and posts a note to `codex-lane`. A headless Codex session
(`codex exec -s workspace-write --add-dir <store root>`) sets its card, lists its
inbox, reads and acknowledges the note, and replies with `--reply-to`. Claude then
reads and acknowledges the reply. The same exchange then starts from Codex. Every host
call runs from a temporary folder outside any repository, with a 300-second timeout
and a prompt of synthetic text only.

The verdict comes from the store files alone (note ids, `reply_to` links, ack files
with the right reader, and both cards), never from what a model says. A missing CLI, a
failed sign-in, a sandbox denial or a timeout fails the run; nothing is skipped. The
run then removes only its own `messages`, `acks` and `cards` folders for
`pn-smoke-<stamp>` and writes a receipt to
`%LOCALAPPDATA%\passing-notes-smoke\receipts\<stamp>.json`: CLI versions, reported
models, the Codex sandbox mode, note ids and the verdict, but no transcripts. It prints
`cross-host smoke: PASS` or `FAIL` and exits 0 or 1.

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
