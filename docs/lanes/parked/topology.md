# Parked lane: topology (passing-notes)

Parked 2026-11-17 at the operator's request ("yes, park all four", relayed by central in
close request `20261117T014312Z-central-to-topology-e081`). Successor while parked:
`central`. Reopen with `lane-open topology --project passing-notes` (same lane id) when the
blocker below clears. This file is not committed; central owns `docs/lanes/`.

## The work

The operator's words (dev - research bullet, 2026-11-16, relayed by central): "a 'dev - main'
should be able see all lanes and communicate with other project main lanes. Project specific
'* - main' lanes should only be able to see and communicate with it's own project lanes."
They also asked for a workflow diagram on the passing-notes README.

Operator decision, 2026-11-16 (note `20261116T231733Z-central-to-topology-17ad`):
"operator-said: topology lane - approve all recommendations". That approves Q1-Q5, recorded
as plan rows P35-P39:
- P35: a project's `main` may post up to `dev/main`, and nowhere else outside its project.
- P36: the operator's window uses lane id `main` everywhere; `central` retires as an id.
- P37: `dev/main` never posts to another project's work lane.
- P38: the CLI refuses any other cross-project `post` (`cross-project-not-allowed`, exit 2).
- P39: `central` becomes `main` only at the operator's next coordinating-window restart,
  through `/lane-handoff`; lanes opened under central keep it as relay until they close.

Central's go-ahead for item 2: `20261117T011440Z-central-to-topology-137a`
(coordinator-decision, within the approval above).

## Done

| Item | State | Where |
|---|---|---|
| 1. Design proposal | Approved | `docs/research/topology-proposal.html` on branch `topology` |
| 2. Rules, CLI check, README diagram, skill text, plan rows | Built, reviewed, gate green, **not merged** | branch `topology`, commits `d0a919b`, `b14f329` and `704b82d` (plan wording) on top of `main` `ec2c667`; pushed to `origin/topology` as a backup |

- Worktree: `<dev>\worktree_topology` (branch `topology`, clean). Keep it until
  the merge is done.
- Gate at `b14f329` (`uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run python tools/diagrams.py --check`): 855 passed, 14 skipped, all clean.
- Independent review (role-reviewer): APPROVE. Its should-fix and four nits are fixed in
  `b14f329`. A mutation check confirmed the three ordering tests fail if the refusal moves
  below the idempotency lookup.
- Plan numbering: topology took P35-P39 and D58-D60. lane-status has P31-P34, D47-D57 and
  Steps 16-18 on its own branch. Whoever merges second rebases the decision inventory.

## Pending and what it waits on

1. **Merge.** It waits on claude-messaging-fix's Step 14 checkpoint landing on `main`. Steps 13
   and 14 change `docs/lane-rules.md`, `README.md`, `docs/cli.md`, `src/passing_notes/cli.py` and
   the lane-open core, which this branch also changes.
2. **Lane-skill reinstall** for both hosts, after the merge only. Installing from the branch would
   ship unmerged text.
3. **Workspace instruction edits.** Central holds proposal note
   `20261117T013021Z-topology-to-central-42d4` (dev `CLAUDE.md`, `workspace-instructions.md`,
   `docs/lanes/README.md`) and applies them after the merge. Not this lane's files.
4. **P39 switch** (`central` to `main`), operational and central's own, at the operator's next
   coordinating-window restart.

## Exact next commands (when reopened)

Run in: a fresh window @ passing-notes · Model: Opus (default)

Session start (bind, inbox, cards):

```powershell
passing-notes card set --project passing-notes --lane topology --host claude --item 'topology: rebase + merge after Step 14' --state active --checkout <dev>/worktree_topology --branch topology
passing-notes inbox --project passing-notes --lane topology
passing-notes card show --project passing-notes --overlaps
```

Check the blocker (read-only). Step 14's checkpoint must be on `main`; stop and report if not:

```powershell
git -C <dev>\passing-notes log --oneline -5 main
```

Rebase and run the gate in the worktree (Git Bash). Resolve conflicts keeping both sides' intent;
the decision inventory takes both lanes' rows in number order:

```bash
cd <dev>/worktree_topology
git fetch origin
git rebase main
uv sync
uv run python tools/diagrams.py
uv run pytest -q; uv run ruff check .; uv run ruff format --check .; uv run mypy; uv run python tools/diagrams.py --check
```

If the rebase changes diagram text (Step 13 may edit `tools/diagrams.py`), commit the regenerated
SVGs. Then merge, through the checkout's shipper. Main has no `main` lane today, so ask central
in a `commit-request` note, or merge yourself if central delegates it:

```bash
git -C <dev>/passing-notes merge --ff-only topology
```

After the merge, reinstall the lane skills for both hosts, then tell central so it can apply
the workspace edits:

```powershell
cd <dev>\passing-notes
uv run python tools/skills_install.py install --host claude
uv run python tools/skills_install.py install --host codex
```

At the merge, retitle the plan section "Topology — built 2026-11-17, merge pending" to "shipped <date>". Also send lane-status the merged commit that holds `docs/protocol.md#topology` (it cites that
anchor). Then remove the worktree (`git -C <dev>/passing-notes worktree remove
<dev>/worktree_topology`, after `git -C <dev>/worktree_topology status
--porcelain` is empty), and post one handoff to central per finished item.

## Files this lane owns (on branch `topology`)

`docs/protocol.md` (Topology section and Store bullet), `docs/lane-rules.md` (sections 4, 6 and
8 topology text, delegated-decisions wording), `docs/cli.md` (post crossing text, exit codes),
`docs/codex-coordination.md` (central gloss), `README.md` ("Who talks to whom", rules 6-9,
coordinator wording), `docs/diagrams/topology-*.svg`, `tools/diagrams.py` (topology diagram),
`docs/plans/master_plan.md` (P35-P39, D58-D60, "Topology — shipped" record),
`docs/research/topology-proposal.html`, `src/passing_notes/protocol.py` (`WORKSPACE_PROJECT`,
`MAIN_LANE`, `crossing_allowed`), `src/passing_notes/cli.py` (`CROSS_PROJECT_NOT_ALLOWED`,
`_cmd_post` check, `--from-project` help), `src/passing_notes/queue.py` (`DEFAULT_COORDINATOR`
alias), `tests/test_cli.py` (topology tests at the end; two older cross-project tests changed),
`skills/lane-open/core.md`, `skills/lane-open/scripts/open-lane.ps1` (one message),
`skills/lane-close/core.md`, `skills/lane-close/providers/claude.md`.

## Related notes (ids only)

- To claude-messaging-fix: `20261116T230320Z-topology-to-claude-messaging-fix-edeb` (files;
  Stop hook should cover every binding a session holds), `20261117T013039Z-topology-to-claude-messaging-fix-3a68`
  (branch ready; operator-relay diagram still says "asks in its own window").
- With lane-status: `20261116T230623Z-topology-to-lane-status-72dc` (scope answers),
  `20261116T231441Z-topology-to-lane-status-db0b` (H4: "never shows a note body"),
  `20261117T011513Z-topology-to-lane-status-5739` (plan numbering).
- To central: `20261116T230530Z-topology-to-central-1da2` (proposal),
  `20261117T013022Z-topology-to-central-2619` (item 2 handoff),
  `20261117T013021Z-topology-to-central-42d4` (workspace edits).
