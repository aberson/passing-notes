# Seed: `status --mine`, several `--project`s, and `--live`

**Status:** approved by the operator 2026-11-18 ("write the seed and build it"). Built through `/build-step`.
**Why:** a window that is `main` in several projects has no good view of its own lanes. `status --project P` shows one project; `status --all-projects` shows every project in the store (30 projects and 113 lanes on 2026-11-18, mostly `done` lanes), which floods the chat. The operator wants to see "additional lanes, but not all".

## Change

All three are additive. Without them, `status` behaves exactly as today.

1. **`--project` may repeat.** `status --project say-no-more --project skill-mesh` shows those projects, in the order given, with duplicates dropped. Each one passes the same topology check (`status.refusal`) as a single `--project` does today; the first refused project fails the whole command with the existing `status-out-of-scope` error and prints nothing on stdout.
2. **`--mine`** shows the projects of the caller's live bindings (`Scope.caller`), sorted by project id. An unbound caller has no "mine": exit 2 `invalid-argument` with a one-line reason ("--mine needs a window bound to at least one lane"). The scope selectors stay mutually exclusive: exactly one of `--project` (one or more), `--all-projects` and `--mine`.
3. **`--live`** leaves `done` lanes out of the rows. It combines with any scope. Each project keeps its true `done_count`. In text output, a project with hidden done lanes gets one line `done: N hidden` after its rows (and `<project>: no lane cards` still appears only when the project has no cards at all). A project whose only cards are done shows just that line. JSON keeps the `passing-notes.status/1` schema: `lanes` omits the done rows, and `done_count` stays the real count. That is additive (the schema already carries `done_count`), so no schema version change.

## Where

- `src/passing_notes/status.py`: `collect` takes a sequence of projects (or None for all); a small helper for the `--mine` project list; `--live` filtering after `_project_block` so `done_count` is computed from every card; the text renderer's `done: N hidden` line.
- `src/passing_notes/cli.py`: `--project` becomes `action="append"`; new `--mine` and `--live` flags; the exactly-one-scope check; per-project refusal.
- `docs/cli.md`: the status section documents the three flags and the `--live` JSON rule.
- `skills/lane-status/core.md` (canonical) and its generated profiles: accept `--mine`, several `--project`s and `--live`; the default scope becomes `--mine --live` when the window holds lanes in more than one project, else today's default; the report's `Scope:` line names the flags; the rows rule says done lanes are listed unless `--live` was passed. Rebuild the installed profiles with the repository's skills installer.
- Tests (`tests/test_status.py` or the existing status test file): repeated `--project` (order, duplicates, a refused second project), `--mine` bound and unbound, `--live` with mixed and all-done projects in text and JSON (with `done_count` still true), and the mutual-exclusion errors. One end-to-end test through the real CLI entry point.

## Done when

- `uv run pytest -q` passes, plus `mypy` and `ruff check` / `ruff format --check` clean, per the repository's declared gate.
- From a window bound in several projects, `passing-notes status --mine --live` prints only those projects' non-done lanes, with a `done: N hidden` line per project.
- Nothing in the public repository names a private project or a home path (fixtures use synthetic project ids).
