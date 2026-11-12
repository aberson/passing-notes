# Passing Notes

Status: manual protocol v0 adopted 2026-11-11 ([docs/protocol.md](../protocol.md));
CLI and session integration pending.

## Selected execution route

**Update 2026-11-11 (operator):** lanes do not wait for the CLI. Protocol v0, the
manual form of this plan's store and format, is extended to every workspace
project that runs several lanes. The workspace's Passing Notes instructions now
point to it. Step 1 implements that same format. The build route for the CLI (the
P3 text below) is under operator re-selection. It stands until the operator
changes it.

Operator approved building Step 1 through Ambition -> Swarm Forge, after the
prepared Ambition retained-output build is accepted. Do not implement this CLI
manually in a parallel session. Forge must separately qualify this repository,
freeze exact write paths/suites and source pins, and prepare a fresh grant. The
operator starts the build with Approve & Go in Ambition. This scaffold is not yet
a qualified Forge target or a dispatch-ready plan.

After native acceptance, exchange and explicitly acknowledge one real coordination
note with the Yarvis window. Automatic session hooks remain Step 2 and are not
required for that initial explicit CLI exchange. Phone Wi-Fi/Tailscale work follows
this real exchange in the owning Forge sequence. Yarvis S1 stays in its own window.

## Approved outcome

Agents working in different local sessions can leave a short coordination note,
find notes addressed to their project/session, and explicitly acknowledge them.
The operator should not have to copy whole prompts between model windows.

Public repository: code, documentation, tests and synthetic examples only.
Private runtime root: `%LOCALAPPDATA%/passing-notes` on Windows, outside all
repositories. Messages and acknowledgements never become public project data.

## First version

Python standard-library CLI with `post`, `inbox`, `read`, and `ack`.
One immutable file per message, separate acknowledgement records, no shared
append-only board that concurrent agents must rewrite. Use unique IDs and atomic
publication. Read does not implicitly acknowledge. Retrying an acknowledgement
must not create conflicting state. Invalid or incomplete files produce a bounded
diagnostic rather than disappearing silently.

A note records its ID, creation time, sender, recipient project/session, subject,
body, source references and revisions, and requested response. An acknowledgement
identifies the note, reader and time. Acknowledgement means receipt, not agreement,
execution authority, implementation completion or user acceptance. Treat note
content as untrusted input and reconcile it with the current operator instruction.

Read message text from an explicit file or stdin; do not require private content
in shell arguments. Bound file sizes. Validate IDs before resolving filenames and
keep reads/writes within the runtime root. Do not silently scan other projects,
inboxes, credentials or model transcripts.

**Format, IDs, ack key, addressing and store layout are fixed by
[protocol v0](../protocol.md).** The CLI reads and writes exactly that format.
Recipients are durable lanes within a project, not sessions. The reconciliation
below records why.

## Proposed CLI contract (D5)

- `passing-notes post --project P --from LANE --to RCPT --subject TEXT --needs N
  [--reply-to ID] [--ref TEXT]... [--from-project P2] (--body-file PATH | --body-stdin)`
  prints the new note id.
- `passing-notes inbox --project P --lane LANE [--include-acked]` prints one line
  per note: id, needs, from, subject. It then prints at most 20 diagnostic lines
  plus a total count for invalid, oversize, unmigrated (flat layout) or leftover
  `.tmp` files.
- `passing-notes read --project P ID` prints the note. It never writes an ack.
- `passing-notes ack --project P --lane LANE ID` writes the ack. If the ack
  already exists, it reports "already acknowledged" and succeeds.
- Exit codes: 0 success; 2 usage or validation error; 3 note not found; 4 store
  unusable.
- Store root: `%LOCALAPPDATA%\passing-notes`, overridable by
  `PASSING_NOTES_ROOT` (tests use temporary directories). Startup refuses a root
  inside a Git working tree, with the stable error code `store-in-repo`.
- Body text comes only from a file or stdin, never from an argument.
- Lane identity is always explicit (`--project`, `--lane`/`--from`). It is never
  derived from a host's session variables. Codex binds no session id, and
  borrowing another host's variable would misattribute acks.
- The CLI is host-neutral. Claude and Codex lanes run the same commands against
  the same store. Being reachable from both hosts is a core requirement: it is why
  Claude's built-in cross-session messaging is not enough.

## Build order

<!-- autofix-applied: 2026-11-11 -->
### Step 1: Local private message CLI
- **Status:** PENDING
- **Type:** code
- **Issue:** #1
- **Problem:** Lanes exchange notes by hand-writing protocol v0 files. Nothing
  validates IDs, bounds or no-overwrite publication. Malformed or stray files are
  only noticed by eye.
- **Flags:** `--reviewers deep`
- **Files (proposed; freeze at build qualification):** `pyproject.toml` (no
  runtime dependencies; `passing-notes` console script);
  `src/passing_notes/{__init__,__main__,cli,protocol,store}.py`. `protocol.py`
  is the single source of the grammar, field set and bounds. Also
  `tests/test_{protocol,store,cli}.py`, synthetic `tests/fixtures/`, the opt-in
  `tests/cross_host/run_smoke.py` (outside default discovery), and the README
  quickstart.
- **Produces:** post/inbox/read/ack over the protocol v0 store, with private
  default storage and clear errors. Proven between a real Claude session and a
  real Codex session.
- **Run (proposed):** `uv run passing-notes ...`; unit tests:
  `uv run python -m unittest discover -s tests -t .`; cross-host smoke:
  `uv run python tests/cross_host/run_smoke.py`
- **Cross-host smoke (real models; the operator approved spending Claude and
  Codex credits, 2026-11-11):**
  - **Setup:** use a fresh synthetic project `pn-smoke-<utc stamp, lowercase>` in
    the real default store root. That exercises the real path and each host's
    write permission, including the Codex sandbox.
  - **Claude to Codex:** a headless Claude session (`claude -p`) posts a note to
    lane `codex-lane` through the CLI. A headless Codex session (`codex exec`)
    lists its inbox, reads the note, acks it and posts a reply with `reply_to`.
    Claude then reads the reply and acks it.
  - **Codex to Claude:** the same exchange, started by Codex.
  - **Verdict from the store only:** note ids, `reply_to` links, and ack files
    with the right reader. Never from what either model says it did.
  - **Both hosts are required.** A missing CLI, failed auth or a sandbox denial
    fails the run. It never counts as a skip.
  - **Calibrate first:** a seeded run with one ack missing must fail the checker.
  - **Record:** each host's CLI version, the model it reports, and the Codex
    sandbox mode, which must be the one a real Codex lane would use.
  - **Clean up:** remove only the `pn-smoke-*` folders the run created.
- **Done when:** Two separate local processes exchange and acknowledge a synthetic
  note. Recipient filtering works, and `to: all` excludes the sender. Concurrent
  publication preserves both notes. A repeated ack is a successful no-op.
  Malformed IDs cannot escape the store. A root inside a repository is refused
  with `store-in-repo`. Synthetic hand-written v0 files are read unchanged (CRLF,
  blank lines in acks, unknown header keys). Flat-layout, oversize and `.tmp`
  files produce bounded diagnostics. Runtime files stay outside the public
  repository. The cross-host smoke passes in both directions. The DONE-gate
  checkpoint names both the unit suite and the cross-host smoke. Use focused
  standard-library tests, not a new test framework or a broad performance campaign.
- **Pilot evidence (format only):** see the reconciliation below. It does not
  exercise the CLI, concurrency, ID rejection or any Codex lane.

<!-- autofix-applied: 2026-11-11 -->
### Step 2-prep: Session and handoff integration
- **Status:** PENDING
- **Type:** code
- **Issue:** #2
- **Problem:** Lanes remember inbox moments only through written instructions.
  Skills do not check the inbox at session start, gates or handoff.
- **Flags:** `--reviewers deep`
- **Files:** Canonical skill-mesh session/task-handoff integration and its docs,
  for both the Claude and the Codex host profiles; passing-notes usage
  documentation. Resolve exact owning files before edits.
- **Produces:** Inbox checks at session startup, meaningful checkpoints and
  handoff. Each is an advisory call that fails open and never blocks.
- **Depends on:** 1
- **Done when:** An integration test through each host's production skill entry
  point (Claude profile and Codex profile) reaches `passing-notes inbox`, and
  degrades silently when the CLI or store is absent. The Codex profile passes its
  lane explicitly and never reads another host's session variables.

### Step 2: Real cross-session acknowledgement
- **Status:** PENDING
- **Type:** operator
- **Issue:** #
- **Problem:** File writes alone do not prove delivery between real sessions.
- **Produces:** Recorded evidence of a real exchange through the integrated checks.
- **Depends on:** 2-prep
- **Done when:** One interactive Claude window and one interactive Codex window
  discover the same shared store. Each reads and explicitly acknowledges a note
  from the other, in both directions, without the operator copying its contents.
  Record actual evidence; a file write alone is not delivered/read. (The manual
  pilot showed the exchange by hand, but only between Claude lanes. This step
  proves it across hosts through the integration.)

## Reuse and boundaries

- heads-up owns advisory work claims and conflict warnings.
- find-again can retrieve explicitly selected historical artifacts.
- paper-trail owns consequential durable decisions.
- same-page checks supported status contradictions.

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
All 3 lanes were Claude windows, so the pilot proves nothing about Codex. The
Step 1 cross-host smoke and the Step 2 check cover that.
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

**Reserved, not defined in v0:** header names `kind`, `work`, `revision`,
`supersedes`, `idempotency_key` and the ack line `session`. A separate workspace
planning thread has proposed them: a fixed note kind set, a work ref plus
revision, harmless re-posts, and recording which window acknowledged. They await
an operator decision. Because readers ignore unknown keys, adding them later
needs no migration. Lane cards and an operator board page stay outside v1 (see
the boundaries above).

| ID | P/D | Choice | Status |
|---|---|---|---|
| P4 | P | Lanes coordinate now through protocol v0, in every project that runs lanes | selected 2026-11-11 |
| D5 | D | CLI contract above (flags, exit codes, `store-in-repo` guard) | proposed default |
| D6 | D | Per-project store folders (C1) | proposed default; the pilot lanes migrate |

Before implementation, follow the workspace's existing plan review and build
workflow. This project creation does not start another build campaign or block
Ambition/Forge UAT.

## Initial scope review (2026-10-20)

> Superseded in part on 2026-11-11: protocol v0 and the steps above resolve
> wrap gaps 1, 2 and 7, and partly 5 and 8. Re-run plan-review, then
> plan-redline, then plan-wrap, against the current text.

Reviewed in order: plan-review ? plan-redline ? plan-wrap. This is an approved
project scaffold and implementation backlog, not a dispatch-ready build plan.
No implementation or external messaging is started by this review.

### Plan review

Reviewing as: greenfield plan. Sections 17?21 skipped.

The contributor conventions and step-sizing carve-outs still apply.

- **Blockers:** None for publishing the public scaffold with private runtime data.
- **Significant gaps:** Implementation details remain deliberately unselected:
  exact message/ack schema and ID formats, runtime permissions and rejection of
  repository-contained stores, owning source files, command contracts, and run
  commands. Resolve these in the implementation plan before dispatch.
- **Missing items:** Build steps lack Type/Problem/Issue fields and concrete file
  lists. Step 2 combines code integration with attended cross-session acceptance;
  separate those when authoring its dispatchable steps. Do not manufacture issues
  or start integration during project creation.
- **Nice-to-haves:** None proposed.

No contradictions or scope drift found between this plan and `AGENTS.md`.
Private message storage, explicit acknowledgement and no automatic delivery are
consistent. The two build steps are backlog slices, not claims of readiness or
installed skill integration.

### Decision Inventory ? compact redline

| ID | P/D | choice | rationale / tweak | status |
|---|---|---|---|---|
| P1 | P | Public `aberson/passing-notes` repository | Explicit operator selection | selected |
| P2 | P | Messages and acknowledgements remain private | Explicit operator privacy boundary | selected |
| P3 | P | Step 1 built through Forge after Ambition delivery, then explicit Yarvis note/ack before Android | Explicit operator sequence; fresh qualification and human Go required | selected 2026-10-20 |
| D1 | D | Python standard-library CLI | Small local interface; command names can change before implementation | proposed default |
| D2 | D | Immutable note files and separate atomic acknowledgements | Avoid shared-file rewrite races; choose exact schema/IDs before building | proposed default |
| D3 | D | Explicit inbox/read/ack | Receipt is distinct from reading or agreement; timing can be adjusted | proposed default |
| D4 | D | Session/handoff integration after CLI | Prove the store before wiring skills; select exact hook locations later | proposed default |

What this proposes: a local note-and-receipt utility, followed by narrowly owned
skill hooks. It excludes dispatch, waking sessions, external delivery and a new
source of project truth. Feedback may reference an ID, e.g. `D3 ? change inbox
check timing`. This in-plan view is the requested compact proposal; no separate
proposal artifact or new approval gate is introduced.

### Plan wrap

Completion gate: no consistent completion markers found -- running full check (fail-safe default).

?1 Schemas and data structures ? findings: 1 (fields named; exact types/bounds pending)
?2 Identifiers ? findings: 1 (ID generation and acknowledgement key pending)
?3 Acronyms and tool names ? pass
?4 Stack decisions with rationale ? pass
?5 Unresolved decisions ? findings: 1 (implementation choices intentionally pending)
?6 API contracts ? N/A: local CLI, no backend API
?7 Development process ? findings: 1 (dispatchable step shape pending)
?8 Quickstart / how to run ? findings: 1 (CLI not implemented)
?9 Referenced external files ? pass (future owners explicitly unresolved)
?10 Scope and constraints ? pass
?11 Operator/code step-shape integrity (Blocker if violated) ? N/A: backlog has no Type declarations; split Step 2 before dispatch
?12 Conditional steps must declare a Condition: predicate (Blocker) ? N/A: no conditional steps
?13 Substrate-smoke step present when the plan touches deployment seams (Significant Gap) ? pass (Step 2 requires real-session observation)

**Blocker:** None for this scaffold. This is not approval to dispatch the backlog.

**Gap:** Five forward-build gaps listed above. Resolve only when implementation
is selected; they do not block repository creation or Ambition/Forge UAT.

**Minor:** None.

READY WITH GAPS: 5 gaps
