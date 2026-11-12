# Passing Notes

## 1. What This Is

Status: protocol v1 adopted 2026-11-11 ([docs/protocol.md](../protocol.md)). Lanes
already use its v0 subject by hand. Steps 1-3 (CLI, lane cards, cross-host smoke)
are built (2026-11-12). Step 4 (the real Claude and Codex check) follows the
skill-mesh inbox hooks.

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
- **Typecheck:** `uv run mypy` (strict, over `src` and `tests`)
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
  (including an unreadable `--body-stdin`); 3 note not found; 4 store unusable
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

Passing Notes owns note exchange and receipt only. It does not assign work,
replace canonical plans, grant permissions, launch agents, send external messages,
or establish acceptance. No dashboard, daemon, rapid polling, network server,
distributed synchronization, automatic dispatch, or mandatory integration with all
sibling utilities in v1. Agents must be active and invoke inbox checks; this tool
does not wake sleeping sessions.

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
| R1 | Project rules (commit etiquette, provenance numbering, engine lock, ownership via lane tables) | not adopted | Stay in each project's own lane doc. Passing Notes keeps no lane registry and enforces no project rule |
| R2 | Absolute user-profile path in docs | not adopted | Public docs use `%LOCALAPPDATA%` |

**Defined in v1 (P13, P14):** the fields v0 reserved (`kind`, `work`, `revision`,
`supersedes`, `idempotency_key`, ack line `session`) and lane cards, by operator
approval of the skill-mesh v3 decision sheet (items 54 and 57). Readers ignore
unknown keys, so v0 files stay valid and need no migration. The operator board
page remains outside Passing Notes (P16).

## Appendix

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
