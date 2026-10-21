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
- **Files:** CLI/package, focused tests, synthetic fixtures, README.
- **Produces:** post/inbox/read/ack with private default storage and clear errors.
- **Done when:** Two separate local processes exchange and acknowledge a synthetic
  note; recipient filtering works; concurrent publication preserves both notes;
  repeated ack is safe; malformed IDs cannot escape the store; runtime files stay
  outside the public repository. Use focused standard-library tests, not a new
  test framework or broad performance campaign.

### Step 2: Session and handoff integration
- **Status:** PENDING
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
