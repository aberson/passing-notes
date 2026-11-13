# Seed: wake Codex lanes too, so decisions reach a Codex "Main"

**Provenance:** 2026-11-13 session that built `passing-notes wake`, the lane skills and the
lane rules. The operator approved "Main speaks for you" (lane rules, section 4). In the
project that prompted it, Main is the coordinator, and the coordinator is a Codex window.
Doorbells reach only Claude lanes, so a lane's decision note to a Codex Main waits for
Main's next inbox check. The operator saw this happen: a lane asked a decision in its own
window, and it never showed up in Main.

**Status:** PROPOSED. Nothing built. One candidate mechanism has been found, but not
tested.

## Problem

`passing-notes wake send` rings a lane by sending one message to its bound Claude session
through Claude Code's cross-session messaging. A Codex window has no such channel, so
`wake bind --host` accepts only `claude`. Today a Codex lane learns of new notes only at
its inbox checks. That breaks two flows:

- **Operator relay:** a lane's `--needs operator-decision` note to a Codex Main cannot
  wake it.
- **Lane lifecycle:** close requests, handovers and `lane-close --all` cannot ring Codex
  lanes. A Codex shipper, which closes last, learns that the other lanes are done only
  when it next checks.

## Candidate mechanism: `codex queue`

The Codex CLI (codex-cli 0.167.0) has a supported command:

```text
codex queue [OPTIONS] --thread <THREAD> --message <TEXT>
  --thread <THREAD>   Session UUID or exact session name
  --message <TEXT>    Message text to queue
  --resume            After queueing, try to resume the thread on the local daemon or
                      selected --remote server. A failed resume produces a warning and
                      the command still succeeds
```

If an idle interactive Codex window picks up a queued message and starts a turn, this is
the Codex counterpart of the Claude doorbell. "Queue" suggests it waits for a busy session
instead of interrupting it, which the doorbell contract needs anyway.

## What is already known (from 2026-11-13 tests)

- Codex runs model commands as a separate Windows user. `workspace-write` uses
  `CodexSandboxOffline`, and `CodexSandboxOnline` with network access. That user cannot
  see or message Claude sessions. With escalation, a command runs as the real user.
- The doorbell bridge for Claude works and is verified live. Its sender rules (bound
  session id, idle-only, ring once, evidence-only report) should carry over unchanged.
- `codex exec` prints its session id at startup. Whether an interactive Codex window
  exposes its own session id to its commands (an environment variable, or a file) is
  unknown. Binding needs it, because a window is never picked by its title.
- Background or child-session pitfalls on the Claude side, such as inherited session
  variables, may have Codex equivalents (`CODEX_*` variables). The `lane-open` launcher
  already clears `CODEX_*` except `CODEX_HOME`.

## Spike (each step a pass/fail fact, recorded here)

1. **Own id.** From inside an interactive Codex window, can a command learn the window's
   session id? Check the environment (`CODEX_*`) and Codex's session files. Without it,
   binding must fall back to an id the operator reads from the window, never a title.
2. **Idle delivery.** Open a synthetic Codex lane and leave it idle. From a real-user
   shell, run `codex queue --thread <id> --message "New Passing Note <id>. Read it and
   respond through Passing Notes."`. Does the window start a turn by itself? Try with
   and without `--resume`.
3. **Busy behaviour.** Queue while the window is mid-turn. Is the message delivered after
   the turn ends, or does it interrupt? Interrupting would need a busy check first.
4. **Status signal.** Is there a machine-readable way to tell idle from busy and live
   from closed (`codex agents`, or session files)? The doorbell's `busy` and
   `unavailable` refusals need one, or a documented equivalent.
5. **Sandbox.** Confirm that `codex queue` needs the real user (escalation from Codex,
   none from Claude), and that a Codex sender's escalated `wake send` can reach both
   hosts.
6. **Round trip.** Have a Claude lane post to a Codex lane and ring it. The Codex lane
   reads, acknowledges and replies. Repeat with Codex to Codex, and check a repeated ring
   for the same note and session.

## Build (only if the spike passes)

- `wake bind --host codex --session <codex session id>`: binding records the host. The
  Codex permission mode does not apply, so the record says so instead of guessing.
- `wake send`: for a `codex` binding, run `codex queue --thread <id> --message
  <doorbell>`, as argument lists and outside any sandbox. Report `wake: delivered` only
  on exit 0 plus whatever acknowledgement `codex queue` prints. Keep the ring record,
  the idle-only rule (or "queued, waits for the turn", if spike step 3 shows that), and
  exit 5 for refusals.
- Lane rules section 2: Codex lanes bind at session start too. Section 4: the relay works
  for a Codex Main.
- `lane-open`: the opening prompt of a Codex lane includes the bind command.
- Tests: a fake Codex host as for Claude, plus one live round trip in each direction on a
  synthetic project.

## Acceptance

- A Codex Main receives a lane's `operator-decision` note by doorbell while idle. The
  operator answers in Main, and the lane gets the `operator-said:` reply without anyone
  prompting either window.
- `lane-close --all` run from a Codex shipper sees the other lanes close without manual
  prompts, for every lane that is bound.
- No path types into a window, steals focus, interrupts a busy session, or selects a
  window by title.

## Open questions

- If a Codex window cannot learn its own session id, is an operator-typed id acceptable
  for binding, or does this need a Codex-side change first?
- Should a queued message for a busy session count as `delivered`, or as a separate
  `queued` state? The doorbell contract reports only states backed by evidence.
