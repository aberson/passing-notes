# Passing Notes

## 1. What This Is

Status: protocol v1 adopted 2026-11-11 ([docs/protocol.md](../protocol.md)). Lanes
already use its v0 subject by hand. Steps 1-3 (CLI, lane cards, cross-host smoke)
are built (2026-11-12). The wake doorbell, lane lifecycle skills, ledgered installer
and shared lane rules shipped on 2026-11-13 (see the Wake and lane-skills phase
record below). Step 4 (the real Claude and Codex check) follows the skill-mesh
inbox hooks and remains pending.

**Goal.** Agents working in different local sessions, Claude and Codex alike,
can leave a short coordination note, find the notes addressed to their project
lane, and explicitly acknowledge them. The operator should not have to copy
whole prompts between model windows. Working across both Claude and Codex is a
core requirement: it is why Claude's built-in cross-session messaging is not
enough. A **lane** is a named, lasting slice of one project's work. One window
(Claude or Codex) works it at a time, and the lane outlives that window.

Public repository: code, documentation, tests and synthetic examples only.
Private runtime root: `%LOCALAPPDATA%/passing-notes` on Windows, outside all
repositories. Messages and acknowledgements never become public project data.

Progress: [docs/build-progress.html](../build-progress.html).
Review decisions: [docs/plan-review/passing-notes-review.html](../plan-review/passing-notes-review.html).

Proposal: docs/plan-redline/passing-notes-proposal.html

## Selected execution route

**Operator, 2026-11-11 (P5):** build Step 1 now as its own small `/build-phase`
run in this repository. This replaces the 2026-10-20 route (P3), which queued
the build behind another workspace project's acceptance pipeline: the first real
Swarm Forge build of the Ambition app. That pipeline must pick a different first
real job, and its plan owner records the swap. Its later step that waits on
"Passing Notes issue #1 plus a real note exchange" can use the CLI once Step 1
lands. The skill-mesh v3 decision sheet (item 55), which the operator approved
the same night, records the same answer. The protocol already lets lanes
coordinate by hand, so no lane waits on the CLI.

**Operator, 2026-11-11 23:45 PST:** approved the skill-mesh v3 decision sheet's
lane items, recorded verbatim in that review's README. Their effect here:
- protocol v1 note fields built into Step 1 (item 54, P13);
- lane cards (item 57, P14);
- content-free doorbells (item 60, P15);
- the operator board belongs to Observatory (item 62, P16).

Build order: Step 1 (CLI), then Step 2 (lane cards), then Step 3 (cross-host
smoke), then Step 4 (operator check, after skill-mesh ships the inbox hooks).

## First version

Python standard-library CLI with `post`, `inbox`, `read`, and `ack`.
One immutable file per message, separate acknowledgement records, no shared
append-only board that concurrent agents must rewrite. Use unique IDs and atomic
publication. Read does not implicitly acknowledge. Retrying an acknowledgement
must not create conflicting state. Invalid or incomplete files produce a bounded
diagnostic rather than disappearing silently.

A note records its ID, creation time, sender, recipient project and lane,
subject, body, source references and revisions, and requested response. An
acknowledgement identifies the note, reader and time. Acknowledgement means
receipt, not agreement, execution authority, implementation completion or user
acceptance. Treat note content as untrusted input and reconcile it with the
current operator instruction.

Read message text from an explicit file or stdin; do not require private content
in shell arguments. Bound file sizes. Validate IDs before resolving filenames and
keep reads/writes within the runtime root. Do not silently scan other projects,
inboxes, credentials or model transcripts.

**Format, IDs, ack key, addressing, lane cards, limits and store layout are
fixed by [protocol v1](../protocol.md).** The CLI reads and writes exactly that
format, including hand-written v0 files. Summary below; the protocol is
authoritative.

`Note` shape: file `messages\<project>\<id>.md`, at most 16 KiB. A header between
`---` lines, then a Markdown body.

| field | type | note |
|---|---|---|
| id | `<yyyymmddTHHMMSSZ>-<from>-to-<to>-<4 lowercase hex>` | Equals the file stem; e.g. `20300101T120000Z-lane-a-to-lane-b-1a2b` |
| created_utc | `YYYY-MM-DDTHH:MM:SSZ` | Within 60 s of the id's stamp |
| project, from_project | project id `[a-z0-9]+(-[a-z0-9]+)*`, at most 32 chars | Recipient project (= folder); optional sender project |
| from, to | lane id, same grammar, no `to` segment | `to` may also be `all` or `operator` |
| subject | one line, 1-160 chars | Must not start with a quote or bracket |
| kind | `handoff`, `change-notice`, `question`, `answer`, `blocker`, `commit-request`, `reservation`, `operator-relay`, `fyi` | Required by `post`; optional when read |
| needs | `reply`, `ack`, `fyi`, `operator-decision` | Requested response |
| work, revision | one line, at most 200 / 80 chars | Optional |
| supersedes, reply_to | note id (`reply_to` may be `none`) | `supersedes` counts only from the same sender |
| idempotency_key | token `[A-Za-z0-9._:-]+`, at most 64 chars | Optional |
| refs | list of at most 20 items of at most 300 chars | Commits, branches, paths |

`Ack` shape: file `acks\<project>\<note id>--<reader>.md`, at most 1 KiB. It
holds the lines `reader: <lane>`, `read_utc: <UTC>`, an optional
`session: <token>`, and finally `received`.

`Card` shape: file `cards\<project>\<lane>.md`, at most 8 KiB, replaced on each
update.

| field | type | note |
|---|---|---|
| lane, project | lane id, project id | Equal the file stem and the folder |
| host | `claude`, `codex`, `other` | |
| window | token | Optional |
| checkout, branch | one line, at most 300 / 100 chars | Optional |
| owns, holds | lists of at most 20 items of at most 300 chars | Owned paths; shared resources held |
| item | one line, at most 200 chars | Current work item |
| state | `active`, `waiting`, `blocked`, `idle`, `done` | |
| next_checkpoint | one line, at most 200 chars | Optional |
| updated_utc | `YYYY-MM-DDTHH:MM:SSZ` | |

Recipients are durable lanes within a project, not sessions. The reconciliation
below records why.

## Platform and toolchain

- **Runtime:** Python 3.11 or later, standard library only. Native Windows only
  in v1; WSL lanes are out of scope (D7).
- **Package manager:** uv. Development-only tools: pytest, ruff and mypy (P10, D18).
- **Install for development:** `uv sync`
- **Install for lanes (P9):** `uv tool install --editable <passing-notes checkout>`
  puts `passing-notes` on PATH for Claude and Codex sessions in any project. It
  follows the checkout as the code changes; re-run it after changing dependencies
  or entry points.
- **Run:** `passing-notes ...` once installed, or `uv run passing-notes ...`
  inside the checkout.
- **Test:** `uv run pytest -q` (pytest is the development-only test runner, D18)
- **Lint:** `uv run ruff check .` and `uv run ruff format --check .`
- **Typecheck:** `uv run mypy` (strict, over `src`, `tests` and `tools`)
- **Build:** none beyond the editable tool install; no published package in v1.
- **Cross-host smoke (Step 3):** `uv run python tests/cross_host/run_smoke.py`

## CLI contract (D5)

- `passing-notes post --project P --from LANE --to RCPT --subject TEXT --kind K
  --needs N [--work TEXT] [--revision TEXT] [--supersedes ID] [--reply-to ID]
  [--idempotency-key TOKEN] [--ref TEXT]... [--from-project P2]
  (--body-file PATH | --body-stdin)` prints the new note id. If the same sender
  already posted a note with that idempotency key in the project, it prints that
  note's id, writes nothing, and exits 0.
- `passing-notes inbox --project P --lane LANE [--include-acked]
  [--include-superseded]` prints one tab-separated line per note: id, kind, needs,
  from, subject. Notes superseded by their own sender are hidden by default. It then
  prints at most 20 diagnostic lines plus a total count, covering invalid,
  oversize, unmigrated (flat layout) or leftover `.tmp` files and ignored
  cross-sender `supersedes`.
- `passing-notes read --project P ID` prints the header, then the body between
  `--- BEGIN NOTE BODY (untrusted data) ---` and `--- END NOTE BODY ---` lines,
  each body line behind a `| ` gutter so no body line can print as a frame line
  (D10). Characters that can hide or reorder text are escaped on every output
  path. It never writes an ack.
- `passing-notes ack --project P --lane LANE [--session TOKEN] ID` writes the
  ack. If the ack already exists, it reports "already acknowledged" and succeeds.
  It refuses (exit 2, `not-addressed`) a note the lane is not a recipient of: a
  note to another lane or to `operator`, or a `to: all` note the lane sent itself.
- `post --supersedes ID` refuses (exit 2) a target that is missing or was sent by a
  different sender, so a lane can never hide another lane's note.
- `passing-notes card set --project P --lane LANE --host H --item TEXT --state S
  [--window TOKEN] [--checkout TEXT] [--branch TEXT] [--own TEXT]... [--hold TEXT]...
  [--next TEXT] [--body-file PATH | --body-stdin]` replaces that lane's card. It
  writes `<lane>.md.tmp` exclusively, then replaces the card.
- `passing-notes card show --project P [--lane LANE]` prints one card, or one line
  per lane (lane, host, state, item, updated), followed by bounded diagnostics for
  invalid cards.
- `passing-notes --version` prints the package version.
- Exit codes: 0 success; 1 output unwritable (`output-unwritable`, e.g. a closed
  stdout pipe; never reported as a store error); 2 usage or validation error
  (including an unreadable `--body-stdin`); 3 note or card not found or invalid; 4 store unusable
  (including `store-in-repo` and `store-link`).
- Store root: `%LOCALAPPDATA%\passing-notes`, overridable by
  `PASSING_NOTES_ROOT` (tests use temporary directories). Startup refuses a root
  inside a Git working tree, with the stable error code `store-in-repo`.
- Missing store folders are created with permissions inherited from
  `%LOCALAPPDATA%`. The CLI never loosens permissions (D8), and it refuses to read
  or write through a store folder that is a symlink, junction or other reparse
  point (`store-link`, exit 4).
- Publication (D9): create a per-attempt temporary file `<final name>.<8 random
  hex>.tmp` exclusively, write it, then rename it to the final name without
  overwriting (a crashed attempt's leftover never blocks a later one). On a clash, pick new hex, up to
  5 attempts, then exit 4. Acks are published the same way.
- Body text comes only from a file or stdin, never from an argument.
- Lane identity is always explicit (`--project`, `--lane`/`--from`). It is never
  derived from a host's session variables: hosts expose different session
  identifiers (or none), and borrowing another host's variable would misattribute
  acks.
- The CLI is host-neutral: Claude and Codex lanes run the same commands against
  the same store.
- **Wake extension:** `wake bind`, `wake send`, `wake show`, and `post --wake`
  add an opt-in doorbell for idle, bound Claude sessions. Exit 5 means the wake
  was not delivered; an already-posted note is kept. Full flags, binding fields
  and delivery states are documented in [the CLI reference](../cli.md).
- **Coordinated-lanes views (2026-11-15):** `post --wake` refuses a note
  `ring_reason` would not ring before storing it (exit 2, `wake-not-needed`);
  `post --wake-if-needed` posts and rings only an eligible note.
  `inbox --summary [--stale-after MIN]` prints one pinned `inbox-summary:` line;
  `card show --overlaps` and `card set` print tab-separated `overlap` rows. The
  [CLI reference](../cli.md) is the authority for these and later flags.
- **Codex lanes (P8):** Codex's workspace-write sandbox writes only inside the
  working project. Codex lanes add the store root as an extra writable directory
  with `--add-dir %LOCALAPPDATA%\passing-notes` (verified 2026-11-12, codex-cli
  0.167.0: a `workspace-write` session could not write to a folder under
  `%LOCALAPPDATA%` without it and could with it). Step 3 uses the same flag and
  documents it in the README.

## Build order

### Step 1: Local private message CLI
- **Status:** DONE (2026-11-12)
- **Type:** code
- **Issue:** #1
- **Problem:** Lanes exchange notes by hand-writing protocol files. Nothing
  validates IDs, bounds or no-overwrite publication. Malformed or stray files are
  only noticed by eye.
- **Flags:** --reviewers deep --max-iter 12
- **Files:** `pyproject.toml` (extends the committed scaffold: requires-python >= 3.11;
  no runtime dependencies; development group with pytest, ruff and mypy;
  `passing-notes` console script);
  `src/passing_notes/__init__.py`, `src/passing_notes/__main__.py`,
  `src/passing_notes/cli.py`, `src/passing_notes/protocol.py` (the single source
  of the grammar, field set and limits), `src/passing_notes/store.py` (root
  resolution, `store-in-repo` guard, exclusive publication);
  `tests/test_protocol.py`, `tests/test_store.py`, `tests/test_cli.py`,
  `tests/test_protocol_doc.py`, `tests/conftest.py` (shared temporary-store
  fixtures); `tests/test_package.py` (the scaffold's tautological version test,
  replaced by a check that the package version matches the installed metadata); synthetic `tests/fixtures/`; `.gitattributes` (keeps the CRLF
  fixtures byte-exact); `README.md` quickstart; `docs/protocol.md`
  (clarifications found in review, each stating a rule the CLI enforces: Windows
  reserved device names are invalid ids; temporary names may carry a random
  suffix before `.tmp`; characters that can hide or reorder text are refused in
  one-line values and escaped when printed, with tab printed as-is in body
  lines).
- **Produces:** post/inbox/read/ack over protocol v1 notes and acks (including
  `kind`, `work`, `revision`, `supersedes`, `idempotency_key` and the ack
  `session` line), with private default storage and clear errors, runnable as
  `passing-notes`.
- **Done when:**
  - Two separate local processes exchange and acknowledge a synthetic note
    through the console entry point.
  - Every v1 field is validated against the protocol's rules and limits. A
    repeated post with the same idempotency key from the same sender returns the
    first note's id and writes nothing. A same-sender `supersedes` hides the old
    note from the inbox. A cross-sender `supersedes` is ignored and reported. An
    ack with `--session` records the line.
  - Recipient filtering works, and `to: all` excludes the sender.
  - Concurrent publication from two processes preserves both notes, and a
    repeated ack is a successful no-op.
  - Malformed note, project and lane ids cannot escape the store: traversal
    attempts exit 2.
  - A root inside a Git working tree is refused with `store-in-repo` (exit 4).
  - Synthetic hand-written v0 files are read unchanged (CRLF, blank lines in
    acks, unknown header keys).
  - Flat-layout, oversize, invalid and leftover `.tmp` files produce at most 20
    diagnostic lines plus a count.
  - `read` frames the body as untrusted data.
  - `tests/test_protocol_doc.py` parses the example note and ack in
    `docs/protocol.md` and checks its Limits table against the constants of the
    same names in `protocol.py` (D11).
  - ruff and mypy report zero findings, and the full unit suite passes.
  - No runtime file is written inside the repository (tests use temporary roots
    through `PASSING_NOTES_ROOT`).
- **Pilot evidence (format only):** see the reconciliation below. It does not
  exercise the CLI, concurrency, ID rejection or any Codex lane.

<!-- autofix-applied: 2026-11-12 -->
### Step 2: Lane cards
- **Status:** DONE (2026-11-12)
- **Type:** code
- **Issue:** #3
- **Problem:** A lane's current state (host, checkout, owned paths, held
  resources, current item) is only known by asking the operator. Protocol v1
  defines a per-lane card, but nothing writes or validates cards.
- **Flags:** --reviewers deep
- **Files:** `src/passing_notes/protocol.py` (card grammar, fields, limits, parse
  and render; the single source), `src/passing_notes/store.py` (card read and
  replace through the existing link checks and temporary-file helpers),
  `src/passing_notes/cli.py` (the `card set` and `card show` verbs),
  `tests/test_cards.py`, `tests/conftest.py` (shared CLI, store and race
  fixtures moved here), `tests/test_cli.py` and `tests/test_store.py` (their
  fixtures moved to conftest), `tests/test_protocol_doc.py` (card example and
  limits), `README.md` (cards
  section), `docs/protocol.md` (card limit names in the Limits table and the
  replace-step wording). No separate `cards.py`: it would only re-export
  protocol and store code.
- **Produces:** `passing-notes card set` and `card show` over
  `cards\<project>\<lane>.md`, following protocol v1's Lane cards section.
- **Done when:**
  - `card set` validates every field against the protocol's rules and limits.
    It writes only `cards\<project>\<lane>.md`, through an exclusive `.tmp` and a
    replace.
  - Two concurrent `card set` calls for the same lane leave one complete,
    valid card, never a partial one.
  - `card show` prints a single card, or one line per lane, plus bounded
    diagnostics for invalid cards.
  - The doc-parity test also covers the card example in `docs/protocol.md`.
  - ruff and mypy report zero findings, and the full unit suite passes.
- **Depends on:** 1

<!-- autofix-applied: 2026-11-12 -->
### Step 3: Cross-host smoke with real Claude and Codex sessions
- **Status:** DONE (2026-11-12)
- **Result (2026-11-12):** the first attempt stopped on a signed-out standalone
  `claude` CLI; after the operator signed in, the resumed run passed review-deep
  in one round. Real smoke run `pn-smoke-20261112t171349z` passed 6/6 phases in
  both directions (Claude Code 2.1.311, codex-cli 0.167.0 in `workspace-write`
  with the store root added). Unit suite: 488 passed, 12 skipped.
- **Type:** code
- **Issue:** #4
- **Problem:** The manual pilot ran only Claude lanes. Nothing shows that a Codex
  session can reach the store, post, read, acknowledge and write its card, or
  that the two hosts interoperate through the CLI.
- **Flags:** --reviewers deep
- **Files:** `tests/cross_host/run_smoke.py` (driver, outside default pytest
  collection), `tests/cross_host/check_store.py` (store-only checker),
  `tests/test_cross_host_checker.py` (checker calibration in the default suite),
  `README.md` (smoke prerequisites and the Codex writable-root line).
- **Produces:** an opt-in smoke that drives headless `claude -p` and `codex exec`
  sessions through the installed CLI in both directions, plus a receipt per run.
- **Smoke contract (P7, P8, P12; spending Claude and Codex credits approved by
  the operator 2026-11-11):**
  - **Setup:** a fresh synthetic project `pn-smoke-<stamp>` in the real default
    store root. `<stamp>` is the run's start time in UTC, written as
    `yyyymmddthhmmssz` in lowercase (e.g. `pn-smoke-20261112t081500z`). The run
    uses the CLI installed with
    `uv tool install --editable`. That exercises the real path, the real install
    and each host's write permission.
  - **Claude to Codex:** a headless Claude session posts a note to lane
    `codex-lane`. A headless Codex session lists its inbox, reads the note, acks
    it and posts a reply with `reply_to`. A Claude session then reads the reply
    and acks it.
  - **Codex to Claude:** the same exchange, started by Codex.
  - **Cards:** each host also sets its own lane card once, with `host` set to
    `claude` or `codex`.
  - **Verdict from the store only:** note ids, `reply_to` links, ack files with
    the right reader, and both cards. Never from what either model says it did.
  - **Both hosts are required.** A missing CLI, failed auth or a sandbox denial
    fails the run. It never counts as a skip.
  - **Limits:** 300-second timeout per host call; Claude turn cap 8. Host CLIs
    are called with argument lists, never a shell string. Prompts contain only
    synthetic text.
  - **Codex sandbox:** the run uses the writable-root setting that lanes use (P8),
    passed for the run, and records the sandbox mode.
  - **Receipt:** JSON at `%LOCALAPPDATA%\passing-notes-smoke\receipts\<stamp>.json`,
    outside both the repository and the store. It records CLI versions, reported
    models, the Codex sandbox mode, note ids and the verdict. No raw transcripts
    are kept.
  - **Clean up:** remove only the `messages\pn-smoke-<stamp>`,
    `acks\pn-smoke-<stamp>` and `cards\pn-smoke-<stamp>` folders this run
    created. Any other path is refused before deletion.
- **Done when:**
  - `tests/test_cross_host_checker.py` passes. A complete synthetic exchange
    passes the checker, while a missing ack, a wrong reader and a broken
    `reply_to` each fail it, and so does a missing card. A unit test shows the
    cleanup guard refuses every path except this run's three `pn-smoke-<stamp>`
    folders.
  - One real smoke run passes in both directions, and its receipt exists outside
    the repository and the store.
  - A run with a host unavailable reports failure, not a skip.
  - The full unit suite still passes. The step's checkpoint names both the unit
    suite and the smoke run.
- **Depends on:** 2

### Step 4: Real cross-host acknowledgement
- **Status:** PENDING
- **Type:** operator
- **Issue:** #2
- **Problem:** Headless runs and file writes do not prove that interactive lanes
  on both hosts find and acknowledge each other's notes during normal work.
- **Files:** none. Evidence is recorded in this step block: note ids, ack file
  names and timestamps, never note bodies.
- **Produces:** recorded evidence of a real exchange between an interactive
  Claude window and an interactive Codex window.
- **Depends on:** 3; skill-mesh v3 item 61 (inbox checks in the Claude and Codex
  skill profiles, P11)
- **Done when:** Using project id `pn-uat` with lanes `claude-lane` and
  `codex-lane`, an interactive Claude window and an interactive Codex window each
  find the other's note through their normal inbox checks. Each reads it and
  explicitly acknowledges it, in both directions, without the operator copying
  its contents. The note ids, ack file names and timestamps are recorded here. A
  file write alone does not count as delivered or read.

## Resource queue phase — planned 2026-11-15

**State: HELD for four operator decisions (Q1-Q4 below), which the operator routed to
`/plan-redline`. Steps 5-9 are written against the recommended option of each, marked
"pending Q<n>"; redline may change them. Step 4 keeps its number and stays pending.**

### What this feature does

A lane's turn on a shared resource (an engine lock, a play checkout, a GPU) becomes a
Passing Notes object. Releasing a turn promotes the next waiter and rings it, so no one
has to remember to write a "your turn" note. Beneficiaries: coordinators and work lanes
that share one resource, and the operator, who otherwise finds idle windows.

Evidence: [seed_shared_resource_queue.md](../seeds/seed_shared_resource_queue.md)
(commit `1ce9cd9`) and item 4 of
[seed_coordinated_lanes_playbook.md](../seeds/seed_coordinated_lanes_playbook.md). On
2026-11-15, four of six lanes in another workspace project sat idle about 80 minutes:
the next engine turn was announced in one `needs: fyi` note `to: all`, which
`ring_reason` never rings.

### Existing context

- **Wake path.** `wake.ring_reason` (`src/passing_notes/wake.py:759`) rings a note whose
  `needs` is in `UNBLOCKING_NEEDS` (`reply`, `operator-decision`, `wake.py:106`), a
  `handoff`, or the answer to such a note. `wake.send` (`wake.py:791`) checks, at ring
  time, the recipient's binding and card: a `done` card is refused with
  `wake-session-unavailable` (`wake.py:866`), and a note already acknowledged is not rung.
  `cli._ring` (`src/passing_notes/cli.py:691`) prints the `note:`/`wake:`/`ack:`/`reply:`
  evidence lines and returns exit 5 when the wake is not delivered; the note stays.
- **Post with a wake.** `cli._cmd_post` (`cli.py:405`) already refuses `--wake` and
  `--wake-if-needed` to `all` or `operator` before storing anything, with
  `invalid-argument: --wake needs --to naming one lane, not all` (`cli.py:436-439`).
  `tests/test_wake.py:1136-1150` asserts that exact stderr for `--wake-if-needed`.
- **Kind.** `reservation` is already a protocol v1 kind (`src/passing_notes/protocol.py:209`),
  unused by the CLI so far. A note of kind `reservation` with `needs: reply` is eligible
  under `ring_reason` as it stands.
- **Store guards.** `Store.open` (`src/passing_notes/store.py:331`) refuses a root inside
  a Git working tree (`store-in-repo`, exit 4); `Store._store_folder` (`store.py:350`)
  refuses links at every level (`store-link`). The wake records already keep mutable
  JSON beside the protocol folders under `wake\<project>\` (`wake.py:480`), written by
  exclusive temporary file then replace (`_write_json`, `wake.py:494`), with an exclusive
  lock file and stale break (`_claim`, `wake.py:1060`).
- **Roles.** [Lane rules](../lane-rules.md) section 8: a project names its coordinator in
  its own lane doc, and a project naming none uses its `main` lane. Passing Notes cannot
  read a project's lane doc.
- **R1.** The Manual pilot reconciliation's row R1 kept "engine lock" a project rule. This
  phase reverses R1 for **ordering and notification only** (P20): the lock's mechanics
  stay with the project.

### Scope

In:
- generic named resources with one FIFO queue each, per project: `reserve`, `release`,
  `queue show`, cancel;
- release promotes the next waiter and rings only it, through the existing wake path, by
  posting a `kind: reservation`, `needs: reply` note;
- `queue move`, for the coordinator lane only;
- holder deadlines (`--minutes`, per-resource default) and a read-only `queue check`
  reporting past-deadline, stalled and told-about mismatches, with `--ring` ringing once
  per stall;
- a decision for `post --to all --wake` (clearer refusal, or fan-out; Q3);
- a done card in a queue is skipped with a diagnostic and never rung;
- a protocol v1 addition ("Resource queues") in [docs/protocol.md](../protocol.md).

Out:
- reading, taking, writing or deleting any project's own lock files; automatic fixing of
  a mismatch; eviction of a holder past its deadline (a human or coordinator call);
- any change to `ring_reason` policy, `UNBLOCKING_NEEDS`, or existing note, ack and card
  formats;
- any change to the output of existing commands, except what Q3 decides for
  `post --to all --wake`;
- card writes by queue verbs (a card is written only by its lane);
- a daemon, polling loop or scheduler: `queue check` runs when a hook, loop or lane runs
  it;
- dispatch: a turn is a place in line the lane asked for, never assigned work.

### Impact analysis

Search root: the `passing-notes` checkout (`src/`, `tests/`, `docs/`, `skills/`,
`README.md`), excluding `docs/seeds/` and `.claude/`. Shared-interface consumers found:

| File or interface | Change | Reason | Verified evidence |
|---|---|---|---|
| `src/passing_notes/protocol.py` | Add a resource-id check (the lane-id grammar, reserved device names refused) and queue limit constants | One source for grammar and limits (D11) | `check_lane_id` at `protocol.py:443`; Limits parity in `tests/test_protocol_doc.py:74` |
| `src/passing_notes/store.py` | Add a `QUEUES_DIR` constant beside `CARDS_DIR` (`store.py:55`) | Queue state under the same root and guards | `Store._store_folder`, `Store.open` |
| `src/passing_notes/queue.py` (new) | Queue state, lock, reserve/release/cancel/move, check | Keeps `cli.py` to parsing and printing | Modelled on `wake._write_json`/`_claim` |
| `src/passing_notes/cli.py` | New `reserve`, `release`, `queue show|move|check` (and `queue config` if Q1 = config) parsers and handlers; release calls `store.publish_note` then `_ring` | Production caller | `build_parser` at `cli.py:790`; `_ring` at `cli.py:691` |
| `cli._cmd_post` | Q3 only: the `--to all` branch at `cli.py:436-439` | Refusal text or fan-out | Consumers: `tests/test_wake.py:1141` (exact stderr) |
| `wake.ring_reason`, `wake.send` | No change; called as they are | Ring policy unchanged | `wake.py:759`, `wake.py:791` |
| `docs/protocol.md` | "Resource queues" section, Store layout line, Limits rows | Protocol v1 addition | Limits table checked by `tests/test_protocol_doc.py:74` |
| `docs/cli.md`, `docs/lane-rules.md`, `README.md` | Verb contracts; lane-rules says to use the queue for resource turns; README's "No ... execution queue" line clarified | Docs parity tests in `tests/test_docs.py` | `README.md:199`; `tests/test_docs.py:50` (links) |
| `tests/test_queue.py` (new), `tests/conftest.py` | Synthetic tests over temporary stores, `FakeHost` reuse | Existing style | `store_root` autouse fixture `tests/conftest.py:57`; `FakeHost` in `tests/test_wake.py` |

### New components

- `src/passing_notes/queue.py`: queue state, locking and operations.
- `tests/test_queue.py`: the acceptance tests below.
- Store folder `queues\<project>\` under the store root (pending Q4 for its file shape).

### Design decisions

| Choice | Decision | Provenance | Rationale | Alternatives or unresolved state |
|---|---|---|---|---|
| Q1 Where the coordinator is named | **Pending redline.** Recommended: a store-side project config, `queues\<project>\config.json`, set by `passing-notes queue config --project P --coordinator LANE`; unset means the `main` lane (lane rules section 8) | operator decision pending | Projects already name roles once; a per-call flag would let any caller name itself coordinator | (b) a `--coordinator LANE` flag on each `queue move` and `queue check --ring`. Either way the check compares an explicit lane id; it is identity, not security (lanes self-declare) |
| Q2 Default turn minutes and stall N | **Pending redline.** Recommended: 60-minute turn default, overridable per resource in the Q1 config and per turn by `--minutes`; stall N = 10 minutes (the source project's skip rule) | operator decision pending | 40-minute gates were observed; 10 minutes matched the lanes' own skip rule | Other values; per-resource stall N |
| Q3 `post --to all --wake` | **Pending redline.** Recommended: keep the refusal (exit 2, `invalid-argument`, nothing written) and extend the message: "...not all; ring one lane with --to <lane>, or use a resource queue (passing-notes reserve) for turns". This changes one existing error string and its assertion at `tests/test_wake.py:1141` | operator decision pending | Fan-out multiplies turns (the 2026-11-14 echo lesson); the refusal already exists, only its hint is missing | (b) `--wake-each-active`: one note per non-done card lane, each with a derived idempotency key, each rung if eligible. (c) leave the message byte-identical and document the queue only |
| Q4 Queue file format and atomic-write rule | **Pending redline.** Recommended: one JSON record per resource, `queues\<project>\<resource>.json`, at most `QUEUE_MAX_BYTES`; each change takes an exclusive `<resource>.lock` (stale after `QUEUE_LOCK_STALE_S`, held only for read-modify-write, never across a ring), then writes D9's per-attempt `<final>.<8 hex>.tmp` exclusively and replaces the record (the card rule, D15) | operator decision pending | Matches the wake records and card replace already in the code; one file is easy to show and check | (b) immutable event files published by D9 exactly (no lock; order folded from events; one `release` file per turn makes promotion exactly-once), at the cost of unbounded growth and fold logic |
| Resource names | Lane-id grammar, at most 32 chars, reserved device names refused | agent default | Safe as a file stem; reuses the one grammar | Free text |
| Verb shape | `reserve <resource> --project P --lane L [--minutes N] [--reason TEXT]`; `reserve <resource> ... --cancel`; `release <resource> --project P --lane L`; `queue show [<resource>] --project P`; `queue move <resource> --project P --lane <coordinator> --waiter L (--to N \| --after LANE)`; `queue check [<resource>] --project P [--lane L --ring] [--lock-holder LANE\|none]` | agent default (seed's shape; `--lane` is always the caller, as for `ack` and `card set`) | Explicit identity (D5) | Separate `cancel` verb |
| Retry safety | `reserve` by a lane already holding or waiting prints its place and writes nothing; `release` by a lane not in the queue prints "nothing to release" and exits 0, except that the lane that granted the current turn (`granted_by`) re-posts the reservation note if it is missing (Step 6); `release` by a waiter is refused (exit 2, `not-holder`, use `--cancel`); `--cancel` by the holder is refused (use `release`) | agent default | Sequential retries harmless, as for `post` (D14) | |
| Reservation note | From the releasing lane to the new holder; `kind: reservation`, `needs: reply`; subject `Your turn on <resource>`; `work` names the resource; `idempotency_key` `turn-<resource>-<grant stamp>`; body generated by the CLI from fixed synthetic text (no caller content, so D5's body-from-file rule is not engaged) | agent default within the operator's brief | Eligible under `ring_reason` unchanged; a retried release finds the note instead of posting twice | |
| Skipped lanes | At promotion, a waiter whose card is `done` is dropped from the queue with the diagnostic `skipped <lane>: card is done` and never rung; the next waiter is tried. A waiter with no card is promoted (the wake path then decides) | agent default | A reopened lane reserves again | Keep it queued but passed over |
| Ring failure on release | Release and promotion stand; `_ring` prints `wake: not delivered`, exit 5, note kept | agent default | Same contract as `post --wake`; `queue check` reports the stall | |
| "Stalled" | A holder whose turn came by release and who has not acknowledged its reservation note for N minutes (the resource is free in the project's sense, and the next lane is idle). A self-granted turn (reserve on an empty queue) is never stalled | agent inference; confirm at redline | With promotion on release, "free with waiters and no holder" cannot arise; the observed stall was an idle promoted lane | Also count "no holder, waiters present" if Q4 (b) makes it reachable |
| Stall ring | `queue check --ring --lane L` posts one note per stall to the holder (`reservation`, `needs: reply`, key `stall-<resource>-<grant stamp>`) and one to the Q1 coordinator (`blocker`, `needs: reply`, key `stall-coord-<resource>-<grant stamp>`, skipped when L is the coordinator), and rings each through `_ring`. A run that finds those keys prints `stall: already rung <id>` and rings nothing | agent default | Idempotency keys make a second run in the same stall a no-op without writing queue state | |
| Mismatch | Reported only when the caller passes `--lock-holder LANE\|none` (the project's lock script says who holds its lock); never read from lock files, never fixed | operator brief | Passing Notes owns order and notification only | |
| Move | Reorders waiters only; never displaces the holder; rings no one | agent default | Ring volume stays at one ring per release | |
| `queue check` output | One tab-separated row per finding (`past-deadline`, `stalled`, `mismatch`), then one pinned `queue-check: past_deadline=<n> stalled=<n> mismatch=<n>` line; exit 0 unless the store is unusable | agent default, mirroring `inbox --summary` | Hook-friendly | |

### Build steps

### Step 5: Resource queue verbs (reserve, release, show, cancel)
- **Problem:** A shared resource's turn order lives only in note prose, so nothing can
  show who holds it or who is next.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers deep
- **Files:** `src/passing_notes/protocol.py`, `src/passing_notes/store.py`,
  `src/passing_notes/queue.py` (new), `src/passing_notes/cli.py`, `tests/test_queue.py`
  (new), `tests/conftest.py`, `tests/test_protocol_doc.py`, `docs/protocol.md`,
  `docs/cli.md`, `README.md`
- **Produces:** `reserve` (with `--cancel` and `--minutes`), `release` (promotes the next
  waiter in the record, no ring yet) and `queue show` over `queues\<project>\` (shape
  pending Q4), plus the "Resource queues" protocol section and Limits rows.
- **Done when:**
  - Through the CLI: `reserve` on an empty queue makes the lane holder and prints its
    start and deadline; a second and third lane print positions 1 and 2 and the holder;
    `queue show` lists holder, start, deadline, then waiters in order.
  - `release` by the holder makes the first waiter holder; by a waiter it exits 2
    (`not-holder`) and writes nothing; by an absent lane it exits 0 and writes nothing.
    `--cancel` removes a waiter; by the holder it exits 2.
  - Repeated `reserve` and `release` are harmless no-ops as specified above.
  - Concurrent `reserve` from several processes (the `race` fixture) leaves each lane in
    the queue exactly once and a valid record.
  - Privacy: queue files appear only under the store root's `queues\<project>\`; a root
    inside a Git working tree is refused with `store-in-repo` (exit 4) and a linked
    `queues` folder with `store-link`, nothing written in either case.
  - `tests/test_protocol_doc.py` checks the new Limits rows against `protocol.py`.
  - Every existing test passes unchanged; ruff and mypy report zero findings.
- **Depends on:** none (Step 4 is an unrelated operator step)

### Step 6: Release rings the next holder
- **Problem:** Promotion alone wakes no one; the new holder must be told through the
  path that already rings.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers deep
- **Files:** `src/passing_notes/queue.py`, `src/passing_notes/cli.py`,
  `tests/test_queue.py`, `docs/cli.md`, `docs/lane-rules.md`
- **Produces:** `release` posts the reservation note to the new holder and rings it
  through `cli._ring`/`wake.send`; done-card waiters are skipped and never rung.
- **Done when:**
  - Three lanes reserve `engine` (synthetic project, `FakeHost` bindings). The holder
    releases: the second lane becomes holder, one reservation note to it is stored, and
    the host records exactly one doorbell, to the second lane; the third lane receives
    nothing. When the second releases, the third gets exactly one.
  - The stored note has `kind: reservation`, `needs: reply`, and `ring_reason` returns a
    reason for it; `wake.ring_reason` and `UNBLOCKING_NEEDS` are unchanged in the diff.
  - A retried release by the previous holder after a crash between promotion and post
    (the record names it as `granted_by`) posts and rings the missing note, found by its
    idempotency key; a retry after a complete release posts and rings nothing. One
    note per grant, never two.
  - A waiter whose card is `done` is dropped with `skipped <lane>: card is done`, gets
    no note and no doorbell, and the next waiter is promoted and rung.
  - A ring that fails (no binding) leaves the promotion and the note, prints
    `wake: not delivered`, and exits 5.
  - `docs/lane-rules.md` tells lanes to take resource turns through the queue instead of
    "your turn" notes. Full suite, ruff and mypy clean.
- **Depends on:** 5

### Step 7: Coordinator reordering
- **Problem:** A reroute sent as an fyi note was not read before the release it meant to
  change (seed, 22:59Z); order must change in the queue itself.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers code
- **Files:** `src/passing_notes/queue.py`, `src/passing_notes/cli.py`,
  `tests/test_queue.py`, `docs/cli.md`, `docs/protocol.md` (config record, pending Q1)
- **Produces:** `queue move` for the coordinator lane, and (pending Q1) `queue config`
  naming the coordinator and per-resource turn minutes.
- **Done when:**
  - A non-coordinator `queue move` exits 2 (`not-coordinator`) and writes nothing; the
    coordinator's move changes only waiter order, and the holder cannot be moved.
  - After a move, the next release rings the moved lane, not the earlier order.
  - With no coordinator configured, `main` is the coordinator (pending Q1).
  - A per-resource default from config sets a new turn's deadline; `--minutes`
    overrides it (pending Q1/Q2). Full suite, ruff and mypy clean.
- **Depends on:** 6

### Step 8: Queue check and stall alarm
- **Problem:** Nobody notices a free resource with idle waiters unless someone happens
  to look.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers deep
- **Files:** `src/passing_notes/queue.py`, `src/passing_notes/cli.py`,
  `tests/test_queue.py`, `docs/cli.md`, `docs/lane-rules.md`
- **Produces:** `queue check` (read-only rows plus pinned summary), `--ring` once per
  stall, and `--lock-holder` mismatch reporting.
- **Done when:**
  - A holder past its deadline is reported as `past-deadline`; the queue record's bytes
    are identical before and after the check (never evicted).
  - With a promoted holder that has not acknowledged its reservation note for N
    minutes (clock injected, as `cli._now_utc` is for inbox views), `queue check`
    reports `stalled`; `--ring` stores and rings one note to the holder and one to the
    coordinator; a second run in the same stall stores and rings nothing and prints
    `stall: already rung`. Acknowledging the note clears the stall.
  - `--lock-holder` naming a different lane prints a `mismatch` row; without the flag no
    mismatch is ever reported; no lock file is read in any test (only store files are
    touched).
  - Without `--ring`, `queue check` writes nothing (store snapshot unchanged). Full
    suite, ruff and mypy clean.
- **Depends on:** 7

### Step 9: Waking `all`
- **Problem:** `post --to all --wake` is refused without pointing the sender at a way to
  reach the lanes it meant.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers code
- **Files:** `src/passing_notes/cli.py`, `tests/test_wake.py`, `docs/cli.md`
- **Produces:** the Q3 behaviour (recommended: the refusal names `--to <lane>` and the
  resource queue).
- **Done when (recommended option):** `post --to all --wake` and `--wake-if-needed`
  still exit 2 with `invalid-argument` and write nothing, and the message names both
  routes; `tests/test_wake.py:1141` is updated only for that message, and the change is
  listed in the phase record as the one existing-output change. **(Fan-out option):**
  `--wake-each-active` posts one note per non-done card lane except the sender, each
  with a derived idempotency key, rings each eligible lane once, and a retry posts and
  rings nothing new. Full suite, ruff and mypy clean.
- **Depends on:** 5

### Risks and open questions

| Item | Consequence | Mitigation / owner | Blocking |
|---|---|---|---|
| Q1-Q4 unresolved | Steps 5, 7, 8, 9 shapes may change | Operator at `/plan-redline`; re-run plan-review and plan-wrap if material | Yes, for build |
| Stall definition is an inference | Check could miss or over-report | Confirm at redline | Yes, for Step 8 |
| Lane identity is self-declared | Any process can claim the coordinator lane | Document as identity, not security (as cards) | No |
| Stale-lock break (Q4 a) | A broken lock during a slow write could lose an update | Lock held only for read-modify-write; stale timeout far above that; concurrency test | No |
| Codex lanes ring by queue | `queued` is not `delivered` | Existing wake states; `queue check` reports the stall | No |
| Double holders (project lock taken without a turn) | Queue and lock disagree | Reported only via `--lock-holder` | No |
| No evidence for many resources or more than six lanes per queue | Limits may be wrong | `QUEUE_MAX_WAITERS` bounded and refused with a clear error | No |
| Woken Codex turns may run in a tighter sandbox (seed, unproven) | A rung lane may fail its first command | Out of scope; tracked in the seed | No |

### Testing strategy

- Narrow: `uv run pytest -q tests/test_queue.py` while iterating; Step 9 adds
  `tests/test_wake.py`.
- Through the production caller: every acceptance test drives `cli.main` via the
  `invoke` fixture (`tests/conftest.py:179`) or the console script (`run_cli`), with the
  autouse temporary `store_root` and a `FakeHost`; no test reaches a real host.
- Classification: one-shot CLI verbs, not always-on. `queue check` is meant for hooks
  or loops but is itself one-shot; no long observation is triggered. No deployment seam
  beyond the existing editable tool install.
- Per-change gate: `uv run pytest -q`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy` (Platform and toolchain section).
  Baseline: 662 passed, 12 skipped (slice A record).
- Manual check after Step 6 (optional, synthetic project): two bound Claude windows,
  one releases `engine`, the other receives exactly one doorbell.

## Reuse and boundaries

- heads-up owns advisory work claims and conflict warnings.
- find-again can retrieve explicitly selected historical artifacts.
- paper-trail owns consequential durable decisions.
- same-page checks supported status contradictions.
- Inbox checks inside skills (session start, gates, handoff) are owned by the
  skill-mesh v3 plan, item 61 (P11). This repository supplies the CLI they call
  and its usage documentation. Issue #2 now tracks Step 4.
- The operator board page is an Observatory derived view (P16), not part of
  Passing Notes.

Passing Notes owns note exchange, receipt and an opt-in content-free doorbell.
It does not assign work, replace canonical plans, grant permissions or establish
acceptance. The CLI opens no interactive agent windows; the separately invoked
lane-open skill does. No dashboard, daemon, rapid polling, network server,
distributed synchronization, automatic dispatch, or mandatory integration with all
sibling utilities in v1. Idle, bound Claude sessions can receive doorbells;
Codex lanes still invoke inbox checks at checkpoints.

## Manual pilot reconciliation (2026-11-11)

**Source:** a private workspace project ran this plan by hand. Its lane protocol
and note template are at commit `125664f` in that project's repository. The
store was inspected read-only on 2026-11-11, file names and headers only, with no
bodies quoted or copied. It held 4 notes and 2 acks from 3 lanes in separate
windows. All files were UTF-8 with LF and no BOM, at most 2.7 KB, with bodies of
at most 25 lines. All headers fit protocol v0 except one subject of 127 characters
(hence the 160 limit), and one ack has a blank line (hence blank-line tolerance).
All 3 lanes were Claude windows, so the pilot proves nothing about Codex. Steps 3
and 4 cover that.
No real note or ack is copied into this repository. The fixtures stay synthetic.

| ID | Pilot element | Decision | Detail |
|---|---|---|---|
| A1 | Root `%LOCALAPPDATA%\passing-notes` with `messages\` and `acks\` | adopt | Unchanged |
| A2 | One immutable Markdown note with a restricted front-matter header; corrections are new notes with `reply_to` | adopt | Header grammar pinned: `key: value` literal lines plus `refs` list items |
| A3 | Note id `<yyyymmddTHHMMSSZ>-<from>-to-<to>-<4 hex>` = file stem | adopt | Header authoritative; id `from`/`to` for listing only |
| A4 | `.tmp` then rename | adopt + clarify | Rename never overwrites; on collision pick new hex; readers ignore non-id files |
| A5 | Fields `id, created_utc, project, from, to, subject, needs, reply_to, refs` | adopt | Maps to plan fields: sender = `from`; recipient = `project` + `to`; requested response = `needs`; references and revisions = `refs` |
| A6 | `needs`: `reply`, `ack`, `fyi`, `operator-decision` | adopt | Closed set |
| A7 | Lane ids as recipients, plus `all` and `operator` | adopt | Lanes replace "session" addressing: a lane outlives its window. `all` = every lane of the project except the sender |
| A8 | Ack `acks\<id>--<reader>.md` with `reader`, `read_utc`, `received` | adopt | Key = (project, note id, reader); existing file = already acknowledged, retry succeeds |
| A9 | Inbox moments, no polling, optional one-line doorbell | adopt | Unchanged |
| C1 | Flat `messages\`/`acks\` folders | change | Per-project `messages\<project>\` and `acks\<project>\`. Ids omit the project, so a second project with a `central` lane or `to: all` would collide in filename-based inboxes. Lanes then list only their own project, honoring "do not silently scan other projects" |
| C2 | Lane id format unspecified | change (tighten) | `[a-z0-9]+(-[a-z0-9]+)*`, at most 32 chars, no `to` segment; `all`/`operator` reserved; no lane writes `from: operator`. Existing ids comply |
| C3 | Bounds unspecified ("about 60 lines") | change (tighten) | Note at most 16 KiB, ack at most 1 KiB, subject 1-160 chars, at most 20 refs of at most 300 chars; `created_utc` within 60 s of the id stamp (observed skew 1 s); body ~60 lines stays guidance |
| C4 | Cross-project sender not expressible | change (additive) | Optional `from_project`, defaulting to `project`; a note lives in the recipient project's folder |
| R1 | Project rules (commit etiquette, provenance numbering, engine lock, ownership via lane tables) | not adopted | Stay in each project's own lane doc. Passing Notes keeps no lane registry and enforces no project rule. **2026-11-15 (P20):** reversed for the engine lock's ordering and notification only, on the evidence of `docs/seeds/seed_shared_resource_queue.md`; the lock's mechanics stay project-side (Resource queue phase) |
| R2 | Absolute user-profile path in docs | not adopted | Public docs use `%LOCALAPPDATA%` |

**Defined in v1 (P13, P14):** the fields v0 reserved (`kind`, `work`, `revision`,
`supersedes`, `idempotency_key`, ack line `session`) and lane cards, by operator
approval of the skill-mesh v3 decision sheet (items 54 and 57). Readers ignore
unknown keys, so v0 files stay valid and need no migration. The operator board
page remains outside Passing Notes (P16).

---

## Phase 1 — First version: CLI, lane cards and cross-host smoke

**3 of 4 steps done: issues #1, #3 and #4 closed; #2 stays open for Step 4. 488 tests
passing, 12 skipped. Zero type errors. Zero lint violations.**

### What was built

- `passing-notes post`, `inbox`, `read` and `ack` over protocol v1 notes and acks:
  every field validated against the protocol's limits, no-overwrite publication,
  idempotency keys, same-sender `supersedes`, bounded diagnostics, and the
  `store-in-repo` and `store-link` refusals (Step 1, #1).
- `passing-notes card set` and `card show` over one card per lane, replaced in one
  step through an exclusive `.tmp` file (Step 2, #3).
- An opt-in cross-host smoke that drives headless `claude -p` and `codex exec`
  sessions through the installed CLI in both directions and judges only the store
  files. Real run `pn-smoke-20261112t171349z` passed 6/6 phases (Step 3, #4).
- `read` and `card show --lane` frame bodies as untrusted data behind a `| ` gutter;
  one-line fields refuse invisible characters and printed output escapes them (D10,
  D19).

### Files changed

| File | Change |
|---|---|
| `src/passing_notes/protocol.py` | The single source of the note, ack and card grammar, fields and limits; parse and render |
| `src/passing_notes/store.py` | Store root resolution, `store-in-repo` and `store-link` guards, no-overwrite publication, card replace |
| `src/passing_notes/cli.py` | The six verbs, exit codes and one-line errors |
| `src/passing_notes/__main__.py` | `python -m passing_notes` entry point |
| `pyproject.toml` | Python 3.11+, no runtime dependencies, development group, `passing-notes` console script |
| `tests/cross_host/run_smoke.py`, `tests/cross_host/check_store.py` | Opt-in smoke driver and store-only checker, outside default pytest collection |
| `tests/test_*.py`, `tests/conftest.py`, `tests/fixtures/` | Unit suite over synthetic fixtures and temporary stores, including doc parity and the smoke checker's calibration |
| `docs/protocol.md` | Rules the CLI enforces (reserved device names, `.tmp` suffixes, invisible characters, card limit names) |
| `README.md` | Install, quickstart, lane cards, the Codex writable root, cross-host smoke |
| `.gitattributes` | Keeps the CRLF fixtures byte-exact |

### Fresh context notes for Phase 1

| Issue | Detail |
|---|---|
| Codex store access | Codex `workspace-write` cannot write to `%LOCALAPPDATA%` unless the store root is added with `--add-dir` (P8); the smoke passes it on every Codex call |
| Smoke sign-in | Both standalone CLIs (`claude`, `codex`) must be signed in. A signed-out `claude` CLI blocked the first Step 3 attempt while the IDE session still worked |
| Smoke is opt-in | `uv run python tests/cross_host/run_smoke.py` spends real credits; `uv run pytest` runs only the checker's calibration |
| Step 4 | Waits on skill-mesh v3 item 61; tracked as Manual UAT M1 below |

## Wake and lane-skills phase — shipped 2026-11-13

**Delivered in `f61c840`; no implementation issues were assigned to this chunk.
The shipped build recorded 555 tests passing, 12 skipped, zero type errors and
zero lint violations. Issue #2 / Manual UAT M1 remains pending.**

### What was built

- A content-free doorbell to an idle Claude lane, bound to its exact session id.
  The headless bridge is restricted to ListAgents and SendMessage; binding reads
  the configured permission mode unless explicitly supplied. Busy, closed and
  already-rung sessions are refused, with the posted note retained.
- `lane-open`, `lane-close` (including `--all`) and `lane-handoff` skills for
  Claude Code and Codex, with one shipper per checkout and the shipper closing last.
- A SHA-256 ownership ledger for building, installing and removing lane skills.
  These skills remain in this repository, outside Skill Mesh's v3.0.0 catalog.
- Shared lane rules: inbox moments, binding each new session, operator relay using
  verbatim `operator-said:` replies, and evidence-based handover and closure.
- A full CLI reference and four workflow diagrams with light and dark variants.

### Files changed

| File | Change |
|---|---|
| `src/passing_notes/wake.py`, `src/passing_notes/cli.py` | Bind/send/show, bridge result validation, duplicate and busy refusals, optional wake after posting |
| `skills/lane-open/`, `skills/lane-close/`, `skills/lane-handoff/` | Shared cores, host adapters and the window-opening script |
| `tools/skills_install.py` | Ledgered build/install/uninstall with ownership checks |
| `docs/lane-rules.md`, `docs/cli.md` | Lifecycle rules and command contracts |
| `tools/diagrams.py`, `docs/diagrams/`, `README.md` | Workflow illustrations and operator entry points |
| `tests/test_wake.py`, `tests/test_lane_open.py`, `tests/test_skills_install.py`, `tests/test_docs.py` | Synthetic regression checks for the shipped surfaces |

### Fresh context notes

| Issue | Detail |
|---|---|
| Live evidence | The shipped commit records a Codex-to-Claude round trip, busy/closed/duplicate doorbells, and lane open/close/handoff checks. This phase record does not claim a new live run. |
| Bindings | Restarting Claude changes its session id; every new session must bind again. |
| Delivery | Posted, wake delivered, acknowledged and replied are separate evidence states. |
| Remaining work | Codex wake is a seed, not shipped functionality. Lane-scoped session-wrap git handling and wrap reminders are queued for Skill Mesh v3.1. |
| Installation | Restart host sessions after installing generated skills; edit canonical sources only. |

---

## Coordinated-lanes fixes (slice A) — shipped 2026-11-15

**Delivered in `b3b0c67`, `c030f2c`, `1f0a919` and `8217790`; no implementation
issues were assigned to this chunk. 662 tests passing, 12 skipped. Zero type errors.
Zero lint violations. Protocol v1 and `wake.ring_reason` policy are unchanged.**

### What was built

- Doorbell text says acknowledging a doorbell is not the deliverable; a finished item
  posts its own handoff note. The `lane-open` brief names `/user-build-route`, a
  compile/test before hand-off and one handoff per item; `lane-close` SELF mode ends
  with a `SAFE TO CLOSE THIS WINDOW` line.
- `post --wake` refuses a note that `ring_reason` would not ring before storing it
  (exit 2, `wake-not-needed`, nothing written). `post --wake-if-needed` posts always,
  rings only an eligible note and otherwise prints `wake: not rung (<why>)` with exit 0,
  including on an idempotent retry.
- `inbox --summary` prints one pinned line,
  `inbox-summary: count=<n> oldest_age_min=<m|-> stale=<k|->`; `--stale-after <min>`
  counts unacked notes that need a reply and are older than that many minutes.
- `card show --overlaps` lists `overlap` rows for owns/holds two non-done lanes share
  (tab-separated: `overlap`, lane-a, lane-b, entry-a, entry-b, with lane-a sorting
  before lane-b); `card set` prints the same rows for the lane just set. The overlap rule is
  case-insensitive and normalises `\`, `//`, `./` and trailing `/`.

### Files changed

| File | Change |
|---|---|
| `src/passing_notes/wake.py` | Doorbell wording, `RING_RULE`, `ring_reason` accepts a draft |
| `src/passing_notes/cli.py` | Pre-store wake check, `--wake-if-needed`, inbox summary and stale views, overlap detection |
| `skills/lane-open/core.md`, `skills/lane-close/` | Brief line, overlaps mention, SAFE TO CLOSE line |
| `docs/cli.md`, `docs/lane-rules.md`, `README.md`, `CLAUDE.md`, `AGENTS.md` | Flag contracts, exit codes, session-start `--overlaps` |
| `tests/test_wake.py`, `tests/test_cli.py`, `tests/test_cards.py`, `tests/test_docs.py`, `tests/test_lane_open.py` | Regression checks through the real CLI |

### Fresh context notes

| Issue | Detail |
|---|---|
| Output stability | Every new view is an opt-in flag, except `card set`'s overlap rows, which print only when an overlap exists; otherwise output is byte-identical. |
| Exit codes | Strict `--wake` refusal is exit 2 (pre-store); `wake send` keeps exit 5. Binding and done-card checks stay at ring time. |
| Open advisories | The `--wait` usage error still says "only with --wake"; a leading `//` (UNC) path collapses to `/` in the overlap rule. |

## Appendix

### Central Codex coordination repair (operator scope, 2026-11-14)

The operator delegated routine decisions within authorized work to central and
requested supported session delivery, launch preflight, native-approval boundary
tests, restart and graceful closure. This extends P18: central may make scoped
`coordinator-decision:` replies without pretending they are verbatim operator
words. Native host permission remains separate. Implementation and live evidence
are tracked in [Codex coordination](../codex-coordination.md). The work is not
accepted merely because notes were posted or queued; recipient acknowledgement,
answer and completion remain separate evidence states. Existing maintainer edits
are preserved and integrated, not discarded.

### Decision Inventory

| ID | P/D | choice | status |
|---|---|---|---|
| P1 | P | Public `aberson/passing-notes` repository | stands |
| P2 | P | Messages and acknowledgements remain private, outside Git | stands |
| P3 | P | Step 1 built through Forge after Ambition delivery | changed 2026-11-11 (replaced by P5) |
| P4 | P | Lanes coordinate now through protocol v0, in every project that runs lanes | stands |
| P5 | P | Build Step 1 now as its own `/build-phase` run in this repository | stands |
| P6 | P | Whole-number step headings so `/build-phase` parses every step | stands |
| P7 | P | Cross-host smoke is its own code step | stands |
| P8 | P | Codex lanes add the store root as an extra writable root | stands |
| P9 | P | Lanes install with `uv tool install --editable <checkout>` | stands |
| P10 | P | ruff and mypy as development-only tools | stands |
| P11 | P | Skill inbox hooks owned by skill-mesh v3 item 61; Issue #2 tracks Step 4 | stands |
| P12 | P | Smoke limits: 300 s per host call, Claude turn cap 8, receipt outside repository and store, no raw transcripts | stands |
| P13 | P | Protocol v1 note fields (`kind`, `work`, `revision`, `supersedes`, `idempotency_key`, ack `session`) built with Step 1 (decision-sheet item 54) | stands |
| P14 | P | Lane cards in `cards\<project>\<lane>.md`, one per lane, written only by that lane (item 57) | stands |
| P15 | P | Content-free doorbells between Claude lanes; Codex lanes check at checkpoints (item 60) | stands |
| P16 | P | Operator board page is an Observatory derived view, not Passing Notes (item 62) | stands |
| P17 | P | The generic lane skills `lane-open`, `lane-close` and `lane-handoff` (open, close, and hand off or consolidate lanes, each in its own Claude or Codex window) live in this repository in Skill Mesh's package shape, for Skill Mesh to register; the CLI itself still launches no agents | stands; operator 2026-11-13 |
| P18 | P | Operator relay ("Main speaks for you"): a lane posts operator decisions to the project's relay lane and rings it; the relay answers only with the operator's verbatim words on an `operator-said:` line, accepted only from the relay lane as a reply to the lane's own question; close confirmations work the same way ([lane rules](../lane-rules.md) section 4) | stands; operator 2026-11-13 |
| P19 | P | An established, shared rule set ([docs/lane-rules.md](../lane-rules.md)) that projects adopt, including `wake bind` at every session start because a restart changes the session id; the README presents it with workflow diagrams in Skill Mesh's style | stands; operator 2026-11-13 |
| P20 | P | Resource queue phase (Steps 5-9): Passing Notes owns turn order and notification for named shared resources; R1 reversed for ordering and notification only, never a project's lock files; `ring_reason` policy and existing formats unchanged | stands; operator brief 2026-11-15 |
| Q1-Q4 | P | Coordinator naming; default turn and stall minutes; `post --to all --wake`; queue file format and write rule | pending `/plan-redline` (Resource queue phase, Design decisions) |
| D1 | D | Python standard-library CLI | stands |
| D2 | D | Immutable note files and separate atomic acknowledgements | stands |
| D3 | D | Explicit inbox/read/ack | stands |
| D4 | D | Session/handoff integration after the CLI, in this repository | changed 2026-11-11 (moved to skill-mesh, P11) |
| D5 | D | CLI contract (flags, exit codes, `store-in-repo` guard) | stands |
| D6 | D | Per-project store folders (C1) | stands |
| D7 | D | Python 3.11 or later; native Windows only in v1 | stands |
| D8 | D | Store folders inherit `%LOCALAPPDATA%` permissions; never loosened | stands |
| D9 | D | Exclusive per-attempt `<final>.<random>.tmp` creation, rename without overwrite, up to 5 new-hex attempts | stands |
| D10 | D | `read` frames the body as untrusted data | stands; the `\| ` body-line gutter confirmed by the operator 2026-11-12 |
| D11 | D | Test that `docs/protocol.md` and `protocol.py` agree, through a named Limits table | stands |
| D12 | D | Kind slugs: `handoff`, `change-notice`, `question`, `answer`, `blocker`, `commit-request`, `reservation`, `operator-relay`, `fyi` ("question and answer" split so a reply is distinguishable) | stands |
| D13 | D | `supersedes` honored only from the same sender, so no lane can hide another lane's note | stands |
| D14 | D | Idempotency is checked before posting: sequential retries are harmless, simultaneous duplicates can both land | stands |
| D15 | D | Cards are mutable: an exclusive `.tmp`, then a replace; at most 8 KiB | stands |
| D16 | D | `post` requires `--kind`; hand-written v0 notes without it stay valid | stands |
| D17 | D | Lane cards get their own step (Step 2) so Step 1 stays one vertical slice | stands |
| D18 | D | pytest is the development-only test runner (workspace Python rule; the build tooling's test counter reads pytest); runtime stays standard-library only. A minimal package scaffold is committed before Step 1 so the build has install and test commands | stands |
| D19 | D | One-line fields refuse characters that can hide or reorder text, including zero-width joiners and variation selectors, even though that rejects some emoji and Persian text that uses a zero-width non-joiner | stands; operator chose strictness over breadth 2026-11-12 |

### Initial scope review (2026-10-20, historical)

Superseded by the 2026-11-11 plan review ([decisions](../plan-review/passing-notes-review.html))
and the Decision Inventory above. Kept for the record. Its original route (P3)
and backlog-only verdict no longer apply.

#### Plan review (2026-10-20)

Reviewing as: greenfield plan. Sections 17-21 skipped.

- **Blockers:** None for publishing the public scaffold with private runtime data.
- **Significant gaps:** exact message/ack schema and ID formats, runtime
  permissions and rejection of repository-contained stores, owning source files,
  command contracts, and run commands were deliberately unselected.
- **Missing items:** build steps lacked Type/Problem/Issue fields and concrete
  file lists; Step 2 combined code integration with attended acceptance.
- **Nice-to-haves:** None proposed.

#### Compact redline (2026-10-20)

P1-P3 and D1-D4 originated here; their current wording and status live in the
Decision Inventory above.

#### Plan wrap (2026-10-20)

READY WITH GAPS: 5 gaps (schemas, identifiers, unresolved implementation
choices, dispatchable step shape, quickstart). Resolved 2026-11-11 by protocol
v0, the CLI contract, the platform and toolchain section, and the step blocks
above.

## Manual UAT

*Generated by /build-phase on 2026-11-12. Append-only; re-running the phase adds new items below, never modifies existing ones.*

### M1: Real cross-host acknowledgement
- **Source step:** Step 4 (from this plan's Build order)
- **Issue:** #2
- **What you're looking for:**

  | Check | Expected outcome |
  |---|---|
  | Precondition | skill-mesh v3 item 61 (inbox checks in the Claude and Codex skill profiles) has landed, and both host windows were restarted after the upgrade |
  | Claude to Codex | Using project `pn-uat`, an interactive Codex window on lane `codex-lane` finds the Claude window's note through its normal inbox check, reads it and explicitly acknowledges it, without the operator copying its contents |
  | Codex to Claude | An interactive Claude window on lane `claude-lane` does the same for the Codex window's note |
  | Store evidence | `%LOCALAPPDATA%\passing-notes\acks\pn-uat\` holds `<note-id>--codex-lane.md` and `<note-id>--claude-lane.md` for the two notes; a file write alone does not count as delivered or read |
  | Recorded in Step 4 | The note ids, ack file names and timestamps are written into the Step 4 block, never note bodies |
