# NOTE: This is the canonical provider-independent contract. Every provider wrapper (Claude, Codex, GPT) loads it in full.

## Provider-neutral host abstractions

- Named-skill calls: `lane-close` (REQUEST mode, or SELF mode when the invoking window is itself an old lane) and `lane-open`. Load and follow each one's core where this contract calls it. Never re-implement them.
- External commands used: `passing-notes` (card, inbox, post, wake). Window launching happens only inside `lane-open`, through its support script.
- This skill spawns no sub-agent and uses no model tier. The model and effort named in the request are passed to `lane-open` for the new window.
- Never expose hidden chain-of-thought. Return only the report this contract defines.

# Lane Handoff: replace one or more lanes with a new lane

Hands the work of one or more existing lanes to one new lane in its own window, optionally with added scope. Some examples:

- "Close the parser lane and open a new one in a Claude Code terminal that also handles error recovery."
- Consolidation: "merge the docs and api lanes into one api-docs lane".
- Replacement: "restart the main lane on Codex".

Each old lane closes itself through `lane-close`, handing its state to the new lane id. The new lane opens through `lane-open`, with a brief that combines the old scopes and the added scope. The coordinator gets one note proposing the change. Ownership moves only under the project's lane rules, and the operator's word decides.

## Invocation

```text
/lane-handoff <old-lane>[,<old-lane>...] [--to <new-lane>] [--project <id>] [--host claude|codex] [--model <name>] [--effort <level>] [--title <window title>] [--wait <seconds>] -- <added scope>
```

- Prose requests map onto these fields.
- **"Via Claude Code terminal"** means `--host claude`. `lane-open` always opens a visible terminal window.
- **`--to`**: the new lane id. It must differ from every old lane's id: a lane cannot acknowledge a note it sent itself, so a handover from `parser` to `parser` could never be acknowledged (found in the 2026-11-13 out-of-sample run). Derive a short id from the combined scope (for example `parser-recovery`), and state the derivation.
- **`--project`**: resolved as in `lane-open`.
- **`--wait`**: how long to wait for the old lanes' handovers before opening the new lane. Default 180 seconds, at most 600. The new lane opens either way.
- **`<added scope>`**: carried verbatim. It may be empty.

## Procedure

1. **Read the old lanes.** Run `passing-notes card show --project <P> --lane <old>` for each. Record its host, item, owned paths, holds and state. Only a `done` card means the lane needs no close; its last handover note, if any, is still passed on. A **missing card does not mean closed**: lanes that predate cards have none (found in a 2026-11-13 run on real lanes). Such a lane still gets a close request when the operator named it or the project's lane rules list it. Stop with `Result: NOT HANDED OFF (<reason>)` if the new lane id is invalid, equals an old lane's id, or names a lane that has an active card and is not one of the old lanes.
2. **Request the closes.** For each old lane that is not `done`, run `lane-close` in REQUEST mode with `--successor <new-lane>` and `--wait 0`. Quote the operator's words in the request. If the invoking window is itself one of the old lanes, it runs `lane-close` in SELF mode instead. It closes last, after step 5, so it can finish this procedure first.
3. **Wait, bounded.** For up to `--wait`, check every 15 seconds for each old lane's card turning `done` and its handover note to `<new-lane>`. Record what arrived. Stop early once all have.
4. **Open the new lane** with `lane-open <new-lane>`, using the host, model, effort and title. Its brief is, in order:
   - **Takes over:** each old lane with its last item and owned paths, as read from its card in step 1;
   - **Added scope:** verbatim;
   - **First job:** read each handover note addressed to you. Give their ids if they have arrived. Otherwise say they arrive later in the inbox, with a doorbell once this lane is bound. Then propose a plan for the combined scope;
   - **Guard:** do not edit an old lane's owned paths until its handover has arrived and the coordinator has confirmed the ownership transfer under the project's lane rules.

   Seed prompts are drafted only if the request asks for them, as in `lane-open`. Let `lane-open` post its own coordinator note, with this subject override: `Lane handoff proposed: <old lanes> -> <new-lane>`. Its body lists, per old lane: its close state, its handover note id or "pending", and the owned paths whose transfer is requested.
5. **Ring for late handovers.** A handover that arrives after the new lane's startup inbox check can sit unread: the old lane's own doorbell finds the new lane busy registering (the 2026-11-13 run). So after `lane-open` reports the new lane bound, keep watching for whatever remains of `--wait`, but for at least 120 seconds, every 15 seconds. For each handover addressed to `<new-lane>` that `<new-lane>` has not acknowledged (no `acks\<project>\<handover id>--<new-lane>.md`), run `passing-notes wake send --project <P> --lane <new-lane> <handover id>`. A busy refusal is retried at the next check; `wake send` never rings twice for the same note and session. Handovers that arrive after this watch still reach the new lane at its inbox checks; the report names them as pending.

## Report

```text
Result: HANDED OFF | PARTLY HANDED OFF (<what is pending>) | NOT HANDED OFF (<reason>)
Old lanes:
  <old>: close <requested | already done | self> · wake <delivered | not delivered (<code>)> · card <done | not done | no card> · handover <note id | pending> · successor ack <read at <utc> | not yet>
New lane: <new-lane> · <the lane-open report lines: window, session, card, wake>
Coordinator note: <note id> to <coordinator>   (or: skipped (<reason>))
Next: <confirm the close in an old lane's window | accept the trust prompt in the new window | coordinator to confirm ownership>
```

`HANDED OFF` requires every old lane `done` with a handover note seen, and the new lane's window launched. Anything less is `PARTLY HANDED OFF`, naming what is pending.

## What NOT to do

- Do not write an old lane's card, post a handover in its name, or close its window.
- Do not open the new lane twice. A failed `lane-open` is reported, not retried.
- Do not let the new lane edit an old lane's paths before its handover and the coordinator's confirmation.
- Do not move ownership in the project's lane registry yourself unless the project's rules make the invoking lane its owner.
