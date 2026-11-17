# Parked lane: lane-status

Parked 2026-11-17 at central's request, to free memory. Successor: `central`, which reopens
this lane with `/lane-open` under the same id `lane-status` once the blocker below clears.
This file is the whole resume context; it copies no note bodies.

## What the lane builds

A read-only `passing-notes status (--project P | --all-projects) [--stale-after H] [--json]`
command and a thin `lane-status` skill beside lane-open, lane-close and lane-handoff. It gives
the operator one table of every lane; user-pm and a later Observatory board consume `--json`.
Read-only means: never acks, never writes cards, never rings, never creates folders, stdout
only, never shows a note body or subject.

Research behind it: `<dev>\docs\research\lane-status-skill-2026-11-16.html`.

## Done

- Branch `lane-status`, worktree `<dev>\worktree_lane-status`, two plan-only
  commits on top of main `ec2c667`, pushed to `origin/lane-status` as a backup:
  - `2cf09aa` docs: plan - lane status phase (Steps 16-18), reviewed; proposal publication 5
  - `34cdc0d` docs: plan - lane status phase redline fold (P34, H1-H4 resolved), wrap-ready
- Plan: `docs/plans/master_plan.md`, section "## Lane status phase — planned 2026-11-16"
  (before "## Reuse and boundaries"); Steps 16, 17, 18; inventory rows P31-P34, D47-D57.
- Pipeline so far: plan-feature, plan-review --autofix, plan-redline (proposal publication 6 at
  `docs/plan-redline/passing-notes-proposal.html`), plan-wrap: READY (same-session check, not
  independent; three minor notes: the terms Heads Up, Observatory and R25 are not defined).
- No code written. No issues created yet (repo-sync not run).

## Operator decisions already given

- 2026-11-16: an operator-run, read-only `status` may read other projects (P31).
- Visibility (P32): dev's `main` sees all lanes; a project's `main` sees only its own project.
  Enforcement as topology recommended and the operator approved (Q1-Q5): the literal lane id
  `main`; a window bound in several projects sees their union; an unbound caller sees every
  project only with an explicit `--all-projects`; the check is soft; no special case for
  `central`.
- Redline answers: D47 approved (aligned ASCII table, columns
  `PROJECT LANE STATE HOST WINDOW UPDATED OPEN WORKING ON`); D49 changed to P34 (done lanes are
  always listed, at the bottom of each project block, no `--include-done`); D50 approved
  (`stale` after 24 hours); every other default stands as written.
- 2026-11-17, through central: the table shape is approved, "let's build it".
- Note-body wording (P31, settled with topology): the view "never shows a note body (never
  prints, returns or keeps one) and writes nothing".

## Pending, and what it waits on

1. **Blocker:** lane `claude-messaging-fix` must land Steps 13-14 on main. Step 16 reuses Step
   13's `hook.session_bindings(store, session_id)` and its liveness check `hook._binding_live`
   (it was asked whether it will make that public; no answer yet; if not, wrap it locally).
   Do not touch main while their build-phase runs.
2. Topology commits `docs/protocol.md#topology` (its item 2, after Step 14). Step 16 code may
   be built before it; the docs citation of `docs/protocol.md#topology` lands with it.
3. Numbering: topology takes P35-P39 (its Q1-Q5) and D58-D60 and adds no steps. New numbers
   here start at P40 and D61. Whichever branch merges second rebases the decision inventory;
   once both are on main, P32 may point at P35-P39.

## Exact next commands (after Steps 13-14 are on main)

Run in: fresh window @ `<dev>\worktree_lane-status` · Model: Opus (default)

```bash
passing-notes wake bind --project passing-notes --lane lane-status --host claude --session "$CLAUDE_CODE_SESSION_ID"
passing-notes card set --project passing-notes --lane lane-status --host claude --item "lane-status: rebase, repo-sync, build Step 16" --state active
passing-notes inbox --project passing-notes --lane lane-status
git -C <dev>/worktree_lane-status fetch origin
git -C <dev>/worktree_lane-status rebase main
```

Then, in order, each item starting with `/user-build-route`:

1. `/repo-sync` for Steps 16-18 (creates their issues).
2. Build Step 16 with `/build-step` (`--reviewers deep`): `src/passing_notes/status.py` (new),
   the `status` subparser in `cli.py`, `tests/test_status.py`, `tests/test_docs.py` pins,
   `docs/cli.md`, `README.md`. Gate in the worktree: `uv sync`, `uv run pytest -q`,
   `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`. Hand off to central.
3. Build Step 17: `skills/lane-status/{core.md, providers/claude.md, providers/codex.md,
   providers/gpt.md, skill-mesh-entry.json}`, the `SKILLS` entry in `tools/skills_install.py`,
   `tests/test_skills_install.py`, `README.md`. The skill only runs `status --json`. Hand off.
4. Step 18 is the operator's live check from real windows (reinstall skills for both hosts,
   restart, run from dev main and from a project main). Hand off.

Every finished item gets its own handoff note to central. Operator decisions: ask in the
window and post to central with `--needs operator-decision`, then ring it.

## Files this lane owns

- `docs/plans/master_plan.md`: only the Lane status phase section and rows P31-P34, D47-D57.
- `docs/plan-redline/passing-notes-proposal.html` (publication 6).
- To be created: `src/passing_notes/status.py`, `tests/test_status.py`, `skills/lane-status/`.
- Shared edits planned: `cli.py` (status subparser only), `docs/cli.md`, `README.md` (two list
  entries), `tests/test_docs.py`, `tools/skills_install.py`, `tests/test_skills_install.py`.
- Not owned: `docs/protocol.md` (topology owns `## Topology`).
