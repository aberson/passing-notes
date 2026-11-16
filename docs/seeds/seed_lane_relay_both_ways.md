# Seed: operator decisions get lost between lanes and Main, in both directions

**Provenance:** 2026-11-16 session in a four-lane project (`main` coordinator, operator
relay and shipper, plus `pipeline`, `sweep` and `profile`), all of them Claude Code
windows opened with `/lane-open`. The operator had to go from window to window to answer
questions and approve prompts, and asked: "why aren't the agents bringing things to you
or why aren't you responding to their messages, I keep having to go into windows and
approve." The operator expects this to work already. `seed_codex_lane_wake.md` describes
the same symptom for a Codex Main (2026-11-13). This session shows it with an all-Claude
setup, so the cause is not only the Codex gap.

**Status:** PLANNED as the lane messaging repair phase (Steps 11-15) in docs/plans/master_plan.md, 2026-11-16. The evidence below comes from the store's ring
records and from `src/passing_notes/wake.py`.

## Problem

Lane rules section 4 says that the operator gives every decision in Main and that Main
relays it. In practice, decisions failed in both directions:

- **Lane to Main:** two `--needs operator-decision` notes sat unread in Main's inbox. Main
  only read its inbox when a doorbell arrived, and neither doorbell was ever delivered.
- **Main to lane:** Main's `operator-said:` reply could not ring the lane. The lane was
  blocked on the same question, which it had also asked in its own window, so the
  operator had to type into the lane window anyway.
- **Not visible to Main:** a lane in `bypassPermissions` mode still stopped on a tool
  permission prompt. Main could see only `waitingFor: "permission prompt"` in
  `claude agents --json`, not what the prompt was asking.

## Evidence (2026-11-16, UTC)

| Time | Note | What happened |
|---|---|---|
| 19:58:43 | `pipeline` to `main`, question, needs operator-decision | No ring record. Main was busy running lane-open's 180-second verification loop. |
| 20:01, 20:06, 20:18:10, 20:52 | Handoffs and questions to `main` | Rang while Main was idle. All `delivered`, and all acted on within a turn. |
| 20:18:24 | `sweep` to `main`, question, needs operator-decision | No ring record. 14 seconds earlier Main had been rung for `sweep`'s handoff and was busy handling it. |
| 20:28 | `main` to two lanes, operator-relay, needs ack, `--wake` | Refused with `wake-not-needed`, and nothing was written. Main had to repost with `--needs reply` to get a ring. |
| 20:48:17 | `main` to `pipeline`, operator-relay, `--wake-if-needed` | Refused with `wake-session-busy`. The session was `waiting` on the question it had asked in its own window. The note was stored, but there is no ring record and no retry. |
| 20:44 | `sweep` session | Stopped on `waitingFor: "permission prompt"` even though it is bound in `bypassPermissions`. Main could not see what the prompt was. |

Doorbells that were delivered worked every time. Every failure was on the path where the
doorbell was not delivered.

## Root causes

1. **A refused ring disappears.** `wake.py:951-956` raises `wake-session-busy` for any
   session that is not idle, and no ring record is written (`rung()` docstring,
   `wake.py:622`). The error tells the sender to "send again when it is idle", but no one
   owns that retry. The sender has already posted the note and moves on.
2. **The relay lane reads its inbox only when a doorbell arrives.** A busy coordinator
   is the normal case, not an edge case, so the relay misses exactly the notes sent while
   it is working.
3. **The lane-open opening prompt causes the deadlock.** Step 3 item 2 of
   `skills/lane-open/core.md` says "Ask operator decisions in this window and also post
   them to <relay>". Asking in its own window leaves the lane `waiting` on input. A
   waiting session refuses rings, so the operator's answer, relayed through Main, cannot
   reach it. This also contradicts lane rules section 4.
4. **`operator-relay` notes are not treated as unblocking.** With `needs ack`, the
   `wake-not-needed` gate refuses `--wake`. An `operator-said:` relay is by definition the
   answer a lane is waiting for.
5. **Main cannot see why a lane is stuck.** Questions asked in a lane's window and
   permission prompts there show up in Main only as a `waitingFor` string. Nothing tells
   the relay what is being asked.

## Cross-project sweep (2026-11-16, about 21:00 UTC)

The operator reported "basically all messaging isn't working" across every project. A
sweep of the store, the bindings and `claude agents --json` across the four projects with
live lanes (alpha4gate, career-ops, shake-spear, say-no-more) found a cause the
career-ops session above did not see, because career-ops Main happened to be bound.

| Project | Main window | Main bound? | Stuck at sweep time |
|---|---|---|---|
| alpha4gate | `alpha4gate-89`, idle | **No** | `evolve-demo` operator decision unread by Main. `evolve-demo`'s `--wake-if-needed` got `wake-no-binding`, so it asked in its own window and is now `waiting: input needed` (deadlock, cause 3). |
| shake-spear | `shake-spear-8e`, idle | **No** | 6 unacked notes to Main: 2 operator decisions (the lanes resolved them in their own windows), 2 handoffs. No ring record exists for any of them. |
| career-ops | `career-ops-db`, idle | Yes | Main is fine. 4 `operator-said:` relays to `pipeline` and `sweep` are unread: `needs ack` cannot ring (cause 4), and `sweep` is `waiting: permission prompt`. |
| say-no-more | `say-no-more - main` (Claude), busy | No (card says Codex, idle) | The card host and the live window disagree; 4 old FYIs to Main are unacked. |

The sweep window bound the two idle Mains by their session ids and rang them; both
doorbells were `delivered` at once. So the wake path works. The failures are all before
it: no binding, or a refusal.

6. **Main never binds (the largest cause).** `/lane-open` puts "Bind for doorbells at
   every session start" only into the *new* lane's opening prompt. The window the operator
   opens as Main never receives that prompt. Every project's always-loaded "Agent
   coordination (Passing Notes)" section (Alpha4Gate, shake_spear, passing-notes,
   skill-mesh `CLAUDE.md`) lists card show, card set and inbox, and omits `wake bind`. The
   workspace `dev/CLAUDE.md` stub omits it too. Only `docs/lane-rules.md` section 2 and
   `workspace-instructions.md` say it, and a Main does not read them in the normal case.
   career-ops Main was bound only because that session noticed. A lane's
   `wake-no-binding` result points at Main's window ("the lane runs wake bind from its own
   window"), but no one relays it, so the lane falls back to asking in its own window.
7. **Nothing mechanical runs.** `~/.claude/settings.json` has no hooks. The dev-root
   `.claude/settings.json` hooks apply only to windows whose project directory is `dev`,
   so windows opened in Alpha4Gate, career-ops or shake_spear run none of them. Every step
   of the delivery path (bind, check the inbox, re-ring after a refusal) relies on the
   model remembering an instruction. A fix that adds hooks must install them at the user
   level, or in each project's settings, or they will not fire in lane windows.
8. **Permission prompts in bypass mode come from sub-agents.** `career-ops` `sweep` is
   bound in `bypassPermissions`. Its `waiting: permission prompt` was a sub-agent's Bash
   call (a heredoc `python -I -` edit of a CV JSON), not anything in the main thread. This
   part of cause 5 is a host behavior, outside Passing Notes. The relay still cannot see
   it.

Refusal counts in the 14 top-level Claude transcripts changed since 2026-11-15 (raw
grep hits, so each refusal is counted roughly two or three times): `wake-not-needed`
182, `wake-session-unavailable` 48, `wake-session-busy` 44,
`wake-no-binding: lane main in alpha4gate` 24.

## Candidate fixes (for plan-review to weigh)

- **F. Every Main binds (fixes 6).** Add the `wake bind` line to the "Agent coordination
  (Passing Notes)" section of every project's `CLAUDE.md`/`AGENTS.md` and to the
  workspace stub. Keep one owner for that section's wording, because it was hand-copied
  and the copies dropped the line. `/lane-open` and `/lane-handoff` check the opener's own
  binding in their pre-checks and bind it when it is missing, since an opener is by
  definition the relay its new lane will ring. A `wake-no-binding` result for the relay
  lane gets its own lane instruction: post a `blocker` to `operator` and say so in one
  line. Do not fall back to an in-window question.
- **G. Hooks live where lane windows load them (fixes 7).** Install any Passing Notes
  hook (fix A) at the user level, and make the hook a no-op for a session that is not
  bound to any lane. An unbound session in a project with active cards could get one
  advisory line telling it to bind.

- **A. A session does not stop with unread notes (recipient side; fixes 1 and 2).** Each
  bound Claude lane window gets a Stop hook, and possibly a UserPromptSubmit hook, that
  runs `passing-notes inbox --project <P> --lane <L>`. When it lists an unread note with
  needs `reply` or `operator-decision`, or of kind `operator-relay` or `handoff`, the hook
  blocks the stop, says why, and the session reads the note before going idle.
  `lane-open` or `wake bind` would install the hook. This removes the need to retry after
  a busy refusal, and it costs no extra turns.
- **B. A refusal leaves a record (sender side; fixes 1).** A busy refusal writes a ring
  record with state `refused-busy`, plus the session status it saw. `card show` or a new
  `wake pending` lists these records, so a refused ring can be seen and retried.
  Alternatively, the next `wake send` to an idle session by any lane delivers the pending
  rings first.
- **C. Change the lane-open opening-prompt wording (fixes 3).** New wording: "Send every
  operator decision to <relay> only, with `--needs operator-decision` and
  `--wake-if-needed`. Do not ask it in this window and do not block: park the item and
  continue queued work. A tool permission prompt is posted to <relay> as a `blocker`
  naming the exact action and path." This applies to `lane-handoff` too, and to whatever
  other skill text carries the same line.
- **D. `operator-relay` is always ringable (fixes 4).** Treat `kind: operator-relay` as
  unblocking whatever its `needs` value is, in the `--wake` gate (`UNBLOCKING_NEEDS`,
  `wake.py:106`).
- **E. Show lane state to Main (fixes 5).** `card show` gains the bound session's host
  status (idle, busy, `waiting: input needed`, `waiting: permission prompt`), read from
  `claude agents --json`, so the relay can tell the operator which lane is stuck and why.
  Also find out why a `bypassPermissions` session still shows a permission prompt: a
  protected path, or a mode the window did not actually start in.

## Acceptance (replayable)

1. Main is busy for 5 minutes. A lane posts an operator-decision note during that time.
   When Main's current turn ends, Main shows the question without any operator action.
2. A lane has an open question. The operator answers in Main, and Main posts an
   `operator-said:` relay. The lane picks it up and continues with no typing in the lane
   window.
3. `post --kind operator-relay --needs ack --wake` rings an idle lane.
4. A lane stopped on a permission prompt appears in Main's view with that reason, and the
   lane has posted a `blocker` note naming the action.
5. No ring is ever refused without leaving a record, or without the recipient's hook
   catching the note at its next stop.
6. In a fresh Main window opened by the operator in any project with a coordination
   section, the window binds itself without being told, and a lane's first
   `--wake-if-needed` to `main` is `delivered`, not `wake-no-binding`.
7. A bound lane window in a project other than `dev` (for example Alpha4Gate) runs the
   recipient-side hook from fix A.

## Related

- `docs/seeds/seed_codex_lane_wake.md`: the same symptom with a Codex Main. Fix A does
  not cover Codex until Codex has an equivalent hook.
- `docs/seeds/seed_coordinated_lanes_playbook.md`
- `skills/lane-open/core.md` step 3 (opening prompt); lane rules section 4 (Main speaks
  for you).
