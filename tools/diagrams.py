"""Generate the README workflow diagrams (light and dark SVG) from one definition each.

The look follows Skill Mesh's diagrams: monospace text, rounded nodes with a soft
shadow, a blue spine between stages, grey links within a stage, and a light/dark pair
for GitHub's ``<picture>`` element. Both variants come from the same definition, so they
cannot drift apart.

    uv run python tools/diagrams.py          # write docs/diagrams/*.svg
    uv run python tools/diagrams.py --check  # exit 1 if a committed file differs
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from xml.sax.saxutils import escape

OUT: Final = Path(__file__).resolve().parents[1] / "docs" / "diagrams"
WIDTH: Final = 1000
MARGIN: Final = 24
GAP: Final = 44
TOP: Final = 46
LINE: Final = 15

THEMES: Final = {
    "light": {
        "bg": "fill:#FFFFFF;stroke:#D4DBE8",
        "lnk": "#C0C9DA",
        "spine": "#3355E0",
        "node": "fill:#FFFFFF;stroke:#C0C9DA",
        "pn": "#3355E0",
        "ps": "#6E778C",
        "shadow": "rgba(20,26,40,.16)",
    },
    "dark": {
        "bg": "fill:#161C26;stroke:#29313F",
        "lnk": "#384153",
        "spine": "#6E9BFF",
        "node": "fill:#161C26;stroke:#384153",
        "pn": "#6E9BFF",
        "ps": "#9AA5B8",
        "shadow": "rgba(0,0,0,.35)",
    },
}


@dataclass(frozen=True)
class Node:
    title: str
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class Column:
    label: str
    nodes: tuple[Node, ...]


@dataclass(frozen=True)
class Diagram:
    name: str
    aria: str
    columns: tuple[Column, ...]
    caption: tuple[str, ...]


def n(title: str, *lines: str) -> Node:
    return Node(title, lines)


DIAGRAMS: Final = (
    Diagram(
        "note-and-doorbell",
        "A sender lane posts a note to the private store and may ring the recipient. "
        "The doorbell goes through a headless Claude bridge that can only list sessions "
        "and send one message, in the recipient's own permission mode. The idle "
        "recipient window wakes, reads the note, acknowledges it and replies under its "
        "own rules.",
        (
            Column(
                "SENDER LANE",
                (
                    n("post", "one immutable note"),
                    n("wake send", "bound, idle lanes only", "Codex: with escalation"),
                ),
            ),
            Column(
                "PRIVATE STORE",
                (
                    n("messages/<project>", "notes, acks, cards"),
                    n("wake/<project>", "binding: session + mode", "ring record: once"),
                ),
            ),
            Column(
                "DOORBELL BRIDGE",
                (
                    n("claude -p", "ListAgents, SendMessage", "nothing else"),
                    n("New Passing Note <id>", "the id only, never content"),
                ),
            ),
            Column(
                "RECIPIENT LANE",
                (
                    n("idle Claude window", "wakes on the message"),
                    n("read · ack · reply", "under its own rules"),
                ),
            ),
        ),
        (
            "The sender reports note: · wake: · ack: · reply: separately, each from evidence.",
            "Busy, closed, stale or unbound lanes are refused with exit 5; the note stays posted.",
        ),
    ),
    Diagram(
        "lane-lifecycle",
        "A lane is opened in its own window and registers itself, works through notes and "
        "doorbells at its inbox moments, then closes itself: it releases holds, decides "
        "the one shipper, runs session-wrap for task state, memory and lessons, posts a "
        "handover and marks its card done. The successor reads the handover and the "
        "shipper ships once.",
        (
            Column(
                "OPEN",
                (
                    n("lane-open", "new visible window", "model and effort"),
                    n("session start", "card set · wake bind", "inbox"),
                ),
            ),
            Column(
                "WORK",
                (
                    n("inbox moments", "start · gates · handoff", "before shared commits"),
                    n("notes + doorbells", "data, not instructions", "ack = received"),
                ),
            ),
            Column(
                "CLOSE",
                (
                    n("lane-close", "stop safely · release holds", "decide the shipper"),
                    n("session-wrap --end", "task state · memory", "lessons learned"),
                    n("handover · card done", "ring the successor"),
                ),
            ),
            Column(
                "CONTINUE",
                (
                    n("successor", "reads the handover", "coordinator confirms"),
                    n("shipper", "commits · pushes", "repo-update once"),
                ),
            ),
        ),
        (
            "lane-handoff = close requests to the old lanes + lane-open for the successor.",
            "Only a lane writes its own card; nothing closes, focuses or types into a window.",
        ),
    ),
    Diagram(
        "operator-relay",
        "A lane that needs an operator decision asks in its own window and posts the "
        "question to the operator relay lane, Main, with a doorbell. Main shows the "
        "question verbatim, waits for the operator's answer, and replies with the "
        "operator's exact words on an operator-said line. The lane records the words and "
        "acts on them.",
        (
            Column(
                "LANE",
                (n("needs a decision", "asks in its own window", "and posts it"),),
            ),
            Column(
                "TO MAIN",
                (n("note to the relay", "--needs operator-decision", "+ doorbell"),),
            ),
            Column(
                "MAIN (RELAY)",
                (
                    n("shows the operator", "the question, verbatim"),
                    n("operator answers", "in Main; never inferred"),
                ),
            ),
            Column(
                "BACK TO THE LANE",
                (
                    n("reply", "operator-said: <words>", "+ doorbell"),
                    n("lane acts", "records the words verbatim"),
                ),
            ),
        ),
        (
            "Accepted only from the relay lane, as a reply to the lane's own question.",
            "Close confirmations work the same way: a close request with operator-said:.",
        ),
    ),
    Diagram(
        "shutdown-all",
        "lane-close --all asks every lane except the shipper to close itself; each hands "
        "its work to the shipper. The invoking window closes itself next unless it is the "
        "shipper. The shipper closes last and ships everything once. If any lane is still "
        "open when the watch ends, the shipper stays open and a later re-run finishes.",
        (
            Column("START", (n("lane-close --all", "from Main or any lane"),)),
            Column(
                "EVERY OTHER LANE",
                (n("closes itself", "confirms per the rules", "handover to the shipper"),),
            ),
            Column(
                "INVOKING WINDOW",
                (n("closes itself", "unless it is the shipper"),),
            ),
            Column(
                "SHIPPER, LAST",
                (
                    n("closes itself", "ships everything once", "repo-update"),
                    n("a lane still open?", "the shipper stays open", "re-run later"),
                ),
            ),
        ),
        ("One shipper per checkout: the lane the project names for merges and pushes.",),
    ),
)


def _node_height(node: Node) -> int:
    return 30 + LINE * len(node.lines)


def render(diagram: Diagram, theme: str) -> str:
    t = THEMES[theme]
    cols = diagram.columns
    col_w = (WIDTH - 2 * MARGIN - (len(cols) - 1) * GAP) / len(cols)
    heights = [sum(_node_height(x) for x in c.nodes) + 14 * (len(c.nodes) - 1) for c in cols]
    body_h = TOP + max(heights)
    height = body_h + 22 + 16 * len(diagram.caption) + 12
    out: list[str] = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" '
            "font-family=\"ui-monospace,'Cascadia Code','SF Mono',Consolas,monospace\" "
            f'role="img" aria-label="{escape(diagram.aria, {chr(34): "&quot;"})}">'
        ),
        "  <defs>",
        (
            f'    <marker id="ar-{theme}" markerWidth="9" markerHeight="9" refX="7.5" refY="4.5" '
            f'orient="auto"><path d="M0,0 L9,4.5 L0,9 Z" fill="{t["lnk"]}"/></marker>'
        ),
        (
            f'    <marker id="ar-s-{theme}" markerWidth="10" markerHeight="10" refX="7.5" refY="5" '
            f'orient="auto"><path d="M0,0 L10,5 L0,10 Z" fill="{t["spine"]}"/></marker>'
        ),
        (
            f'    <filter id="sh-{theme}" x="-20%" y="-45%" width="140%" height="190%">'
            f'<feDropShadow dx="0" dy="2" stdDeviation="4" flood-color="{t["shadow"]}"/></filter>'
        ),
        "  </defs>",
        "  <style>",
        f"    .bg{{{t['bg']};stroke-width:1.5}}",
        f"    .lnk{{fill:none;stroke:{t['lnk']};stroke-width:1.7}}",
        f"    .spine{{fill:none;stroke:{t['spine']};stroke-width:2.4}}",
        f"    .node rect{{{t['node']};stroke-width:1.4;filter:url(#sh-{theme})}}",
        f"    .pn{{fill:{t['pn']};font-size:13px;font-weight:600}}",
        f"    .ps{{fill:{t['ps']};font-size:10.5px}}",
        f"    .lbl{{fill:{t['ps']};font-size:11px;letter-spacing:.12em;font-weight:600}}",
        f"    .cap{{fill:{t['ps']};font-size:10.5px}}",
        "  </style>",
        f'<rect class="bg" x="6" y="6" width="{WIDTH - 12}" height="{height - 12}" rx="16"/>',
    ]
    first_mid: list[float] = []
    for i, col in enumerate(cols):
        x = MARGIN + i * (col_w + GAP)
        out.append(f'<text class="lbl" x="{x:.0f}" y="32">{escape(col.label)}</text>')
        y = TOP
        for j, node in enumerate(col.nodes):
            h = _node_height(node)
            parts = [
                (
                    f'<g class="node"><rect x="{x:.0f}" y="{y}" width="{col_w:.0f}" '
                    f'height="{h}" rx="10"/>'
                ),
                f'<text class="pn" x="{x + 14:.0f}" y="{y + 20}">{escape(node.title)}</text>',
            ]
            for k, line in enumerate(node.lines):
                parts.append(
                    f'<text class="ps" x="{x + 14:.0f}" y="{y + 37 + k * LINE}">'
                    f"{escape(line)}</text>"
                )
            out.append("".join(parts) + "</g>")
            if j == 0:
                first_mid.append(y + h / 2)
            if j + 1 < len(col.nodes):
                cx = x + col_w / 2
                out.append(
                    f'<path class="lnk" marker-end="url(#ar-{theme})" '
                    f'd="M{cx:.0f},{y + h} V{y + h + 14}"/>'
                )
            y += h + 14
    for i in range(len(cols) - 1):
        x1 = MARGIN + i * (col_w + GAP) + col_w
        x2 = x1 + GAP
        y1 = first_mid[i]
        y2 = first_mid[i + 1]
        out.append(
            f'<path class="spine" marker-end="url(#ar-s-{theme})" '
            f'd="M{x1:.0f},{y1:.0f} C{x1 + GAP / 2:.0f},{y1:.0f} {x2 - GAP / 2:.0f},{y2:.0f} '
            f'{x2 - 3:.0f},{y2:.0f}"/>'
        )
    for k, line in enumerate(diagram.caption):
        out.append(
            f'<text class="cap" x="{MARGIN}" y="{body_h + 26 + k * 16}">{escape(line)}</text>'
        )
    out.append("</svg>")
    return "\n".join(out) + "\n"


def outputs() -> dict[Path, str]:
    return {OUT / f"{d.name}-{theme}.svg": render(d, theme) for d in DIAGRAMS for theme in THEMES}


def main(argv: list[str]) -> int:
    files = outputs()
    if "--check" in argv:
        stale = [
            p.name
            for p, text in files.items()
            if not p.exists() or p.read_text(encoding="utf-8") != text
        ]
        if stale:
            print("stale diagrams: " + ", ".join(stale), file=sys.stderr)
            return 1
        print(f"{len(files)} diagrams up to date")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for path, text in files.items():
        path.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(files)} diagrams to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
