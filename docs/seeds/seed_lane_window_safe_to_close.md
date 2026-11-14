# Seed: a closed lane's window should say it is safe to close, and so should a superseded one

Captured: 2026-11-14, from the you-asked-for-it project (central coordinator, a Claude Code window). Status: seed only. It changes no lane rule and grants no implementation.

## What happened

At the end of a long session the operator asked central to close every lane (`/lane-close --all`). Passing Notes worked as designed:
- three Codex lanes (combat-abilities, enemy-ai, character-generation) each ran SELF mode, wrote a handover and marked their card `done` within four minutes (18:36-18:37Z);
- central closed last and reported `Result: ALL CLOSED`.

The operator then looked at the screen and saw nine Codex terminal tabs, eight of them belonging to Passing Notes lanes. They said:

> none of them seem to be working. on closing.

> I wouldn't feel comfortable going and closing all of them since not all clearly say close this window. I thought this is part of passing-notes.

Two separate gaps:
1. **A closed lane's window doesn't say it is finished.** Lane-close ends at "card done, handover sent" and, by its own rule, never closes the window ("The operator closes the window"). The last screen of each tab was a mix of `Result: CLOSED`, `Worked ...`, an `Exited (status 1) passing-notes wake send ... wake-session-busy` line, or an idle `Ask Codex` prompt. The operator couldn't tell finished tabs from stuck ones.
2. **Superseded windows pile up.** Only 3 of the 8 tabs were the lanes' current windows (the ones their wake bindings point to). The other 5 were older windows the same lanes left behind when they reopened or retried: a `-v2` retry, two earlier restarts, the research lane closed hours before, and a passing-notes lane. When a lane reopens, `wake bind` replaces the binding, so the old window can no longer be rung. Nothing tells the operator it is now dead weight. Tab titles ("Retry character generation", "Register lane card | yat-eai") don't map to lane ids either, so the coordinator could only match tabs to lanes by process start times.

A note can't fix either after the fact: `wake send` refuses a `done` card ("a closed lane is not rung"), and a superseded window is no longer bound.

## Proposal (for the passing-notes maintainer to shape)

- **Final line on close.** SELF mode's last step prints one fixed, unmistakable line, for example `SAFE TO CLOSE THIS WINDOW - lane <lane> closed, handover <id>`, as the very last output, after any trailing wake or reply errors.
- **Superseded-window notice.** When `wake bind` replaces a binding, queue one message to the previous host session, where the host allows it: `This window was replaced by a newer window for lane <lane>. SAFE TO CLOSE THIS WINDOW.`
- **Window title carries the lane id.** `open-lane.ps1` already sets a title. Make sure Codex tabs keep `<project> - <lane>` so tabs map to lanes.
- **An operator view.** Something like `passing-notes windows --project <P>` that lists each known window or session with its lane, card state, whether it is the current binding, and `safe to close` / `active`.

## Acceptance hints

- After `/lane-close --all`, every lane tab's last visible line says whether it is safe to close.
- After a lane reopens in a new window, the old window shows the superseded notice, or the operator view lists it as safe to close.
- The operator can close every finished tab without asking the coordinator.

## Evidence

- you-asked-for-it notes 20261114T183624Z-combat-abilities-to-central-1755, 20261114T183724Z-character-generation-to-central-a21c, 20261114T183736Z-enemy-ai-to-central-944d (handovers), 20261114T184030Z-central-to-operator-18e2 (central's handover).
- Wake bindings at close: combat-abilities 01a1d848..., enemy-ai 01a1d87d..., character-generation 01a1da6e..., research 01a1d842.... Nine `codex` processes were running, started between 23:38Z on 2026-11-13 and 14:21Z on 2026-11-14.
- Operator screenshot, central's session (not committed).
