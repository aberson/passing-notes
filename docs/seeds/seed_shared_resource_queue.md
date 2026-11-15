# Seed: a shared-resource queue so a released lock wakes the next lane

Captured: 2026-11-15, from the you-asked-for-it project, where a Claude Code coordinator (lane `central`) and six Codex lanes share one Unreal engine lock. Status: seed only; it grants no implementation. The operator asked for a durable write-up of the stall below and of how Passing Notes can prevent it from now on.

Evidence is cited by Passing Notes note id (project `you-asked-for-it`, private store) or commit sha. This seed is item 4 ("A shared-resource queue") of `seed_coordinated_lanes_playbook.md`, which was not built in the 2026-11-15 round (`c030f2c`, `1f0a919`, `8217790` built items 1 and 3).

## What happened

From 21:32Z to 22:50Z on 2026-11-15, four of the six lanes (hardware, audio, combat-abilities and enemy-ai) did no work. The engine lock was free for most of that time. The operator came back to find "I don't see windows working."

Sequence:
1. Central landed a gated integration. It posted the next engine order as **one fyi note to `all`**: "The engine lock is FREE (21:31Z): hardware first, then audio, combat-abilities, enemy-ai, level-design." Evidence: `20261115T213222Z-central-to-all-6baa`.
2. Hardware had already said it would compile "in my next lock turn". It was idle, waiting to be told. Evidence: `20261115T205348Z-hardware-to-central-8a3d`.
3. Every other lane was correctly waiting its turn behind hardware. Evidence: audio `20261115T203817Z-audio-to-hardware-e4a0` ("I will wait for its matching release"), combat-abilities `20261115T194547Z-combat-abilities-to-all-fa0d` ("I will wait for slot 4"), enemy-ai `20261115T202205Z-enemy-ai-to-all-efb5`.
4. Central ended its turn with no background watch, so nobody saw the queue stop.
5. Only level-design moved. It applied the project's 10-minute skip rule, took the lock at 22:12Z and finished. Evidence: `20261115T221248Z-level-design-to-all-fd60`, then handoff `20261115T223237Z-level-design-to-central-1b36`.
6. Recovery: at 22:51Z central re-sent each lane's turn as a `--needs reply --wake` note (`20261115T225150Z-central-to-hardware-3e9b` and three siblings). Audio and enemy-ai answered within about 30 seconds (`20261115T225221Z-audio-to-central-94b0`, `20261115T225223Z-enemy-ai-to-central-c555`), and hardware took the lock at 22:52:33Z.

A second, smaller instance happened 8 minutes later. Central changed hardware's release target with an fyi note (`20261115T225928Z-central-to-hardware-3259`), but hardware released to the previous target (`20261115T230301Z-hardware-to-audio-1a35`). An fyi reroute is not reliably read before the release it is meant to change.

## Why the existing features did not prevent it

- **Wake-ups need a single named lane and an unblocking note.** `ring_reason` (`src/passing_notes/wake.py`) rings for `needs: reply|operator-decision`, a handover, or an answer to one. A "your turn" fyi to `all` qualifies on neither count.
  - `--wake` would have refused that note, which might have prompted the sender, but the sender didn't use `--wake`.
  - `--wake-if-needed` would have posted it and printed "not rung".
- **`inbox --stale-after` had nothing to flag.** It flags unanswered reply-needed notes. The lanes weren't waiting on an answer; they were waiting for a turn, which no note represents. It also runs only when someone runs it.
- **Queue order lived only in prose.** The engine order existed only in note bodies, so nothing could notice "free resource, waiting lanes, no holder."
- **The lock is project-side.** The engine lock is a directory with `owner.json` (owner, pid, checkout, start and deadline); Passing Notes can't see it. The master plan records this as deliberate: row R1 keeps "engine lock" as a project rule, "not adopted".

## Proposal

Make the turn itself a Passing Notes object, so releasing a resource is what wakes the next lane. No one has to remember to write a note.

1. **Generic named resources, FIFO per project.**
   - `passing-notes reserve <resource> --project P --lane L [--reason TEXT]` joins the queue. It prints your position and the current holder.
   - `passing-notes queue show [<resource>] --project P` lists the holder, start time and deadline, then the waiters in order.
   - `passing-notes release <resource> --project P --lane L` ends your turn.
   - `passing-notes reserve ... --cancel` leaves the queue.
   - Resources are plain names (`engine`, `play-checkout`, `gpu`). Passing Notes owns order and notification, never the lock itself.
2. **Release rings the next waiter.** On `release`, Passing Notes promotes the next waiter to holder and posts it a note (`kind: reservation`, `needs: reply`) naming the resource. It then rings that lane through the existing wake path, so it's eligible under `ring_reason` as it stands. A Claude coordinator bound with `wake bind` gets the same ring.
3. **Coordinator reorders in the queue, not in a note.**
   - `passing-notes queue move <resource> --lane L --to N` (or `--after <lane>`) changes the order. Only the project's coordinator lane may use it, named in project config or by a flag.
   - Then the next release follows the current order. This removes the stale-fyi reroute problem.
4. **Holder deadlines and a stall alarm.**
   - A turn carries a deadline (`--minutes`, with a per-resource default).
   - A read-only check, `queue check` (hook- or loop-friendly, like `inbox --summary`), reports two conditions:
     - **past deadline:** the holder overran its deadline;
     - **stalled:** the resource has been free with waiters for more than N minutes.
   - For a stall, `queue check --ring` rings the first waiter and the coordinator, once per stall (idempotent).
5. **Refuse or fan out wakes to `all`.** `post --to all --wake` is refused today without a clear reason. Make the error say "wake needs one lane; use the resource queue or address the lane." Alternatively, `--wake-each-active` could ring every active card's lane.
6. **Project lock stays project-side.** A project's own lock (here, the engine-lock directory with `owner.json`) is still taken only by the holder of the Passing Notes turn. The project's lock script can call `reserve`/`release`, or simply check `queue show` first. Passing Notes never reads or deletes project lock files.

### Acceptance (synthetic, on the existing test style)

- **Ring on release:** three lanes reserve `engine`, and the holder releases. The second lane becomes holder and gets one ring. The third gets none until the second releases.
- **Reorder:** after `queue move`, the next release rings the moved lane, not the earlier order.
- **Stall alarm:**
  - With a free resource and a waiter idle past N minutes, `queue check` reports `stalled` and `--ring` rings once.
  - A second run inside the same stall rings nothing.
- **Deadline:** a holder past its deadline is reported, not evicted. Eviction stays a human or coordinator call.
- **Closed lanes:** a done card in the queue is skipped, with a diagnostic, and never woken (as `ring_reason` already guards).
- **Privacy:** the queue lives only in `%LOCALAPPDATA%\passing-notes`, like cards.

### Watch out for

- **Don't take over project locks.** R1's reason still holds for the lock's mechanics. This seed reverses R1 only for ordering and notification.
- **Double holders.** If a lane takes the project lock without a turn, `queue show` and the project lock disagree. Report the mismatch in `queue check`; don't fix it automatically.
- **Wake volume.** Ring only the new holder on release. Waiters further back need no ring until it's their turn (the 2026-11-14 echo lesson in `ring_reason`).
- **No evidence yet** for more than one shared resource, or for more than six lanes on one queue.

## Interim discipline (until this ships)

The you-asked-for-it coordinator now does the following:
- sends every "your turn" or reroute as `--needs reply --wake` to one named lane;
- tells each lane to ring the next one on release;
- keeps one background inbox watch while lanes are active.

These are habits, and the 21:32Z stall shows what happens when one is missed.
