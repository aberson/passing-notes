# Seed: Codex lane windows keep their lane name as the window title

**Status:** queued 2026-11-19 (operator: "those window names not being what I expect is sort of killer"; approved the seed the same night). No lane yet.

## Problem

`lane-open` sets each new window's title (for example `say-no-more - art-gen lane`), but Codex then retitles the terminal tab with its own summary of the first prompt plus the folder (`Run art-tune hillclimb | say-no-more`, `Complete art pose variants | say-no-more`, `Follow lane rules | dev`). With six or more Codex windows open, the operator cannot tell which window is which lane. On 2026-11-19 this sent an operator message meant for `art-heroes` to `art-enemies`, and repeatedly needed the coordinator to map titles to lanes by reading Codex's session index. Two dev lanes started in the same folder showed only `| dev`.

## Change

1. **Fixed tab title for Codex lanes.** Launch Codex lanes through Windows Terminal with a locked title: `wt.exe -w new new-tab --title "<project> - <lane>" --suppressApplicationTitle ...` (or the profile-level `suppressApplicationTitle` for a dedicated lane profile), so the application cannot rename the tab. Keep the process tracking `open-lane.ps1` relies on (record the launched process tree, not just `wt.exe`).
2. **Fallback when Windows Terminal is absent:** keep today's behaviour, and record the Codex thread name in the lane's card so `passing-notes status` can show the title the operator actually sees.
3. **Status shows the title.** `passing-notes status` (text and JSON) gains the visible window title per lane, from the locked title or, for Codex, the session index's `thread_name` for the lane's thread.

## Where

`skills/lane-open/scripts/open-lane.ps1` (launch line, process tracking), `skills/lane-open/core.md` (report names the title), `docs/cli.md` and the status renderer, plus a test that a launched lane's recorded title equals `<project> - <lane>`.

## Related evidence the same night

- Codex's own approval notification (`[tui] notifications = ["approval-requested", "agent-turn-complete"]` in `~/.codex/config.toml`) was tested on the next Codex lanes as planned (seed `seed_lane_open_codex_no_git_writes.md` item 5): the operator saw **no notification**. Record it as not working on this Windows host; the hand-off rule (item 4 of that seed) is the working route.

## Done when

Six Codex lanes opened with `lane-open` show `<project> - <lane>` as their tab titles for their whole life, and `passing-notes status` prints the same title per lane.
