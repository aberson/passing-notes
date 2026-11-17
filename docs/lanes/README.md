# Lanes

Standing lanes for this project (Passing Notes id `passing-notes`). Shared rules:
[lane rules](../lane-rules.md). This file adds only local rules and never weakens them.

```text
Lanes:           claude-messaging-fix - lane messaging repair (build-phase Steps 11-14)
                 topology - DONE 2026-11-17 (shipped 4a72190 + 592e379; lane skills reinstalled) - dev main / project main topology rules, CLI refusal, README diagram
                 lane-status - REOPENED 2026-11-17 (building Steps 16-17; resume doc docs/lanes/parked/lane-status.md) - read-only `passing-notes status` command and `lane-status` skill
Coordinator:     central   (the operator's coordinating window, standing in while this project has no main)
Shipper:         central
Operator relay:  central
Lane rules:      docs/lane-rules.md, plus the project rules below
```

## Project rules

- Parked lanes keep their entry: their cards are `done`, their resume docs live in
  `docs/lanes/parked/`, and the coordinator reopens them with `lane-open` under the same id
  when their blocker clears. Both parked lanes wait on `claude-messaging-fix`'s Steps 13-14
  landing on `main`: `topology` merges its branch after Step 14, then `lane-status` builds.
- `central` becomes `main` at the operator's next coordinating-window restart (plan P39).
