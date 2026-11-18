# Seed: lane-open sets up Codex lanes so they never need a native approval for git

**Status:** queued 2026-11-18 (operator approved the practice the same day; it runs by hand until this lands). No lane yet.

## Problem

Codex's `workspace-write` sandbox keeps `.git` read-only. A Codex lane that creates a branch or worktree, commits, stashes or checks out therefore raises a native approval in its own window. The operator has to find the window to approve it, and nothing tells them: the lane's blocker note goes out as `needs fyi`, which does not ring the coordinator. This has recurred across several lanes.

## Change

1. **The opener prepares the checkout.** For a Codex lane, `lane-open` (run by the project's `main`) creates the lane's worktree and branch before launch, inside the repository root so the sandbox can write it (for example `<repo>/.worktrees/<lane>`), and hides that folder through `.git/info/exclude`, not a tracked `.gitignore` edit. This replaces today's rule "Never create one here", for Codex lanes only. The opening prompt names the worktree and branch.
2. **No git writes from Codex lanes.** The opening prompt's Rules line says: read-only git only; leave finished work uncommitted in the worktree; the commit-request names the worktree, files, a suggested commit message and gate evidence; the coordinator commits, merges and pushes, and creates any further branch or worktree.
3. **Pending approvals ring the coordinator.** The Rules line's blocker sentence becomes: post a pending native approval at once with `--kind blocker --needs operator-decision --wake-if-needed`, naming the window title and the exact action.
4. **Optional host notification.** Document Codex's own approval-request notification setting once it has been tested on a real lane (operator asked to test it on the next Codex lane), with what it actually shows on Windows.

## Where

`skills/lane-open/core.md` (Procedure step 1 checkout rule, step 3 Rules line, the report), `skills/lane-open/scripts/open-lane.ps1` only if the worktree setup moves into the script, `docs/lane-rules.md`, and the docs tests that pin the Rules line. Reinstall the lane skills afterwards.

## Done when

A Codex lane opened with `lane-open` completes an item, from first edit to merged commit, with zero native approvals, and a forced pending approval rings the coordinator within one minute.
