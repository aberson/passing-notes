# Seed: a lane parked on a permission prompt is invisible, so the operator can sleep through it

Captured: 2026-11-18, from a private game project (coordinator `main`, a Claude Code window). Status: seed only. It changes no lane rule and grants no implementation. Operator's framing: "a big issue, 9 hours of not working even after I asked if anything needed approval."

## What happened

The project's build lane (a Claude Code window) was running `/build-step` for a plan step. Review round 6 dispatched six background reviewer sub-agents at 06:31Z on 2026-11-18. Five passed by 06:33Z. The sixth (Bugs lens, `requestNonInteractive: true`) was fuzzing the tool's `--out` handling with odd paths. It created a real folder `C:\con` (a reserved device name) at the drive root, then at **06:34:24Z** tried to clean up with `rm -r 'C:/con' && ...`. Claude Code raised a permission prompt in the build window. The sub-agent stopped there; the build lane's main thread was idle waiting for that agent's completion notification.

Nothing in Passing Notes changed:
- The build card stayed `active`, last set 03:15Z ("UPDATED 3h" in `passing-notes status`, which looked normal for a long build).
- The lane could not post a blocker: its only running work was the parked call, and the lane rule covers *denied* permissions, not *pending* ones.
- No Unity token was held, no note was posted, `origin/main` did not move.

At **06:58Z (10:58 PM PST)** the operator asked the coordinator: "Need anything from me before sleep time?" The coordinator ran `passing-notes status`, read the inboxes and the build lane's transcript, saw "only the bugs lens is still running", and answered "nothing needed". It had misread the same signature in the opposite direction an hour earlier (it declared a lens dead that was really inside a 100-minute fuzz command), so this time it assumed a long command. It did not read the agent's last tool call or check for a child process.

The operator came back at 16:21Z (8:21 AM PST). The coordinator found the pending `rm -r` within minutes; the operator approved it at about 16:22Z and the lens resumed at once. **Lost: about 9 h 48 min** (06:34Z to 16:22Z). Everything downstream waited: build Steps 16 and 17, and the tools lane's Step 18 (it depends on both).

## Why this is a Passing Notes problem

1. **Card state is self-reported; a parked lane cannot report.** A lane blocked on its host's permission prompt has no turn in which to run `card set --state blocked` or post a blocker.
2. **The wake channel cannot reach it either.** At 05:58Z the same day, `wake send` to this lane returned `wake-session-busy ... is waiting; not interrupting it` (it had a running background agent). A coordinator with a background watcher is unringable the same way.
3. **"Do you need anything from me?" is answered by coordinator judgment, not by data.** The operator asked the right question at the right time; nothing mechanical could answer it.
4. **"Long command" and "waiting for approval" look identical** from the transcript: a final `tool_use` with no `tool_result`, a 0-byte task `.output`, an unfinished `meta.json`. The difference is whether the owning `claude.exe` has a live child process.

## Proposal (for the passing-notes maintainer to shape)

- **Host hook for pending approvals.** Passing Notes already installs Claude hooks (`passing-notes hook stop`). Add a hook on the host's permission-request / permission-prompt notification event (Claude Code: check the current `Notification` (permission prompt) and `PermissionRequest` hook events; Codex: its approval-request equivalent) that, for a bound lane, sets the card to a new state such as `awaiting-operator` with the tool and a one-line summary, and posts one `--kind blocker --needs operator-decision` note to the relay lane. Clear it when the call proceeds.
- **Derived liveness in `status`.** Next to the self-reported card, show what the host shows: last transcript write, open sub-agents, and `pending-approval` / `stalled` when there is an open sub-agent, no transcript write for N minutes, and no child process under the session.
- **An operator "before I step away" command.** For example `passing-notes away-check [--project P]`: one table of everything that needs the operator now (pending approvals, `operator-decision` questions, stalled lanes), with exit code nonzero when anything is listed. The coordinator runs it instead of judging; `/lane-status` can show the same column.
- **Unattended mode fails fast instead of waiting.** Optionally, a lane opened for unattended work (or after an `away` flag) has permission requests denied with a blocker note instead of a prompt, so the agent gets an error and carries on with other work.

## Related failures the same night (coding-root main's view)

- **A relay main missed its own lane's doorbells for about 9 hours.** In another project, a build lane posted eight notes to its project `main` from 07:01Z; none of the doorbells reached that window, so nothing was forwarded until coding-root main's note rang it at 16:21Z. Same symptom: the operator saw nothing to approve, and work sat waiting.
- **A cross-project answer cannot ring.** `post --reply-to <id>` from `dev/main` to `P/main`, answering P's `needs reply` question, reports "the note it answers ... was not found" and does not ring, because the lookup searches the sender's project store. `wake send` then refuses it as `wake-not-needed`. Each answer to a question waits for the recipient's next inbox check.
- Both point at the same gap as this seed: whether a lane is unblocked or stuck is inferred from self-reported cards, not from delivery and host state.

## Status view: what the operator needs from it (added 2026-11-18, operator-approved)

After `status --mine --live` shipped (`282f3ad`), the operator asked for an honest review of it as a monitoring view. It cut 113 rows to 20, but it still does not answer "is anything stuck, and on what?". Fold these in with the liveness work above:

1. **Waiting on what.** A structured card field such as `--waiting-on operator|lane:<id>|note:<id>|resource:<name>|time:<until>` (free text stays in `item`), shown as its own column. Today four kinds of wait (an operator answer not yet read, another lane's step, a memory gap, an approval) look the same: `waiting` plus a truncated item.
2. **The caller's own idle rows collapse.** A coordinator holding `main` in many projects fills half the table with its own idle rows. Collapse the caller's own `idle` cards into one line (for example `this window: main in 10 projects (idle)`) unless asked otherwise; keep its active rows.
3. **Truncation keeps the news.** Items lead with boilerplate and end with the news ("Step 17 review r2 N..." loses the verdict). Either a wider column in the text table, a second line for the active rows, or a card convention (news first) that the CLI warns about.
4. **Counts that age out.** `operator: N decision` and the `r` in OPEN keep counting questions that were answered through a relay or a superseding note, so "Look here first" points at handled work and the operator learns to ignore it. Count a question answered when a valid different note with `reply_to` it exists and the reply's sender is the question's addressee or the destination project's `main`. For cross-project questions, the sender project's `main` also counts; for `all` or `operator`, only those mains count. Always exclude the question's own sender, even if it is a main. Sender identity is project plus lane, regardless of where the reply is stored. An eligible reply counts without requiring the addressee's acknowledgement.
5. **A resource header line.** One line above the table for the shared scarce resources the store already knows: queue holders (for example who holds a heavy-run token and until when) and, where the CLI can read it cheaply, free memory. Lanes on one machine wait on those more than on each other.
6. **WINDOW means wakeable.** `bound` on every row says nothing; show whether a doorbell can actually reach the window (Claude live session, or a Codex binding that can resume), which is the derived liveness above.

## Acceptance hints

- Reproduce: in a bound lane, have a background sub-agent run a command that needs approval (a recursive delete outside the workspace). Within a few minutes the card shows `awaiting-operator`, the relay lane holds one blocker naming the command, and `status` / `away-check` list it.
- No false alarm for a long command: a sub-agent running a 100-minute command with a live child process stays `active`.
- The operator's "anything need me?" question is answered by one command, and the answer would have been "yes: build lane, `rm -r 'C:/con'`" at 06:58Z.

## Interim workaround in use (in that project, not committed)

The coordinator now runs one background exit-on-event loop (a watch script plus a stall check in `%TEMP%`) that wakes it on: a new inbox note, a watched step landing, or a lane with an unfinished sub-agent, no session or sub-agent transcript write for 30 minutes and no child process under its `claude.exe`. It is a project-local stopgap, not a fix: it only covers the lanes it names, and it still needs the operator to click the prompt.

## Evidence

- The build lane's session transcript (last message 06:33:34Z, next activity 16:22Z) and the sub-agent's transcript (last entry 06:34:24Z, the `rm -r 'C:/con'` tool call). Kept privately; paths and session ids withheld from this public repository.
- `C:\con` created 06:34:08Z (anim, crops, strips, two grid PNGs, receipt.json); removed by the approved call.
- Build-step run `build-step-20261118T000429Z-9a1e390d` (review rounds 1-6).
- Two notes in that project's private store: the earlier wrong "lens died" blocker with the `wake-session-busy` refusal, and its correction.
- A coordinator memory entry about wake and stall signals in that project.

## Queued with slice 5: status --working (operator, 2026-11-18)

A plain terminal is unbound, so `--mine` has no scope there. Add `passing-notes status --working`: the projects that have at least one lane other than `main` in state active, waiting or blocked, shown with `--live`. It needs no saved project list and follows the same visibility rule as `--all-projects` for the caller. The operator's interim stand-in is a PowerShell profile function `lanes` that computes the same project list from `status --all-projects --live --json`; the CLI option replaces it. Tests: a project with only idle or done non-main lanes is excluded; a project whose only working lane is main is excluded; a bound caller's refusal rule still applies.

