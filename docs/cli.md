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
  queues/<project>/    # one turn order per shared resource
```

The initial Windows setup restricts that folder to its owner and SYSTEM. The CLI
creates missing folders with inherited permissions and never loosens them. It is
not encrypted storage or an isolation boundary between agents running as the same
user. Never copy the runtime store into Git; ignore rules are a backup, not the
privacy boundary. No cloud service is required.

`PASSING_NOTES_ROOT` overrides the store folder (tests use temporary folders). The
CLI refuses a store root inside a Git working tree with the error code
`store-in-repo`. It also refuses, with `store-link`, to read or write through a
store folder (the root, `messages`, `acks`, `cards`, `queues` or a project folder)
that is a symbolic link, junction or other reparse point.

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

Two optional read-only views change only what `inbox` prints; they write nothing to
the store. Without them, `inbox` prints exactly what is described above.

- `--stale-after MINUTES` (a whole number, at least 1; anything else is a usage error,
  exit 2) flags a listed note as `stale` when it needs `reply` and its `created_utc`
  is more than MINUTES minutes ago. The flag joins the line's trailing state column,
  comma-separated in the order `acked`, `superseded`, `stale`. Other needs are never
  stale.
- `--summary` prints one summary line instead of the note lines, then the
  diagnostic lines and total count exactly as above. A host hook parses this line,
  so its format is stable: space-separated `key=value`, every key always present, in
  this order:

  `inbox-summary: count=<n> oldest_age_min=<m|-> stale=<k|->`

  `count` is the number of notes the listing would show with the same flags
  (`--include-acked` and `--include-superseded` count too). `oldest_age_min` is the
  whole minutes, rounded down and never negative, since the oldest listed note's
  `created_utc`, or `-` when `count` is 0. `stale` is the number of listed notes
  flagged stale by `--stale-after`, or `-` without it.

```powershell
passing-notes inbox --project example-project --lane lane-b --summary --stale-after 30
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

`card set` prints `card set: <project>/<lane> at <time>`. When the card it set is not
`done`, it then prints every `card show --overlaps` line (below) that involves this
lane, in the same format and order, so a lane sees a clash at the moment it claims a
path. This is advisory: it refuses nothing, and with no overlap the output is that one
line, as before. It fails open: if the cards cannot be scanned, it prints nothing more
and the exit code is unchanged, and an unreadable card of another lane is skipped.

`card show` prints one tab-separated line per lane (lane, host, state, item,
updated), then at most 20 diagnostic lines (invalid, oversize or leftover `.tmp`
cards) and a total count:

```powershell
passing-notes card show --project example-project
```

`--overlaps` is a read-only view of that list; it writes nothing. After the card
lines and before the diagnostics it prints one tab-separated line per overlapping
pair, `overlap`, lane-a, lane-b, entry-a, entry-b, where entry-a is one of lane-a's
`owns` or `holds` entries and entry-b one of lane-b's. Cards whose state is `done` are
left out. Two entries overlap when, compared without regard to case and with
backslashes read as `/`, repeated `/` collapsed, `./` and `/./` segments dropped and a
trailing `/` removed, they are equal or one is a path prefix of the other at a `/`
boundary: `src/` overlaps `src/passing_notes/cli.py`, `Src/x`, `./src/a` and `src//a`,
and `C:\A\b` overlaps `c:/a/b/c`, but `src` does not overlap `srcx`. `..` is not
resolved, and an entry that is not a path matches only the same text. The `checkout`
field is not compared: two lanes in different checkouts that own the same path still
overlap, because their changes still collide when they merge. Lines show the entries
as written on the cards. Lines are sorted by lane-a, lane-b, then the entries, with
lane-a before lane-b and each pair once. Without `--overlaps` the output is unchanged.
It applies only to the list: with `--lane` it is a usage error (exit 2).

```powershell
passing-notes card show --project example-project --overlaps
```

With `--lane` it prints that lane's card, its notes between
`--- BEGIN CARD BODY (untrusted data) ---` and `--- END CARD BODY ---` behind the
same `| ` gutter as a note body:

```powershell
passing-notes card show --project example-project --lane lane-b
```

## Taking turns on a shared resource

When several lanes share one resource (an engine lock, a play checkout, a GPU), they
take turns through its queue instead of announcing turns in notes. A queue only
orders turns: a turn grants nothing, assigns no work, and is never the project's own
lock. Take the project's lock by its own rules once your turn comes; the CLI never
reads, takes or deletes a project's lock files. The record format is in the
[protocol](protocol.md#resource-queues). A resource id has the lane id grammar (at
most 32 chars, such as `engine` or `play-checkout`); `con` or `a--b` is a usage error
(exit 2).

**Reserve.** On a free resource you take the turn at once; otherwise you join the end
of the line. `--minutes N` (a whole number, at least 1) sets your turn's length,
default 60 minutes from when the turn is granted; a waiter's applies from its
promotion. `--reason TEXT` is one line of at most 200 characters, with no control,
zero-width or other invisible character (exit 2 otherwise):

```powershell
passing-notes reserve engine --project example-project --lane lane-a --minutes 40 --reason "Step 2 soak"
```

Every queue line is tab-separated and starts with its kind, so a project's lock script
can parse it:

- `holder<TAB><resource><TAB><lane><TAB><granted_utc><TAB><deadline_utc>`, or
  `holder<TAB><resource><TAB>-` when nobody holds the resource;
- `waiter<TAB><resource><TAB><n><TAB><lane><TAB><since_utc>`, where `n` is the
  place in line, from 1.

`reserve` prints the `holder` line, and, when you joined the line, your own `waiter`
line after it. Repeating it while you hold or wait prints the same lines, says
`already ...; nothing written` on stderr, writes nothing and exits 0. A line already
20 waiters long is refused with `queue-full` (exit 2).

**Leave the line** with `--cancel`. It prints the queue's lines afterwards (as
`queue show <resource>` does, without diagnostics). When you are not in the line it
prints `nothing to cancel` and exits 0. The holder is refused with
`holder-cannot-cancel` (exit 2) and must `release` instead:

```powershell
passing-notes reserve engine --project example-project --lane lane-c --cancel
```

**Release** ends your turn. The first waiter becomes the holder at once, with its turn
granted now by you, and `release` prints the new `holder` line, or
`holder<TAB><resource><TAB>-` when nobody was waiting. A waiter whose card is `done`
is dropped from the line in the same change, with one `skipped <lane>: card is done`
line after the `holder` line; it is never told or rung, and the next waiter is tried.
If every waiter is skipped the resource becomes free. A waiter with no card is
promoted, and the wake path decides at ring time; one whose card is invalid is
promoted as not done, with a warning on stderr.

The new holder is then told, after the queue lock is released: `release` posts one
reservation note from you to it and rings it through the ordinary wake path, as
`wake send` does, and prints that ring's `note:`, `wake:`, `ack:` and `reply:` lines
after the queue lines. The note has `kind: reservation`, `needs: reply`, the subject
`Your turn on <resource>`, `work: <resource>`, `reply_to: none` and the idempotency
key `turn-<resource>-<grant_seq>`, the turn's own number, so each turn has at most one
note. Its body is fixed text written by the CLI (resource, project, turn number and
deadline, and how to answer: `taken <resource>` or `passing <resource>`, see
[lane rules](lane-rules.md)); no `--reason` text goes into it. Only the new holder is
rung. Waiters further back get nothing, and a turn taken on a free resource gets no
note. When the doorbell is not delivered (for example the new holder has no wake
binding), the promotion and the note stand, `release` prints `wake: not delivered` and
the error line, and exits 5; the lane sees the note at its next inbox check.

```powershell
passing-notes release engine --project example-project --lane lane-a
```

```text
holder<TAB>engine<TAB>lane-b<TAB>2030-01-01T12:10:00Z<TAB>2030-01-01T12:40:00Z
note: <id> is in the store
wake: delivered to session <uuid> (<window name>) inbox, message <msg id>; delivery is not reading
ack: not observed
reply: not observed
```

A lane that is not in the queue gets `nothing to release`, exit 0, and nothing is
written, so a repeated release is harmless. One case differs: if that lane granted the
current turn (the record names it as `granted_by`) and the holder has no reservation
note from it with that turn's key, a crash came between the promotion and the post,
so `release` prints the `holder` line, says on stderr that the note is missing, and
posts and rings it now. Once the note exists, a retry posts and rings nothing. A
waiter is refused with `not-holder` (exit 2) and leaves with `reserve --cancel`
instead.

**Show** prints, for each resource sorted by name (or only the one named), its
`holder` line, then its `waiter` lines in order, then at most 20 diagnostic lines and
a total count. It writes nothing. A resource named but never reserved shows as free:

```powershell
passing-notes queue show --project example-project
passing-notes queue show engine --project example-project
```

**Damaged records.** A record that is not valid JSON of the documented shape, is over
8 KiB, or is a folder or anything but a regular file is never acted on: `reserve`,
`release` and `--cancel` exit 3 with `queue-invalid` and leave its bytes unchanged,
and `queue show` reports it as a diagnostic instead of its lines. A leftover `.tmp`
file is reported by `queue show`, never read.

**Concurrent changes** are safe: each change holds the resource's `<resource>.lock`
only while it reads and replaces the record, so several lanes reserving at once each
land in the line exactly once. A lock older than 30 seconds is left by a writer that
died and is removed. A lock that stays fresh for longer than that is reported as
`queue-busy` (exit 4); try again.

**Closing lanes.** The `lane-close` skill releases every resource the closing lane
holds and runs `reserve --cancel` for every line it waits in (found with `queue show`),
and `lane-handoff` lists those entries for the successor. Lane skills changed with this
behavior, so reinstall them for both hosts and restart their sessions:

```powershell
uv run python tools/skills_install.py install --host claude
uv run python tools/skills_install.py install --host codex
```

## Waking an idle Claude lane

Writing a note wakes nobody. `passing-notes wake` sends a **content-free doorbell**
to one bound, idle Claude window: the exact text
`New Passing Note <id>. Read it and respond through Passing Notes. Acknowledging this
doorbell is not the deliverable: a finished item always posts its own handoff note. If you
already acknowledged this note or your card is done, stop: post nothing more about it and
ring no one.` The lane then reads the note itself and applies its own instructions and
permissions. The doorbell grants nothing. Acknowledging a doorbell is never the
deliverable: a lane that finishes an item always posts that item's own handoff note.

**Only a note that unblocks rings.** Every doorbell costs the recipient a full turn.
A note rings only if it needs `reply` or `operator-decision`, is a `handoff`, or
replies to a note that needed one of those. Completions, acknowledgement-only notes
and FYIs wait for the recipient's inbox check. `post --wake` checks this before
anything is stored: an ineligible note is refused with `wake-not-needed` (exit 2),
and no note is written. When the note it answers (`--reply-to`) is not in the store,
the refusal says so. `post --wake-if-needed` instead posts the note either way and
rings only an eligible one: an ineligible note is stored, its id and `note:` line are
printed, then `wake: not rung (<why>); the lane sees it at its next inbox check`, and
it exits 0. Use plain `--wake` when you must be told that a ring is impossible.
`wake send` about a note already posted refuses with
`wake-not-needed` (exit 5), and the note stays posted. Only the note's own
eligibility is checked before posting. The recipient's binding and card belong to
that lane and can change between the post and the ring (the lane rebinds or closes,
and a retry may ring much later), so they are checked when the doorbell is sent.

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
reply. Repeating `post --wake` with a key already used prints the stored note's id,
writes nothing and rings that note.

`--wake-if-needed` takes the same `--idempotency-key`, single-lane `--to` and `--wait`
rules, and cannot be combined with `--wake` (exit 2). An eligible note behaves exactly
as with `--wake`, including exit 5 when the doorbell is not delivered. An ineligible one
is posted with the `wake: not rung` line and exit 0, and any `--wait` is ignored,
because nothing was sent to watch. A retry with the same key writes nothing: it prints
the stored id and, for an ineligible note, the `not rung` line again with exit 0.

Make the key unique per distinct note, for example from the date and time
(`lane-a-20261115T1405Z`) or from the commit the note is about (`handoff-<sha>`). A
genuine retry then reuses the key, and a new note never collides with an old one: a
reused key would silently ring the old note instead of posting the new one.

```powershell
passing-notes post --project example-project --from lane-a --to lane-b --subject "Step 2 is ready" --kind handoff --needs reply --idempotency-key lane-a-20261115T1405Z --wake --wait 120 --body-file "$env:TEMP\note.md"
passing-notes wake send --project example-project --lane lane-b --wait 120 20300101T120000Z-lane-a-to-lane-b-1a2b
passing-notes post --project example-project --from lane-a --to lane-b --subject "Docs merged" --kind fyi --needs ack --idempotency-key lane-a-20261115T1410Z --wake-if-needed --body-file "$env:TEMP\note.md"
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
cannot grant that permission. `post --wake`, and `--wake-if-needed` for a note that rings, work only outside the
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

Exit codes: 0 success (including a `post --wake-if-needed` note that was posted
and not rung, and a queue no-op such as `nothing to release`); 2 usage or
validation error (including any malformed id, `--wake` together with
`--wake-if-needed`, a `post --wake` note that may not ring, refused before it is
stored, and a refused queue change: `not-holder`, `holder-cannot-cancel`,
`queue-full`); 3 note or card not found or invalid, or a damaged queue record
(`queue-invalid`); 4 store unusable (including `store-in-repo`, `store-link` and
`queue-busy`); 5 a doorbell was not delivered (the note stays posted; for `release`,
the new holder's turn stands too). Errors are
one stderr line: `passing-notes: error: <code>: <message>`.
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
