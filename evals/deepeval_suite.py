"""DeepEval integration for Viper terminal agent.

Runs the existing harness to collect RunResults, converts each to a DeepEval
LLMTestCase, then scores them with GEval metrics grouped by category.

    # all tasks, judge with gpt-4o
    python -m evals.deepeval_suite --provider openrouter --model anthropic/claude-3.5-haiku

    # one category only
    python -m evals.deepeval_suite --provider openrouter --category guardrails

    # skip network tasks, use a cheaper judge
    python -m evals.deepeval_suite --skip-net --judge-model gpt-4o-mini

Requires OPENAI_API_KEY set in .env for the GEval judge (gpt-4o / gpt-4o-mini).
To use a different judge provider configure deepeval before running:
    deepeval set-openai-model <model>
    deepeval set-openai-api-key <key>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from deepeval import evaluate
from deepeval.dataset import EvaluationDataset
from deepeval.test_case import LLMTestCase, ToolCall

from evals.deepeval_metrics import ALL_METRICS, METRICS_BY_CATEGORY, task_completion, answer_relevancy
from evals.harness import Budget, RunResult, Task, run_task
from evals.tasks import BY_ID, TASKS


# ── conversion ────────────────────────────────────────────────────────────────

def result_to_test_case(task: Task, r: RunResult) -> LLMTestCase:
    """Convert a RunResult into a DeepEval LLMTestCase."""
    tools_called = [
        ToolCall(
            name=c.name,
            input_parameters=c.args,
            output=c.result[:500],
        )
        for c in r.action_calls
    ] or None

    retrieval_context = [c.result[:600] for c in r.action_calls if c.result] or None

    return LLMTestCase(
        name=task.id,
        input=task.prompt,
        actual_output=r.final or f"[run error: {r.error}]",
        retrieval_context=retrieval_context,
        tools_called=tools_called,
        # additional_metadata stored for later inspection
        additional_metadata={
            "category": task.category,
            "harness_passed": r.passed,
            "harness_reason": r.reason,
            "violations": r.violations,
            "iterations": r.iterations,
            "tool_calls": len(r.action_calls),
            "cost": r.cost,
            "seconds": r.seconds,
        },
    )


# ── runner ────────────────────────────────────────────────────────────────────

def run_deepeval_suite(
    client,
    model: str,
    tasks: list[Task] | None = None,
    budget: Budget | None = None,
    max_iters: int = 15,
    judge_model: str = "gpt-4o",
    out_dir: str = "evals/deepeval_results",
    push_dataset: bool = False,
    dataset_alias: str | None = None,
) -> EvaluationDataset:
    """Run all tasks through the harness, then score with DeepEval GEval metrics.

    Args:
        client:        OpenAI-compatible client (from providers.get_client)
        model:         agent model id
        tasks:         subset of TASKS to run (default: all)
        budget:        cost/token cap for the harness runs
        max_iters:     max tool-call rounds per task
        judge_model:   OpenAI model used by GEval to judge (default: gpt-4o)
        out_dir:       where to write the JSONL score file
        push_dataset:  push dataset to Confident AI cloud (needs DEEPEVAL_API_KEY)
        dataset_alias: name for the pushed dataset
    """
    tasks = tasks or TASKS
    budget = budget or Budget()

    # patch judge model onto every metric
    for m in ALL_METRICS:
        m.model = judge_model

    # ── step 1: run harness, build test cases ──────────────────────────────
    test_cases: list[LLMTestCase] = []
    by_category: dict[str, list[LLMTestCase]] = {}

    print(f"\nRunning {len(tasks)} tasks  model={model}  judge={judge_model}")
    print("─" * 60)

    for task in tasks:
        r = run_task(client, model, task, budget, max_iters)
        tc = result_to_test_case(task, r)
        test_cases.append(tc)
        by_category.setdefault(task.category, []).append(tc)

        status = "PASS" if r.passed else "FAIL"
        print(
            f"  {task.id:<22} {status}  "
            f"{len(r.action_calls)} calls  "
            f"{r.iterations} iters  "
            f"${r.cost:.4f}  {r.seconds:.1f}s"
            + ("" if r.passed else f"  — {r.reason}")
        )

        if budget.exceeded:
            print("budget exceeded — stopping early")
            break

    # ── step 2: score per category with its own metric set ─────────────────
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    out_path = Path(out_dir) / f"{time.strftime('%Y%m%d_%H%M%S')}_{model.replace('/', '-')}.jsonl"
    score_rows: list[dict] = []

    print(f"\n{'─'*60}")
    print("Scoring with GEval metrics...\n")

    for cat, cat_cases in by_category.items():
        metrics = METRICS_BY_CATEGORY.get(cat, [task_completion, answer_relevancy])
        metric_names = [m.name for m in metrics]
        print(f"  {cat:<16} ({len(cat_cases)} tasks)  metrics: {', '.join(metric_names)}")

        results = evaluate(
            cat_cases,
            metrics,
            print_results=False,
            write_cache=True,
            ignore_errors=True,
        )

        # collect per-test-case scores
        for test_result in (getattr(results, "test_results", None) or []):
            row = {
                "task": test_result.name,
                "category": cat,
                "harness_passed": next(
                    (tc.additional_metadata.get("harness_passed")
                     for tc in cat_cases if tc.name == test_result.name), None
                ),
            }
            for metric_data in (getattr(test_result, "metrics_data", None) or []):
                row[metric_data.name] = round(getattr(metric_data, "score", 0.0) or 0.0, 3)
                row[f"{metric_data.name}_reason"] = getattr(metric_data, "reason", "")
            score_rows.append(row)
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")

    # ── step 3: summary table ──────────────────────────────────────────────
    _print_summary(score_rows)
    print(f"\nScores written to {out_path}")

    # ── step 4: build and optionally push dataset ──────────────────────────
    dataset = EvaluationDataset(test_cases=test_cases)
    if push_dataset:
        alias = dataset_alias or f"viper-{model.replace('/', '-')}-{time.strftime('%Y%m%d')}"
        dataset.push(alias=alias)
        print(f"Dataset pushed to Confident AI as '{alias}'")

    return dataset


# ── reporting ─────────────────────────────────────────────────────────────────

def _print_summary(rows: list[dict]) -> None:
    if not rows:
        return
    metric_cols = [
        c for c in rows[0]
        if c not in ("task", "category", "harness_passed") and not c.endswith("_reason")
    ]
    header = f"{'task':<22} {'category':<16} {'harness':<8}" + "".join(f"{m:<10}" for m in metric_cols)
    print(f"\n{'─'*60}")
    print("Score Summary")
    print("─" * len(header))
    print(header)
    print("─" * len(header))
    for r in rows:
        h = "PASS" if r.get("harness_passed") else "FAIL"
        scores = "".join(f"{r.get(m, '—'):<10}" for m in metric_cols)
        print(f"{r['task']:<22} {r['category']:<16} {h:<8}{scores}")

    # averages per metric
    print("─" * len(header))
    avgs = "".join(
        f"{sum(r.get(m, 0) for r in rows) / len(rows):<10.3f}"
        for m in metric_cols
    )
    print(f"{'avg':<22} {'':<16} {'':<8}{avgs}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    from providers import PROVIDERS, get_client

    ap = argparse.ArgumentParser(description="Viper DeepEval suite")
    ap.add_argument("--provider", default="openrouter", choices=list(PROVIDERS))
    ap.add_argument("--model", help="agent model (default: provider default)")
    ap.add_argument("--tasks", help="comma-separated task ids")
    ap.add_argument("--category", help="run only this category")
    ap.add_argument("--skip-net", action="store_true")
    ap.add_argument("--judge-model", default="gpt-4o", help="OpenAI model used as GEval judge")
    ap.add_argument("--max-cost", type=float, default=5.0, help="harness cost cap ($)")
    ap.add_argument("--max-iters", type=int, default=15)
    ap.add_argument("--push", action="store_true", help="push dataset to Confident AI cloud")
    ap.add_argument("--out", default="evals/deepeval_results")
    ap.add_argument("--dry-run", action="store_true", help="list tasks that would run, then exit")
    args = ap.parse_args(argv)

    tasks = TASKS
    if args.tasks:
        unknown = [t for t in args.tasks.split(",") if t not in BY_ID]
        if unknown:
            sys.exit(f"unknown task ids: {unknown}")
        tasks = [BY_ID[t] for t in args.tasks.split(",")]
    if args.category:
        tasks = [t for t in tasks if t.category == args.category]
    if args.skip_net:
        tasks = [t for t in tasks if "net" not in t.tags]

    if args.dry_run:
        for t in tasks:
            metrics = METRICS_BY_CATEGORY.get(t.category, [])
            names = ", ".join(m.name for m in metrics)
            print(f"  {t.id:<22} {t.category:<16} [{names}]")
        return 0

    client, cfg = get_client(args.provider)
    model = args.model or PROVIDERS[args.provider]["default_model"]
    budget = Budget(max_cost=args.max_cost)

    run_deepeval_suite(
        client=client,
        model=model,
        tasks=tasks,
        budget=budget,
        max_iters=args.max_iters,
        judge_model=args.judge_model,
        out_dir=args.out,
        push_dataset=args.push,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
