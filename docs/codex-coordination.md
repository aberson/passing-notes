# Codex coordination and approval boundaries

Passing Notes carries work questions and decisions. It does not grant native
command permission. Central may answer routine questions only within authority
the operator has explicitly delegated. See [lane rules](lane-rules.md).

## Three separate paths

| Path | Decision owner | Supported transport |
|---|---|---|
| Work question, scope choice, graceful close | Central, within recorded delegation | Note, queue doorbell, acknowledgement, linked answer, completion reply |
| Native command approval | Client owning the Codex host connection | App-server approval request and response |
| Approve for me | Codex's own reviewer | `--approve-for-me`; not central and not a Passing Note |

The [official app-server protocol](https://learn.chatgpt.com/docs/app-server)
documents server-initiated approval requests. A client receives
`item/commandExecution/requestApproval`, checks its thread, turn, item, command,
working directory and available decisions, and responds to that JSON-RPC request
ID on the same connection. `serverRequest/resolved` records resolution;
`item/completed` records whether the command ran or was declined. Do not approve
unknown requests, reuse a request ID after reconnect, or infer completion from
an accepted response.

`codex queue` has no native-approval response argument. No supported attachment
path has been verified for taking over pending approvals in an existing standalone
window. A tested alternative is a fresh thread owned through `codex app-server
--listen stdio://`: central can receive and answer its native requests directly.
This is a host integration alternative, not a claim that the visible lane launcher
provides such a connection. It needs a client that remains connected and handles
requests throughout the thread's life.

### Status of the central-owned app-server path

It is a **manually exercised protocol demonstration, not a shipped integration.**
Passing Notes contains no app-server host client. The 2026-11-14 accept and cancel
results came from a connection that central drove by hand for the test. To make it
an operator-facing path, a client would have to start or resume the thread, stay
connected for the thread's whole life, show each request to central with its thread,
turn, item, command and working directory, and answer on the same connection. It
would also need to survive central's own restarts without replaying request IDs.
That is a long-running service with its own trust model. It is not built here, and
it must not be replaced by carrying approvals in notes.

### Why an existing standalone window cannot be taken over

A standalone `codex` TUI sends a native approval request only to its own client
connection, the one that started the thread. The protocol answers a request by its
JSON-RPC ID on that same connection. `codex queue` adds a user message to the
thread's queue. It has no channel for that response. No supported way to attach a
second client to a running TUI's connection has been verified. So a queued note,
including central's `coordinator-decision:`, cannot approve, deny or cancel a
pending native prompt in such a window. The 2026-11-14 test showed it: a queued
message left the command `waitingOnApproval`.

### Supported operator-facing paths

| Lane launch | Work questions and decisions | Native command approval |
|---|---|---|
| `-ApprovalMode auto-review` | Central, through notes and queue doorbells | Codex's own reviewer (`--approve-for-me`), not central |
| `-ApprovalMode user` | Central, through notes and queue doorbells | The operator, in the lane's own window |

Both paths send routine work decisions to central without operator copy/paste. The
first also needs no operator click for native requests the reviewer accepts. In
`user` mode, the sandboxed `wake send` escalation prompts the operator each time.
Where the host prompt offers a command-prefix or session-wide approval, the operator
may choose it once for `passing-notes wake send`. That is the operator's own native
decision; a note or central cannot make it for them. Its availability in 0.167.0
was not re-verified for this change.

[PermissionRequest hooks](https://learn.chatgpt.com/docs/hooks) also document an
allow/deny interface, but hooks require review and trust. This change neither
installs an approval hook nor bypasses hook trust. Hook-based central forwarding
has not been exercised here.

## Session identity and delivery

Register the lane card and bind from the actual lane window:

```powershell
passing-notes wake bind --project example --lane worker --host codex --session $env:CODEX_THREAD_ID
```

The launcher clears inherited session variables. Binding accepts the current
host UUID and rejects disagreeing `CODEX_THREAD_ID` / `CODEX_SESSION_ID` values.
It never discovers a destination by a window title, most-recent transcript or
another host's variables. Environment equality is an accidental-misbinding check,
not authentication against another process owned by the same Windows user.

Rebind after resume, restart or changing hosts. A resumed thread may keep its UUID;
a new thread has a new UUID. Binding replaces the previous Claude or Codex binding.
Sending refuses a binding whose host or UUID disagrees with the lane's card.

Codex queues can be accepted while idle, busy or awaiting approval. A successful
queue receipt proves only **queued**. `--resume` may warn that no daemon exists
even though a live session later consumes the message. Preserve that warning;
do not convert it to either delivered or failed. A pending native approval still
requires a native decision. A closed host may require explicit resume before its
queue is consumed. Never resume an arbitrary saved thread to make a delivery pass.

A queue attempt is recorded before `codex queue` runs. A timeout, a missing
receipt or an interrupted attempt is recorded as `unknown` (`wake-queue-unknown`),
because the message may already be queued. That doorbell is never queued again
for the same note and thread. A definite failure (`codex` missing, or a non-zero
exit) may be retried. A Codex lane with no card is not rung.

`wake send` refuses any lane whose card is `done`, for either host. Each doorbell
passes `--resume`. Without this refusal, a late doorbell could restart a finished
Codex thread on a local daemon, where no one would see it. The note stays posted.
A lane that is deliberately reopened sets its card active and rebinds first.

### Coverage by session state (CLI 0.167.0)

| State | What was observed | Not established |
|---|---|---|
| Idle | Interactive and app-server lanes consumed a queued doorbell, read, acknowledged and acted | Delivery to an idle TUI with no other activity, timed |
| Busy | Messages can be queued mid-turn without interrupting it. In the Codex app, each queued doorbell waits as a "Steer" row and runs as its own turn after the current turn ends, often after the note was already handled at an inbox check | Exact pickup latency. One busy close was observed: the request queued during the worker's 90-second command, was acknowledged after that turn ended, and handover, done card and closure reply followed |
| Pending native approval | A queued message did not resolve the request; central answered it natively on its own app-server connection | Any answer for a standalone TUI's pending prompt (not supported) |
| Restart | A fresh app-server resumed the same thread UUID; the lane rebound and verified its earlier marker | Restart of a standalone TUI under `codex resume` |
| Closure | Two lanes acknowledged the close, handed over, replied and wrote done cards; late doorbells were idempotent | `session-wrap` success (it failed in non-Git fixtures and was reported incomplete) |

Report each state independently:

| State | Required evidence |
|---|---|
| Posted | Note exists |
| Queued | Matching Codex queue receipt and per-thread ring record |
| Acknowledged | Recipient's acknowledgement exists |
| Answered | Recipient's linked decision reply exists; inspect its meaning |
| Completed | Completion evidence for the requested action; a generic reply is insufficient |

## Launch preflight

The launcher probes supported `config/read` before opening a Codex window. It
reports trust, sandbox, approval policy, reviewer and configured writable roots.
Unknown values are not silently treated as permission. Named permission profiles
require host-specific validation. No model turn is started by this probe.

For authorized coding work, select `-WorkspaceWrite`. The store is also passed as
`--add-dir`; `--add-dir` alone cannot make a read-only environment writable. A
successful in-session card write remains the actual writable-root check.

Select `-ApprovalMode user` for native user approval or explicitly select
`-ApprovalMode auto-review` for Codex's reviewer. With automatic review the launcher
passes **only** `--approve-for-me`, because Codex rejects combining it with
`--sandbox`. Workspace-write alone does not eliminate approvals for commands,
tools or image inspection.

For an already-authorized, reviewed workspace, `-TrustWorkspace` supplies an
invocation-scoped project trust setting. It is explicit and changes no global
config. Without established trust the launcher stops before opening a window,
instead of surprising the operator with a trust prompt in the new window.

Windows sandbox commands may not see the real user's Codex home. A note can post
successfully while `wake send` fails there. Run that host-facing wake through
supported native escalation; a central-host client or Codex's automatic reviewer
must decide it. Do not add the whole Codex home as a writable root to avoid review.

## Live acceptance, 2026-11-14 (CLI 0.167.0)

Private runtime evidence stays outside Git. Only synthetic outcomes belong here.

- An app-server thread requested native approval, central returned `accept`, the
  server resolved the request, and the synthetic command completed with exit 0.
- A second command remained `waitingOnApproval` after a message was queued.
  Central returned `cancel`; the command was reported `declined` and did not run.
- A worker posted a work question; central acknowledged and answered; the worker
  consumed the queued answer, acknowledged it and created the exact synthetic
  marker without asking the operator again.
- That worker's sandboxed return wake failed with a Codex-home error. Its note
  remained posted. A subsequent native escalation request reached central and
  was approved through app-server. The wake correctly skipped an already
  acknowledged note; that skip is not evidence of a new queue delivery.
- An initial interactive automatic-review launch failed because `--sandbox` and
  `--approve-for-me` conflict. After fixing that combination and verifying no
  session had started, a corrected launch registered its own card and binding
  without an operator trust click, posted a question and queued its doorbell.

- The corrected interactive lane acknowledged central's answer and created its
  exact marker without repeated operator confirmation. Its own return doorbell
  was queued under the automatic-review launch configuration.
- The hosted process was stopped while idle and a fresh app-server resumed the
  exact thread UUID. The lane rebound itself and verified its earlier marker.
  Its native escalation for the return wake was accepted by central; the actual
  queue receipt and completed command were observed before central acknowledged.
- The resumed worker's normal `view_image` completed on a synthetic workspace PNG
  with no native prompt. Its color description was incorrect (blue for a red
  fixture), so only image access passed; visual interpretation did not. This does
  not establish that every host or image path is prompt-free.
- The interactive lane acknowledged a delegated close, posted its handover and
  linked closure reply, released its empty hold set and wrote its own done card.
  It reported `session-wrap` incomplete because the synthetic directory had no
  Git root. Handover preserved the exact failure, lessons and next steps. No human
  relay or repeated confirmation was needed.
- The hosted lane also completed graceful closure with its own done card,
  handover and linked completion reply. Central acknowledged both workers'
  handovers and closure replies. Both retained their synthetic files.

These tests establish the new-lane workflow with explicit automatic review. They
demonstrate, by hand, the separate central-owned app-server approval protocol;
no client for it ships. They do not establish
takeover of native approvals in existing standalone windows or an end-to-end
unattended central service. A 2026-11-14 run showed the cost of busy delivery: five
doorbells to a busy central each became a short, late turn, and two came from a closed
worker answering stale doorbells with new notes. The lane rules now ring only to
unblock, and the doorbell text tells a lane to stop on a stale doorbell. Central must check its inbox at the documented
checkpoints while busy; it can only act on a queued message after its host exposes it.
