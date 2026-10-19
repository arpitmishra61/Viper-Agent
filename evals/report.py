"""Summarise eval result files (JSONL, one line per run).

    python -m evals.report evals/results/*.jsonl
"""

from __future__ import annotations

import glob
import json
import sys
from collections import defaultdict
from pathlib import Path


def load(paths) -> list[dict]:
    rows: list[dict] = []
    for pattern in paths:
        for p in sorted(glob.glob(str(pattern))) or [str(pattern)]:
            for line in Path(p).read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
    return rows


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def render(rows: list[dict]) -> str:
    if not rows:
        return "no results"
    by_model: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_model[(r["provider"], r["model"])].append(r)

    out: list[str] = ["## Overall", ""]
    out.append("| model | runs | pass | avg tool calls | avg iters | avg tokens | avg sec | cost |")
    out.append("|---|---|---|---|---|---|---|---|")
    for (prov, model), rs in by_model.items():
        passed = sum(r["passed"] for r in rs)
        out.append(
            f"| {prov}:{model} | {len(rs)} | {passed}/{len(rs)} ({100 * passed / len(rs):.0f}%) "
            f"| {_mean(r['tool_calls'] for r in rs):.1f} | {_mean(r['iterations'] for r in rs):.1f} "
            f"| {_mean(r['prompt_tokens'] + r['completion_tokens'] for r in rs):,.0f} "
            f"| {_mean(r['seconds'] for r in rs):.1f} | ${sum(r['cost'] for r in rs):.4f} |"
        )

    cats = sorted({r["category"] for r in rows})
    out += ["", "## Pass rate by category", "", "| category | " + " | ".join(f"{p}:{m}" for p, m in by_model) + " |",
            "|---|" + "---|" * len(by_model)]
    for cat in cats:
        cells = []
        for key in by_model:
            rs = [r for r in by_model[key] if r["category"] == cat]
            cells.append(f"{sum(r['passed'] for r in rs)}/{len(rs)}" if rs else "-")
        out.append(f"| {cat} | " + " | ".join(cells) + " |")

    fails: dict[tuple, list[str]] = defaultdict(list)
    for r in rows:
        if not r["passed"]:
            fails[(r["task"], f"{r['provider']}:{r['model']}")].append(r["reason"])
    out += ["", "## Failures", ""]
    if not fails:
        out.append("none")
    for (task, model), reasons in sorted(fails.items()):
        out.append(f"- **{task}** — {model} ({len(reasons)}x): {reasons[0]}")
    return "\n".join(out)


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.exit("usage: python -m evals.report <results.jsonl> [...]")
    print(render(load(argv)))


if __name__ == "__main__":
    main()
