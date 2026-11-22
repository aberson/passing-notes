# Coordinator handover: task-type catalog (2026-11-22)

Requirements input for a future `coordinator-handover` skill (`--from claude|codex --to claude|codex`).
Source: one Claude Code session (id `e68d6a88…`, 16:17Z to 16:56Z) in which a new Claude window took over the central `dev/main` lane and every project-main stand-in from a Codex window (thread `01a1f461…`). Evidence pointers are note ids, commit shas and transcript record numbers `[n]`. No note bodies are reproduced.

## 1. Takeover arc

1. 16:17Z: the operator tells a fresh Claude window "you are the new central lane". It surveys every project's cards, finds 6 projects with active lanes, and finds that one busy Codex window holds `dev/main` plus 4 project-main stand-ins (dev-observatory, passing-notes, say-no-more, skill-mesh).
2. It claims `dev/main` (card plus binding) at once. It then posts an announcement to each project's `main`. The first batch lacks `--from-project dev` and has to be re-posted.
3. The operator tells both windows to transfer. The Codex window sends per-project transfer packets (`…135e`, `…6e14`, `…1fe6`, `…23c8`, `…0e66`, `…ac3c`, `…de42`) and marks each main card done. The Claude window acks them and stands in as each project's `main` (16:28Z to 16:31Z).
4. It closes idle and windowless lanes. In practice-plumber and skill-mesh it opens short close-only Claude windows (lane-open), then closes practice-plumber/main itself. Both close-only windows print SAFE TO CLOSE and stay open.
5. On the operator's word it replaces the idle Codex work lanes (say-no-more `gen-request`, `ship-provider`) with Claude Opus successors through lane-handoff. It closes one Codex launcher with the Done-window helper and confirms the transfers. It assigns next items.
6. It sorts a 290-note say-no-more backlog (Sonnet agent plus an HTML report, then 285 bulk acks) and a 26-note passing-notes backlog. It kills all remaining `codex.exe` processes on the operator's word. It retires the orphaned `dev-observatory/dev-main` lane by standing in for it.
7. It finds 3 finished candidates that never shipped (`b812a49`, `5a79e6f`, review-pin's uncommitted paths) and leaves the shipper-lane decision with the operator. It saves 2 memories and spawns this review.

## 2. Task-type catalog

There are 43 task types in 15 phases (0 to 14). "H" marks host-specific differences, and "Human" says whether the operator was needed.

### Phase 0: Trigger and operator intent (both windows)

**T0.1 Receive the takeover instruction in both windows.**
- Trigger: the operator typed into the new Claude window [2], and separately into the old Codex window ("…pass over central control", quoted inside `…0e66`).
- Commands: none. The operator's verbatim words become `operator-said:` lines in later notes.
- Preconditions: the incoming window must not treat a peer cross-session message as operator consent.
- Verified by: the Codex packets quote the operator's words.
- H: the outgoing Codex window would not have acted on dev/main's announcement alone until it finished its turn. The operator's direct word in the Codex window started the transfer.
- Human: yes. Only the operator can authorize a coordinator change. The skill needs one operator instruction, plus either a relay into the outgoing window or a second instruction typed there.

**T0.2 Absorb mid-turn operator messages.**
- Trigger: 4 operator messages arrived as queued mid-turn commands (records 161, 226, 609, 706): "you should take over…", "close down the idle lanes…", "replace idle Codex with Claude Opus…" and "SAFE TO CLOSE windows weren't closing; status?".
- Verified by: each was quoted verbatim in the next relay note.
- Human: yes. The skill must re-read the operator's queue between phases. Mid-turn decisions changed the plan three times.

### Phase 1: Discover

**T1.1 Workspace card survey.**
- Commands: `passing-notes card show --project dev`, then `card show --project <p>` for each project, filtering out `done` [29], [60].
- Verified by: the list of active lanes per project.
- Note: `--all` is not a `status` flag (exit 2). The right flag is `--all-projects` [39].

**T1.2 Live status across projects.**
- Commands: `passing-notes status --all-projects --live` [39]. Later `status --mine --json --live` through lane-status [494], [991].
- Output gave: free memory (2.8 GiB), queue holders, "bindings are not process counts".
- A one-liner that piped `status --json` to python failed because the JSON shape was guessed [61]. Use the lane-status core's `ConvertFrom-Json` field set instead.

**T1.3 Read governing docs.**
- Read: lane-rules (sections 4 and 6), the workspace-instructions Topology and Dev main sections, and a grep of passing-notes docs for "take over" [23-54].
- Purpose: confirm that only dev/main may post across projects, and only to a project's `main`.

**T1.4 Identify own session and host identity.**
- Commands: `$env:CLAUDE_CODE_SESSION_ID` [60].
- H: Claude uses `CLAUDE_CODE_SESSION_ID`. Codex uses `CODEX_THREAD_ID`, and its card needs `--window`.

**T1.5 Map which window holds which identities.**
- Derived from: the status session grouping ("Session 1 … 4 distinct bindings") and `wake show --project <p>` (thread `01a1f461…` held 5 mains plus `dev-observatory/dev-main`) [617].
- Not done up front: classifying each lane as live and bound, idle with a window, or windowless. That had to be reconstructed per lane later (see G2).

### Phase 2: Claim identity and topology

**T2.1 Claim `dev/main`.**
- Commands: `passing-notes card set --project dev --lane main --host claude --item '…coordination-only…' --state active`, which printed `wake: bound main to e68d… (dev-35)` [100].
- Precondition missed: the outgoing window still believed it was dev/main. Nothing first released or handed over `dev/main` itself (see M10).
- H: the Claude `card set` auto-binds. Codex needs `--window $env:CODEX_THREAD_ID` plus `wake bind --host codex --session $env:CODEX_THREAD_ID`. Claude to Claude: `card set` will not take a binding from another live Claude window (`stays bound to live session`). The outgoing window must go done, or the incoming must `wake bind` after it is gone.
- Human: no.

**T2.2 Inbox check as dev/main.**
- Commands: `passing-notes inbox --project dev --lane main` [109].
- Result: one store diagnostic (an invalid subject on an old note), ignored.

### Phase 3: Announce

**T3.1 Cross-project announcement to each project main.**
- Commands: a body file in Temp, then `passing-notes post --project <p> --from-project dev --from main --to main --kind operator-relay --needs reply --subject … --body-file … --wake|--wake-if-needed --idempotency-key central-announce…-<p>` [116], [178].
- Rules: the first body line is `operator-said: <verbatim>`. Ask each main to confirm, and to relay the words to its own lanes, because dev/main cannot reach work lanes.
- Verified by: note ids in the store, wake lines and later replies.
- H: Codex recipients reported `queued … could not resume thread … already has an active writer (code -32600); delivery and reading unverified` (exit 5). A windowless main gave `wake-no-binding`. Claude recipients get a UDS doorbell, delivered only when idle.
- Human: no.

**T3.2 Correct faulty posts.**
- The first batch lacked `--from-project dev` [116], so each note looked like a self-post from the project's own `main` and could not be acked properly.
- Fix: re-post with `--from-project dev`, add a line saying "ignore the earlier note `<id>`", and use new idempotency keys [178].

### Phase 4: Receive handovers (outgoing to incoming)

**T4.1 Outgoing coordinator writes per-project transfer packets.**
- The Codex window's side: one `handoff` or `operator-relay` from `<p>/main` to `dev/main` per stand-in project, then `card set <p>/main --state done`. It also relayed the operator's words to its live work lanes (`…3c80`, `…1b16`) and posted addenda for live blockers (`…6e14`, `…1fe6`).
- The skill must generate this for the outgoing host. Packet fields: live lanes, pending operator decisions verbatim, blockers, immediate next actions, holds and queues, and "main holds no files" or what it does hold.

**T4.2 Incoming reads, acks and confirms each packet.**
- Commands: `passing-notes read --project dev <id>`, `ack --project dev --lane main <id>`, then a confirming post to `<p>/main` [127], [150], [201].
- Failure: `--reply-to <id>` across stores gave `wake-not-needed: … not found` (exit 2), because the answered note lives in the `dev` store [127]. Workaround: `--ref <id>` with `--kind operator-relay --needs reply`, or `--kind answer --needs ack --wake-if-needed`.
- Explicit permission is needed: "you may mark `<p>/main` done". The Codex window waited for that before going done.

**T4.3 Wait for the outgoing cards to turn done.**
- Commands: repeated `card show --project <p> --lane main | Select-String state` [378], [392]. These were turn-bound checks, not a loop. The Stop hook surfaced the transfer notes.
- Verified by: every outgoing main card showed `done`, plus a transfer note in `dev/main`'s inbox.
- H: a busy outgoing Codex window cannot be rung. Its transfer arrived about 5 minutes later, when its turn ended.

### Phase 5: Stand in for project mains

**T5.1 Bind each project main to the incoming window.**
- Commands: `passing-notes card set --project <p> --lane main --host claude --item 'Stand-in by Claude dev/main (central, <date>): coordination-only; <top state>' --state active`, then `inbox --project <p> --lane main` [343], [417], [446].
- Order constraint: only after the outgoing card is done. Exception: practice-plumber's main was already windowless, so there was nothing to wait for.
- Verified by: `wake: bound main to <session>`, and `status --mine` listing 5 caller identities [494].
- H: same as T2.1. A Codex incoming coordinator would bind once per project with the same `--window`.

**T5.2 Carry the coordinator's control-lane boundary into every stand-in.**
- The card item says "coordination-only". Implementation and shipping stay with lanes (workspace instructions, Dev main).

### Phase 6: Backlog triage and acknowledgement

**T6.1 Small backlog: read and ack.**
- Read and acked one by one: dev-observatory `…b286`, skill-mesh's 4 notes, and the Stop-hook batches of 5 [422], [521-541].

**T6.2 Large backlog: triage report, then bulk ack.**
- Trigger: say-no-more/main had 290 unacked notes left by the outgoing coordinator (243 fyi). The Stop hook showed them 5 per turn [541].
- Commands:
  - `inbox` to a TSV under `%LOCALAPPDATA%\passing-notes-lanes\say-no-more\` (outside git, for privacy).
  - One read-only Sonnet agent (`read`, `card show` and `inbox` only) writes `backlog-triage-20261122.html`, with statuses `settled | superseded | fyi-history | needs-coordinator | needs-operator | unclear` judged against the transfer packet [560].
  - A regex maps report pills to ids, and a bulk `ack` loop acks 285 with 0 failures. 5 open items are kept [910-922].
- Human: yes. The operator asked for the report, and the bulk ack was taken from a later, ambiguous "yes" (see M13).

**T6.3 Bulk ack without triage (passing-notes, 26 notes).**
- Acked all 26 after reading one body [940-962]. This is weaker evidence than T6.2.

**T6.4 Record residual open items on the card.**
- Commands: `card set … --next '<≤200 chars>'`. The first try failed with `next_checkpoint: at most 200 characters (got 257)` [1025].

### Phase 7: Close idle and windowless lanes

**T7.1 REQUEST-mode close through the project main.**
- Commands: the `lane-close` skill [247], then `post --project <p> --from-project dev --from main --to main --kind operator-relay --needs reply --subject 'Close request…' --wake-if-needed`.
- The body asks the main to send its work lane a close request, because dev/main cannot address work lanes [254].
- Result: `wake-no-binding` (practice-plumber) or queued with active writer (skill-mesh). Nothing could close because no windows existed.
- A duplicate request was posted while the 11-18 request `…5cc3` was still open (see M4).

**T7.2 Open a close-only window for a windowless lane.**
- Trigger: the operator picked option 1 ("open a short window for each…") [289].
- Commands: the `lane-open` skill, then:
  - Precheck the title: `claude agents --json`, no `<project> - <lane>` match.
  - Write a brief to Temp. It must say: register, then `/lane-close` SELF `--successor main`, the operator's words are the confirmation, no other work, and leave the listed worktrees untouched.
  - `powershell -File …\lane-open\scripts\open-lane.ps1 -Project <p> -Lane <lane> -HostName claude -WorkingDirectory <dir> -PromptFile <f> -Model sonnet -Effort medium` [339-366].
  - A bounded wait of at most 180 s on `card show` `updated_utc`.
- Preconditions: dev/main must already stand in as that project's main. skill-mesh had to wait for the Codex transfer [397]. Check free RAM (2.8 GiB).
- Verified by: the card shows `done`, plus a handover note to main (`…0a97`, `…9be3`), which main acks.
- Gaps: the first window's session was not seen within 180 s, possibly because of a folder-trust prompt [362]. Both windows stayed open after printing SAFE TO CLOSE.
- H: a Claude close-only window can never be closed by the helper. A Codex close-only window launched by lane-open could be closed by it.
- Human: yes. The operator decided, and had to close the windows by hand.

**T7.3 Stand-in self-close of a main.**
- practice-plumber/main, held by dev/main: write the handover to Temp, then `post --project dev --from-project practice-plumber --from main --to main --kind handoff --needs ack`, then `card set … --state done --item 'closed: handed over to dev/main (<id>)'`. dev/main then acks its own handover [497-502].

**T7.4 Retire an orphaned work lane whose window is gone.**
- dev-observatory/dev-main: Codex, stale since 11-20 and bound to the killed thread. Steps [875-900]:
  - `card set --lane dev-main --host claude --item 'Stand-in … to close' --state active`, which bound it to this window.
  - Read and ack its inbox.
  - `git status` its build-step worktree (5 uncommitted paths).
  - `post --from dev-main --to main --kind handoff --needs ack`, then `card set --state done`, then ack as main.
- Precondition used: the window is provably gone (`codex.exe` count 0).
- This writes another lane's card (see M11).

### Phase 8: Replace host lanes (Codex to Claude Opus)

**T8.1 lane-handoff per idle Codex lane.**
- Trigger: the operator asked to wind down idle Codex windows and open Claude Opus successors [609].
- Commands: the `lane-handoff` skill, then:
  - `card show` each old lane (host, window, checkout, branch, item).
  - `queue show` (no holds).
  - Confirm each successor id is free (`card show --lane <x>-opus` returns not-found) [640].
  - Post a close request per old lane with `--successor <x>-opus`, the operator's words verbatim, the full-state handover list and "leave every worktree in place" [645].
  - Bounded wait on the cards [656].
- Verified by: `ship-provider` done 16:41Z with handover `…d3a0` to its successor; `gen-request` done with `…c2c6`.
- H: every Codex target reported active writer. They still closed within about 3 minutes, at their turn ends.

**T8.2 Open the successor in the same checkout.**
- Precheck: `git -C <checkout> rev-parse --abbrev-ref HEAD`, the lane doc `Shipper:` and `Operator relay:` lines, and free MB.
- Then `open-lane.ps1 … -HostName claude -Model opus -Effort high` [685-730].
- Verified by: the successor card is active, `wake show` lists the new session, and the handover ack file exists under `acks\<p>\<id>--<successor>.md` [700].

**T8.3 Confirm the ownership transfer and assign first work.**
- Each successor posts "handover verified; confirm transfer". Main replies with `--reply-to` (same store) `--kind answer --needs ack --wake`, and the doorbell is delivered [782-799].

### Phase 9: Process and RAM cleanup

**T9.1 Done-window helper.**
- Commands: `python …\lane-close\scripts\close_done_window.py --project <p> --list`, then `--close --pid <pid> --created <utc> --launcher <ps1>` for the exact row. Closed ship-provider's launcher (6 processes) [677].
- It refuses rows unless the card is `done`, the host is Codex and there is an exact window identity [414]. It lists nothing for operator-launched windows (the old Codex central).

**T9.2 Process census.**
- Commands: `Get-CimInstance Win32_Process -Filter "Name='codex.exe'"` with parent, start time and MB [617], [812].
- Classify each process: a lane-open launcher child, the VS Code ChatGPT/Codex extension (parent `Code.exe`), or the app-server daemon pair.

**T9.3 Stop Codex support processes on operator approval.**
- Commands: `codex app-server daemon stop`, then `taskkill /PID <id> /T /F` for the leftover daemon helper and the VS Code extension [860-875].
- Verified by: `codex.exe` count 0 and free memory 3.2 GiB.
- Side effect: this ended the old central Codex thread, which made T7.4 possible.
- Human: yes. The operator said "yes, can you close these".

**T9.4 Memory gating.**
- Free RAM was read before each window open (2.8 to 3.2 GiB). Successor and shipper openings were limited by it ([1117]: shipper lanes deferred).

### Phase 10: Dispatch and assignment

**T10.1 Assign bounded next items to successors.**
- `gen-request-opus` got Item A: exclusive paths, required proof, and forbidden actions.
- `ship-provider-opus` got intake of reviewed `1702df7` and `362dd6a`, with the final gate on HOLD [782].
- Verified by: later handoffs `…3c60` and `…aa2d` (local commits `bbaced9` and `46f5620`).

**T10.2 Open a research lane.**
- `passing-notes/codex-coord-review` (Claude Opus) [589-605], opened through lane-open in a project where dev/main is the stand-in main.

**T10.3 Relay scope addenda to a running lane.**
- `post --project passing-notes --from main --to codex-coord-review --kind operator-relay --needs reply|ack` with `operator-said:` lines [825], [836].
- The wake was "not delivered" because the lane was busy. The note waits for the lane's inbox check.

### Phase 11: Unshipped-work discovery

**T11.1 Read close reports for preserved work.**
- Source: SELF-close summaries in backlog notes, for example "uncommitted … in preserved worktree" [532], [962].

**T11.2 Check git landing state.**
- Commands:
  - `git -C <repo> branch -a --contains <sha>`
  - `git merge-base --is-ancestor <sha> main`
  - `git log --oneline -1 <sha>`
  - `git status --short -b`
  - `git -C <worktree> status --short`
- Results [962-985]:
  - `b812a49` is on a branch only and unpushed.
  - `5a79e6f` is not on main.
  - review-pin has 7 uncommitted paths.
  - practice-plumber has 2 worktrees with 10 paths each, plus docs.
  - the Core10 worktree has 5 paths.

**T11.3 Record findings on the owning project-main card and ask for shipper lanes.**
- dev/main is coordination-only, so it does not land anything itself [988].
- Human: yes. Opening shipper lanes was left as an operator decision because of RAM.

### Phase 12: Operator decision carry-over

**T12.1 Carry pending operator decisions verbatim and never infer approval.**
- 3 say-no-more decisions (paid-call options and a correction round) were listed at [217] and re-listed at [920] and [1225].
- The triage report surfaced 2 more `needs-operator` items from the backlog.

### Phase 13: Reporting and status

**T13.1 Operator reports at each phase end.**
- Each report separated posted, queued, delivered, acked and done [127], [217], [489], [745], [928], [988].

**T13.2 lane-status runs.**
- 3 operator-invoked runs (`--mine --live`, and `--project say-no-more`). These are read-only and never make the window a lane [494-520], [991], [1048].

**T13.3 Tell the operator which windows only they can close.**
- Named: the old Codex central (until the T9.3 kill) and the 2 Claude close-only windows [489], [745], [928].

### Phase 14: Memory and lessons

**T14.1 Save cross-session lessons.**
- Wrote `feedback_passing_notes_cross_project_from_project.md` and `feedback_codex_for_image_gen_only.md`, and edited MEMORY.md [150], [812].
- Relayed the operator's "Codex for image generation only" direction to the research lane.

**T14.2 Spawn an independent review of the session (this catalog).**
- The review gets the raw transcript, not a summary, so its read is not anchored on the session's [1039].

## 3. Mistakes, dead ends and corrections

| # | What happened | Evidence | Fix the skill should build in |
|---|---|---|---|
| M1 | Cross-project announcement posted without `--from-project dev`. Each project saw its own `main` posting to itself (unackable), so all 5 had to be re-posted. | [116], [178] | Build every cross-project post through one helper that always sets `--from-project <sender project>` when the target project differs. Refuse to post otherwise. |
| M2 | `--reply-to <id>` across project stores failed with `wake-not-needed: … not found`. | [127] | When replying from project A to a note stored in project B's store, use `--ref <id>` with `operator-relay` or `needs reply`. Alternatively, file a CLI fix so cross-project `--reply-to` resolves against the sender's store. |
| M3 | Wake refusals were mixed together. Codex reported `active writer -32600` (queued); windowless mains reported `wake-no-binding`; a busy Claude lane got "not delivered". | [123], [280], [825] | Classify each target before posting (bound and idle, bound and busy, unbound). Report posted, queued, delivered, acked and done separately. Never re-send a queued doorbell (codex-lane-liveness). |
| M4 | A new close request was posted to practice-plumber/main while the 11-18 request `…5cc3` was still open and unacked. | [254], [348] | Before any close request, search open requests to that lane and ring the existing one (lane-rules section 6). |
| M5 | `lane-close` REQUEST was sent to lanes with no window. It could never complete and cost an operator round-trip. | [266-284] | Classify windowless lanes during discovery. For a main, dev/main stands in and self-closes. For a work lane, offer "close-only window" or "orphan retirement" (M11) as one operator decision. |
| M6 | Close-only Claude windows printed `SAFE TO CLOSE THIS WINDOW` but stayed open, using RAM. The operator noticed. | [706], [745] | Tell the operator up front how many windows they will need to close. Track them in the final report until gone, and consider a host-side close path for lane-open Claude launchers (scoped to codex-coord-review). |
| M7 | The helper cannot close windows that lane-open did not start (the operator-launched Codex central). The `--list` output did not show it at all. | [418] | In discovery, identify any coordinator window not launched by lane-open and say early that the operator must close it, or ask for an explicit process-level close. |
| M8 | lane-open verification saw no session within 180 s, possibly because of a folder-trust prompt. The card registered about 7 minutes later. | [362], [469] | Re-check registration at the next phase boundary, not in the same long wait. Tell the operator to look for a trust prompt. |
| M9 | `card set --next` was over 200 characters. A `status --json` pipe failed because the JSON shape was guessed. | [1025], [61] | Validate field lengths before calling. Reuse lane-status's documented JSON fields. |
| M10 | `dev/main` was claimed while the outgoing Codex window still believed it held `dev/main`. The outgoing window never posted a `dev/main` handover, because a same-id same-project handover cannot be acked. | [100] | Hand over dev/main explicitly. The outgoing window posts a dev/main transfer through a project-main packet (cross-project, ackable) or a transient outgoing id, then goes done. Only then does the incoming window claim. For Claude to Claude the claim fails anyway (`stays bound to live session`). |
| M11 | dev/main wrote a work lane's card (`dev-observatory/dev-main`) and posted that lane's handover in its name, then acked it as main. The same pattern was used for practice-plumber/main (self-ack). Lane-rules section 6 says only a lane writes its own card, and lane-handoff says not to post a handover in an old lane's name. | [875-900], [497-502] | Define an explicit "orphaned-lane retirement" step. It requires operator authorization and proof that the window is gone (process census plus a stale binding), labels the card as a stand-in retirement, and reports that the handover was written by the coordinator. |
| M12 | Backlog triage was not part of the plan. The Stop hook showed 5 old notes per turn for about 30 minutes, about 8 extra turns. | [521-933] | Sweep backlogs right after T5.1: count unacked per main, then triage and ack before other work. |
| M13 | The bulk ack of 285 notes was taken from a "yes" that answered a different question (closing Codex processes). The 26 passing-notes notes were acked after one body read. | [855], [902], [962] | Ask one explicit question: "ack N settled notes?". Bulk ack only items the triage marked settled. |
| M14 | `codex app-server daemon stop` and the kills leave no transport for the planned Codex image-generation lanes. | [860] | Record "daemon stopped; run `codex app-server daemon start` and verify before any Codex dispatch" on the dev/main card (codex-lane-liveness). |
| M15 | Coordinator turns held bounded waits (180-240 s) on other lanes' cards. That conflicts with the spirit of "dispatch, then end the turn". | [369], [469], [486], [656] | Keep the waits that lane-open and lane-handoff require. Otherwise end the turn and let doorbells and the Stop hook bring the results. |

## 4. Gaps (late, partial or not done)

| Gap | Why it matters |
|---|---|
| No `status --mine --live --needs-attention` / `--coordinator-attention` run at the start. Attention items surfaced piecemeal. | Lane-rules section 3 asks for this at session start. It would have shown the 290-note backlog, stale cards and open operator decisions at once. |
| No single list of pending operator decisions at handover. They arrived across 4 packets, addenda and the triage report. | The operator must decide from one complete list, and a missing decision silently stalls a lane. |
| The outgoing coordinator's task-state, checkpoints and progress pages were not read, only Passing Notes packets. | The workspace progress standard and task-state hold state the notes may not. |
| The unshipped-work sweep came last, found only via Stop-hook backlog notes. No cross-project sweep of `worktree list`, branches not on main, or dirty worktrees. | This was exactly the operator's complaint: finished work sat unlanded for days because no shipper was assigned. |
| No shipper assigned for `b812a49`, `5a79e6f` or review-pin. It was left as a decision. | Dev main must assign a dedicated shipper wherever it would otherwise be the default shipper. Parked work needs an owner or a queued dispatch with a reason. |
| The process and RAM census came late, after the operator's prompt. | The operator had asked the previous coordinator for this "many times". It belongs in discovery. |
| No transport-readiness check before dispatching to Codex (`codex app-server daemon version`), and no record after stopping it. | Codex doorbells need the daemon, and the planned image-generation lanes need it restarted. |
| Lane docs were not updated for new lanes (`*-opus`, `codex-coord-review`) or the relay's host change. | Lane-rules section 1 says the coordinator keeps the lane list. Stale docs mislead the next coordinator. |
| No handover-receipt check on all successors. `gen-request-opus`'s startup inbox was "not confirmed" until its own note arrived. The late-handover ring (lane-handoff step 5) was not shown being run. | A handover that arrives during registration can sit unread. |
| The incoming coordinator wrote no checkpoint or handoff for itself (task-state, session-wrap). | A second handover, or a context reset, would start from scratch again. |
| 2 close-only Claude windows were still open at the end. The parked worktrees (practice-plumber 2, Core10 1) have no register beyond handover notes. | They use RAM, and uncommitted work without an owner is the "untracked work dies" failure. |
| Old done-lane bindings (`cc-codex`, `core-companion`, `central`, …) remain in `wake show`. | Harmless by rule ("never unbind"), but the skill should report them so they are not misread as live windows. |
| The same-host (Claude to Claude) path was never exercised. The binding refusal (`stays bound to live session`) changes the claim order. | The skill must sequence the outgoing done before the incoming claim. |
| The store diagnostic (an invalid note subject in `dev`) was ignored. | It is minor, but a handover is a natural time to report store health once. |

## 5. Proposed checklist for `coordinator-handover`

Flags: `--from claude|codex --to claude|codex`. Steps marked **[out]** run in the outgoing window, **[in]** in the incoming one. When the operator runs the skill only in the incoming window, the [out] steps become one operator-relay note plus a request that the operator type "check your inbox" in the outgoing window.

1. **[in] Intake.** Record the operator's words verbatim. Read the operator queue between every later phase.
2. **[in] Discover (read-only).**
   - `card show` for every project, and `status --all-projects --live --coordinator-attention`.
   - `wake show` per project, to map which window holds which identities.
   - Classify each lane: bound and idle, bound and busy, windowless, or orphaned (window gone).
   - Find the outgoing window's launch type: lane-open launcher or operator-launched.
   - Count backlog unacked per main.
   - Process census (`codex.exe`, `claude` windows, daemon) and free RAM.
   - Codex transport: `codex app-server daemon version`.
3. **[in] Unshipped-work sweep.** For each project: `git worktree list`, branches not on main (`merge-base --is-ancestor`), dirty worktrees, and SELF-close "uncommitted" claims. Output a parked-work table.
4. **[in to out] Request the handover.** Send one operator-relay per outgoing identity with `--from-project` always set. Ask for one packet per project main, including dev/main, with the fields of T4.1. Ask the outgoing window to relay the operator's words to its own live lanes first.
   - **codex out:** the doorbell queues ("active writer"). Do not re-send. Ask the operator to nudge that window. Expect a delay of one turn end.
   - **claude out:** the doorbell is delivered when idle. The Stop hook covers a busy window.
5. **[out] Produce the packets.**
   - One handover per `<p>/main` to dev/main (cross-project, so ackable).
   - For dev/main itself: a packet inside the dev-observatory or other project packet, or a transient id. Never a same-id note.
   - Wait for the incoming window's acceptance, then `card set --state done` on each.
   - **codex out:** `card set` needs `--window $env:CODEX_THREAD_ID`, and `wake send` needs escalation outside the sandbox.
6. **[in] Accept.**
   - `read` and `ack` each packet.
   - Reply with `--ref` (not a cross-store `--reply-to`), saying "you may mark `<p>/main` done".
   - Build the merged operator-decision list.
7. **[in] Claim after release.**
   - When each outgoing card is `done`, run `card set` for `dev/main` and each `<p>/main` stand-in ("coordination-only"), then `inbox`.
   - **claude in:** auto-bind. Same-host Claude to Claude fails until the outgoing card is done; then `wake bind --session $CLAUDE_CODE_SESSION_ID`.
   - **codex in:** `card set --window $env:CODEX_THREAD_ID` plus `wake bind --host codex --session $env:CODEX_THREAD_ID`. Store access needs `--add-dir "$env:LOCALAPPDATA\passing-notes"`.
8. **[in] Backlog.**
   - Small: read and ack.
   - Large: a read-only triage agent writes a report outside git. Ask the operator one explicit bulk-ack question. Ack only settled items.
   - Put residual items on the card `--next` (200 characters at most).
9. **[in] Lane hygiene, with one operator decision covering the whole table.**
   - Idle with a window: REQUEST close through the project main, after checking for open requests (M4).
   - Windowless main: stand-in self-close.
   - Windowless work lane: a close-only window (state the RAM cost and the manual-close count), or orphan retirement with window-gone proof (M11).
   - Host replacement: lane-handoff with a new successor id, the same checkout, and transfer confirmation plus first assignment. For **codex to claude** successors, use Claude Opus by default (operator memory: Codex for image generation only).
10. **[in] Window and process cleanup.**
    - Done-window helper `--list` and exact `--close` for lane-open Codex launchers only.
    - An operator-approved process stop for operator-launched or support processes.
    - Record the daemon state on the dev/main card.
    - List every remaining window the operator must close: all Claude windows, and any non-lane-open Codex window.
11. **[in] Dispatch.**
    - Assign a shipper lane for each parked candidate, or queue it with the reason (RAM).
    - Route each live lane's next item with paths, proof, forbidden actions and the handoff destination. Then end the turn.
12. **[in] Records.**
    - Update project lane docs (new ids, hosts, relay) through the project's shipper, or note the change for it.
    - Write the incoming coordinator's own checkpoint (task-state).
    - Save memories for new lessons.
13. **[in] Final report.**
    - Identities held.
    - Lanes closed, replaced and running.
    - Parked work.
    - Merged operator decisions, verbatim.
    - Windows the operator must close.
    - Transport and RAM state.
    - Report posted, queued, delivered, acked and done separately.

**Case differences in brief**

- **codex to claude** (this session): expect queued doorbells and a delayed outgoing window, and an operator-launched Codex window the helper cannot close. Successor lanes open as Claude.
- **claude to codex:**
  - Verify the daemon before the first dispatch.
  - The incoming Codex window cannot take doorbells while busy, so the outgoing Claude window must stay live until every acceptance arrives.
  - The outgoing Claude window must be closed by the operator after its cards are done.
  - Codex `card set` always carries `--window`.
- **same-host:**
  - Claude to Claude: the binding refuses a live window, so the outgoing done must come strictly before the incoming claim. Both windows can be rung while idle.
  - Codex to Codex: the incoming window must be a new thread. Old bindings cannot be reused (lane-rules section 2). Use the helper only if the outgoing window was lane-open-launched.
