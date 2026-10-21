# Passing Notes

Status: project initialized; implementation pending.

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

## Build order

### Step 1: Local private message CLI
- **Status:** PENDING
- **Issue:** #1
- **Files:** CLI/package, focused tests, synthetic fixtures, README.
- **Produces:** post/inbox/read/ack with private default storage and clear errors.
- **Done when:** Two separate local processes exchange and acknowledge a synthetic
  note; recipient filtering works; concurrent publication preserves both notes;
  repeated ack is safe; malformed IDs cannot escape the store; runtime files stay
  outside the public repository. Use focused standard-library tests, not a new
  test framework or broad performance campaign.

### Step 2: Session and handoff integration
- **Status:** PENDING
- **Issue:** #2
- **Files:** Canonical skill-mesh session/task-handoff integration and its docs;
  passing-notes usage documentation. Resolve exact owning files before edits.
- **Produces:** Inbox checks at session startup, meaningful checkpoints and handoff.
- **Depends on:** 1
- **Done when:** Two real model sessions discover the same shared store, one reads
  and explicitly acknowledges the other's note without the operator copying its
  contents. Record actual evidence; a file write alone is not delivered/read.

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

Before implementation, follow the workspace's existing plan review and build
workflow. This project creation does not start another build campaign or block
Ambition/Forge UAT.

## Initial scope review (2026-10-20)

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
