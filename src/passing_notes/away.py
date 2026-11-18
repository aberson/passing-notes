"""The read-only before-away check, derived only from the scoped status report."""

from __future__ import annotations

from dataclasses import dataclass

from passing_notes import status

SCHEMA = "passing-notes.away-check/1"
EXIT_FINDINGS = 3
KINDS = ("pending-approval", "suspected-stall", "operator-wait", "operator-decisions")


@dataclass(frozen=True)
class Finding:
    project: str
    lane: str | None
    kind: str
    count: int
    waiting_on: str | None
    item: str


@dataclass(frozen=True)
class Check:
    report: status.Report
    findings: tuple[Finding, ...]
    unknown_activity: int

    @property
    def exit_code(self) -> int:
        return EXIT_FINDINGS if self.findings else 0


def inspect(report: status.Report) -> Check:
    """A lane's strongest observed operator need, plus each project's decision count.

    Ordinary lane replies, card-age flags and resource/lane waits are not evidence
    that the operator is needed. Unknown activity is counted, never called healthy.
    """
    findings: list[Finding] = []
    unknown = 0
    for block in report.projects:
        for row in block.lanes:
            if row.state == status.DONE_STATE:
                continue
            unknown += row.evidence.activity == "unknown"
            if row.evidence.activity in KINDS[:2]:
                kind = row.evidence.activity
            elif row.waiting_on == "operator":
                kind = "operator-wait"
            else:
                continue
            findings.append(Finding(block.project, row.lane, kind, 1, row.waiting_on, row.item))
        if block.operator_decisions:
            findings.append(
                Finding(
                    block.project,
                    None,
                    "operator-decisions",
                    block.operator_decisions,
                    "operator",
                    "",
                )
            )
    findings.sort(
        key=lambda finding: (KINDS.index(finding.kind), finding.project, finding.lane or "")
    )
    return Check(report, tuple(findings), unknown)


def to_json(check: Check) -> dict[str, object]:
    base = status.to_json(check.report)
    return {
        "schema": SCHEMA,
        "generated_utc": check.report.generated_utc,
        "scope": base["scope"],
        "findings": [
            {
                "project": f.project,
                "lane": f.lane,
                "kind": f.kind,
                "count": f.count,
                "waiting_on": f.waiting_on,
                "item": f.item,
            }
            for f in check.findings
        ],
        "unknown_activity": check.unknown_activity,
        "resources": base["resources"],
        "diagnostics": base["diagnostics"],
        "exit_code": check.exit_code,
    }


def render_text(check: Check) -> list[str]:
    lines = [status.resource_line(check.report.resources)]
    if check.findings:
        columns = ["PROJECT", "LANE", "NEEDS OPERATOR", "COUNT", "WAITING ON", "WORKING ON"]
        rows = [
            [
                f.project,
                f.lane or "-",
                f.kind,
                str(f.count),
                f.waiting_on or "-",
                status.ascii_cell(f.item, status.ITEM_WIDTH, keep_tail=True) or "-",
            ]
            for f in check.findings
        ]
        widths = [max(len(row[i]) for row in [columns, *rows]) for i in range(len(columns) - 1)]
        for row in [columns, *rows]:
            lines.append(
                "  ".join(
                    [
                        *(text.ljust(width) for text, width in zip(row[:-1], widths, strict=True)),
                        row[-1],
                    ]
                )
            )
    else:
        lines.append("no observed operator actions")
    lines.append(f"findings: {len(check.findings)}  unknown activity: {check.unknown_activity}")
    if check.unknown_activity or check.report.diagnostics:
        lines.append("evidence incomplete: no finding is not proof that every lane is healthy")
    return lines
