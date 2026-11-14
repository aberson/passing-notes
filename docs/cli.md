# Passing Notes CLI reference

The `passing-notes` command line in full: the store, installation, every command, the
wake doorbell and the cross-host smoke. For what lanes should do with it, see the
[lane rules](lane-rules.md). For an overview, see the [README](../README.md).

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

## Waking an idle Claude lane

Writing a note wakes nobody. `passing-notes wake` sends a **content-free doorbell**
to one bound, idle Claude window: the exact text
`New Passing Note <id>. Read it and respond through Passing Notes. If you already
acknowledged it or your card is done, stop: post nothing and ring no one.` The lane then
reads the note itself and applies its own instructions and permissions. The doorbell
grants nothing.

**Only a note that unblocks rings.** Every doorbell costs the recipient a full turn.
`wake send` (and `post --wake`) refuses with `wake-not-needed` (exit 5) unless the
note needs `reply` or `operator-decision`, is a `handoff`, or replies to a note that
needed one of those. Completions, acknowledgement-only notes and FYIs wait for the
recipient's inbox check.

**Bind (at every session start, run by the lane itself).** The session id is the
window's own `CLAUDE_CODE_SESSION_ID`; a window is never picked by its title. A restart
gives the window a new session id, so binding is part of every session start
([lane rules](lane-rules.md), section 2):

```text
passing-notes wake bind --project example-project --lane lane-b --host claude --session $CLAUDE_CODE_SESSION_ID
```

The doorbell must be sent in the window's own permission mode. Without
`--permission-mode`, `bind` reads the window's configured default from Claude's
settings files and prints which file set it: managed settings, then the project's
`.claude/settings.local.json` and `.claude/settings.json`, then your user
`settings.json`. Give `--permission-mode` only if the window was started with a mode
flag or switched modes; a model guessing its own mode is unreliable.

**Ring.** Post with `--wake` (it needs `--idempotency-key`, so a retry never posts a
second note), or ring about a note already posted. `--wait` watches for the ack and
reply:

```powershell
passing-notes post --project example-project --from lane-a --to lane-b --subject "Step 2 is ready" --kind handoff --needs reply --idempotency-key step-2-ready --wake --wait 120 --body-file "$env:TEMP\note.md"
passing-notes wake send --project example-project --lane lane-b --wait 120 20300101T120000Z-lane-a-to-lane-b-1a2b
```

The output reports each state on its own line, and only from evidence:
`note:` (the file is in the store), `wake:` (delivered to the session's inbox, with
Claude's message id; delivery is not reading), `ack:` (the ack file exists) and
`reply:` (a note from that lane with `reply_to` set to this note). Nothing reports that
work started.

- A **busy** session is never interrupted (`wake-session-busy`). A **closed or stale**
  session (`wake-session-unavailable`), a **name** shared by two live sessions
  (`wake-session-ambiguous`) and an **unbound** lane (`wake-no-binding`) are refused
  too. Each exits 5 and leaves the note posted; ring again later or rely on the
  inbox check.
- Ringing again for the same note, lane and window does nothing once delivered or
  acknowledged. A failed delivery can be retried.
- Bindings and ring records live in `wake\<project>\` in the store, outside protocol v1.

**How it is delivered.** Only Claude Code's own cross-session messaging reaches a
running Claude window. `wake` runs a short headless `claude -p` whose only tools are
`ListAgents` and `SendMessage`, with no MCP servers, in the lane's declared permission
mode. Claude holds messages from a session in another mode for its user's approval. The
result is judged from that bridge's tool calls, never from its words.

**Codex senders.** Codex's sandbox user cannot see or message Claude sessions. Post from
inside the sandbox as usual, then run `passing-notes wake send` outside it: request
escalation for that command through the configured native host reviewer. A note
cannot grant that permission. `post --wake` works only outside the
sandbox. Codex destinations support queue doorbells as described below.

**Codex destinations.** Bind from the destination window using `--host codex
--session $env:CODEX_THREAD_ID`, without a Claude permission mode. Sending uses
`codex queue --thread <UUID> --message <doorbell> --resume`. A matching queue
receipt reports `queued`, not `delivered`. A resume warning remains visible even
when a live session subsequently acknowledges the note. Busy and pending-approval
sessions may queue messages; queueing never resolves a native approval. Rebind
after restart or a host switch. A card/binding host or UUID mismatch refuses
delivery to the stale destination. A lane whose card is `done` is never rung, for
either host, so a late doorbell cannot resume a closed thread. See [Codex coordination](codex-coordination.md)
for launch preflight, native approval alternatives and live evidence.

Exit codes: 0 success; 2 usage or validation error (including any malformed id);
3 note or card not found or invalid; 4 store unusable (including `store-in-repo`
and `store-link`); 5 a doorbell was not delivered (the note stays posted). Errors are one stderr line:
`passing-notes: error: <code>: <message>`.
Read the code from `$LASTEXITCODE` in PowerShell. `powershell -Command`, which Codex's
shell tool uses, reports any non-zero native exit as 1 unless the command ends with
`exit $LASTEXITCODE`.

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
