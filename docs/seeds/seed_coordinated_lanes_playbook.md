# Seed: a coordinated-lanes playbook — one coordinator, structured lanes, one gate

Captured: 2026-11-15, from the you-asked-for-it project, where a Claude Code coordinator (lane `central`) ran up to six Codex lanes over about 36 hours. Status: seed only; it grants no implementation. The operator asked for this seed to "build this out in general", including what worked, what didn't, and what to watch for, with evidence or "no evidence".

## The pattern being generalized

- **One coordinator lane.** It owns the queue, reviews, the full gate, merges, pushes and installs, and it relays the operator's words.
- **Work lanes.** Each lane works in its own checkout, owns a slice, builds and tests there, and hands back a commit.
- **Passing Notes** carries every handoff, request and relay. Wake-ups replace the operator pasting prompts between windows.
- **Skill-mesh supplies the build structure** inside each lane: `/user-build-route`, `/build-step`, the Core pipeline and `/deep-research`. The project adopted this mid-session as "How lanes build" (you-asked-for-it `docs/lanes/README.md`, source 171).

Evidence below is cited by Passing Notes note id (project `you-asked-for-it`, private store), commit sha (you-asked-for-it repo) or provenance source number (`docs/provenance/listening-genie/`).

## What worked (evidence)

1. **The coordinator handoff test.** The incoming coordinator verified the outgoing one's facts independently, then posted a test note. The outgoing one replied "handoff accepted" 38 seconds after it was rung. Evidence: notes `…054257Z-claude-central-to-central-da90` and `…054335Z-central-to-claude-central-8b76`; handoff doc commit `a910a3d`; source 160.
2. **Codex wake-ups deliver despite the "could not resume thread" warning.** Acks arrived within a minute when the note was sent with `--wake`. Notes sent without it sat unread. Evidence: da90 (acked 05:50Z) and `…central-to-character-generation-7f0a` (acked 05:50Z), against `…central-to-enemy-ai-a114` and `…central-to-combat-abilities-81bd`, both unacked until rung. The passing-notes lane reached the same finding (note `…064652Z-passing-notes-to-central-b7e7`).
3. **The `operator-said:` first-line rule works as a safety check.** A lane refused to treat an "Overnight activation" relay as the operator's word until it was re-sent with the exact quote. Evidence: `…070829Z-enemy-ai-to-central-f419`, corrected by `…071036Z-central-to-enemy-ai-c7ba`.
4. **`/lane-close --all` closes a fleet fast.** Three lanes ran SELF mode and handed over to the shipper within about 4 minutes. Evidence: requests at 18:33Z on 2026-11-14; cards done at 18:36:47Z, 18:37:30Z and 18:37:40Z; handovers `…183624Z-…-1755`, `…183724Z-…-a21c`, `…183736Z-…-944d`.
5. **One integration branch, one full gate.** Batching lanes' commits into one integration saved a 40-minute gate per lane. Evidence: d8ceb2b (urns, Area 2 scenes and the enemy-ai trial in one gate) and b922bc2 (environment kits plus two lanes' follow-ups); gate summaries `Game/Saved/Housekeeping/20261111/gate-summary-<sha>.txt`.
6. **Central-local review on a frozen, hash-verified snapshot.** When a lane's external-model review was blocked by the host, an independent local review still caught real defects before the gate. Evidence:
   - ABL-01 Step 13: two test-gap Blocks (an assertion that could not fail; a Done-when clause tested off the production path), repaired at 0eb8333;
   - Step 14: an MSVC operator== ambiguity found before the first build (repaired; landed e14386c);
   - a missing engine header in a lane's test, caught before the gate's build (`central-review-mp01-stageA.md`).
7. **Path-scoped staging into dirty shared files.** Central built the index blob from HEAD plus only its own hunk (`git hash-object -w` then `git update-index --cacheinfo`), so other sessions' unsaved hunks in the same file stayed out of every commit. Evidence: commits c0525ea, b501a85, 200c5b3 and 7ae3767 touched `AGENTS.md` and `docs/lanes/README.md` while each held foreign edits.
8. **Lanes with skill structure handed off cleaner work.** combat-abilities used `build-step`, `build-phase` and `review-deep`, and its steps reached central with ledgers and reviewer verdicts. character-generation used the project's `character-generate`. Evidence: Codex session scans (skills opened: combat-abilities build-step x5, build-phase x2, review-deep x6; character-generation character-generate x6). This is limited evidence: there is one comparison, not a defect-rate measurement.

## What didn't work (evidence)

1. **The doorbell text suppressed a finished handoff.** The doorbell says "If you already acknowledged it or your card is done, stop: post nothing". A lane read that as "post nothing at all" and stayed silent about three hours after finishing. Evidence: character-generation finished about 00:32Z on 2026-11-15 (generation.json status `offline_candidate_ready_central_review_pending`). The coordinator then told the operator it was "still working" until the operator pasted the lane's screen.
2. **The coordinator missed handoffs for about seven hours.** No wake-up reached it, and it had no inbox habit. Evidence: `…054002Z-multi-player-to-central-ff7f`, `…054549Z-hardware-to-central-6be0` and `…060600Z-level-design-to-central-97db` were all unread until 13:14Z.
3. **Lanes without skill structure handed off unbuilt code.** Evidence: hardware's guard e7fe28b included `GameFramework/OnlineReplStruct.h`, which doesn't exist; the review FAIL is in `central-review-mp01-stageA.md`. Three lanes (multi-player, hardware, level-design) opened no skill files in their sessions.
4. **Wake refuses some notes.** `--wake` refuses `--needs ack` notes and done cards, and the error appears only after posting, so work requests had to be re-posted with `--supersedes`. Evidence: `…070522Z-central-to-*` refused with `wake-not-needed`, then re-posted as `…070538Z`, `…070710Z` and `…070831Z`; `wake-session-unavailable` on a done card (`…141958Z-central-to-character-generation-4e60`).
5. **Idempotency keys collided across days.** A key reused from an earlier session silently returned the old, already-acknowledged note. Evidence: `lane-close-request-combat-abilities-20261114` returned `…003630Z-…-6af8` instead of posting.
6. **Old lane windows pile up.** Nine terminal tabs were open, five of them superseded; the operator couldn't tell which to close. Evidence: operator screenshot; separate seed `seed_lane_window_safe_to_close.md`.
7. **Lessons are lost in checkouts outside the coding root.** `session-wrap` and `task-handoff` fail in `C:/yat-*` worktrees with "required_tool_missing (schema)", so lanes close without saving lessons. Evidence: handovers `…1755` and `…944d`; skill-mesh seed `seed_task_handoff_external_worktrees.md`.
8. **Ad-hoc engine-lock contention.** Lanes polling one lock lost slots to each other, and the coordinator's play update queued behind a lane build that grabbed the lock 20 seconds earlier. Evidence: combat-abilities lost the lock twice (`…094156Z-combat-abilities-to-central-5152`); `update-yat-play-9d1f47c.txt` started 04:25:28Z while multi-player held the lock from 04:25:08Z.
9. **Two lanes edited the same shared list and both changed its count tests.** Evidence: multi-player (1a01a36) and level-design (e6f10b8) conflicted on `Config/dev-portal-tests.json` and `YatDevPortalTests.cpp`.

## What to watch out for

- **External model export.** A lane that sends source to an outside model is blocked by host auto-review, which is correct. The coordinator must not approve it for the operator; it routes to a local review instead. Evidence: `…102118Z-combat-abilities-to-central-5906` (source 163, item 5).
- **Counting budgets.** Count image calls by images generated, not launches; a launch that fails before generating is not a call. Evidence: source 164, where launch 1 failed with no image.
- **Third-party references.** A reference screenshot from a commercial game is structural guidance only: never committed, never an image-generation input. Evidence: source 170 and the operator's confirmation.
- **Dirty shared checkouts.** Never `git add -A`, stash or build in a main checkout that holds other sessions' edits. Evidence: AGENTS.md rule; about 136 foreign entries all session.
- **Double review cost.** Lane reviewers plus a full coordinator review doubles agent passes. The adopted rule: the coordinator reviews only the integration when the lane's own reviewers ran. Evidence: source 171. No evidence yet on how much this saves.
- **Status from silence.** Check card, evidence files and branch before reporting progress. Evidence: item 1 under "What didn't work".
- **Lane scale.** No evidence beyond six concurrent lanes on one machine with one engine lock.
- **Cross-project lanes.** No evidence; every lane here was in one project.

## Proposal (for the passing-notes maintainer to shape)

1. **Coordinator inbox discipline.** A `passing-notes inbox --summary` mode (count plus oldest age) a host hook can run at the start of every coordinator turn, plus a `--stale-after` flag that flags `needs: reply` notes older than N minutes.
2. **Doorbell wording.** Change the doorbell to say that acknowledging the doorbell is not the deliverable, and that a finished item always posts its own handoff. Add the same line to lane-rules and lane-open briefs.
3. **Wake rules.** Validate `--wake` eligibility before storing the note (or warn first). Allow ringing for `handoff` and `commit-request` kinds. Document a unique-suffix convention for idempotency keys (date plus time, or the target sha).
4. **A shared-resource queue.** `passing-notes reserve <resource> --lane <lane> [--after <lane>]` with FIFO order and release notices, for engines, play checkouts and memory, replacing polling races.
5. **A structured handoff.** Optional fields on `post --kind handoff`: `--sha`, `--reviewers <verdict file>`, `--needs-gate`, `--install-inputs`. A coordinator board command then lists "ready for gate" items.
6. **A lane-open brief template.** It ships with "start each item with `/user-build-route`; follow How lanes build; compile before hand-off; every finished item gets its own handoff" (you-asked-for-it source 171).
7. **Shared-file ownership hints.** Lanes declare files they will touch, such as a dev-portal test list, in their card's `owns` or `holds`. `card show` flags overlaps before two lanes edit the same list.
8. **Window lifecycle:** see `seed_lane_window_safe_to_close.md`.

## Acceptance hints

- A coordinator window shows unread `needs: reply` notes at every turn without being rung.
- A lane that finishes after acknowledging a doorbell still posts its handoff.
- Two lanes wanting the engine get it in request order, and each sees its place.
- A handoff note lets the coordinator start a gate without opening the lane's window.
- A lane opened with the template uses `/user-build-route` on its first item; check its session for the skill read.
