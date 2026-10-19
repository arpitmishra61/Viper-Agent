"""Run the eval suite against one provider and one or more models.

    python -m evals.run_evals --dry-run
    python -m evals.run_evals --provider ollama --models llama3.2 --repeats 1
    python -m evals.run_evals --provider openrouter --models openrouter/free,anthropic/claude-haiku-5-5 \
        --repeats 2 --max-cost 3 --skip-net

Results are appended to evals/results/<timestamp>_<provider>.jsonl as each run finishes, so a
crash or a budget stop never loses data. Then: python -m evals.report evals/results/*.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from evals.harness import Budget, run_task
from evals.report import render
from evals.tasks import BY_ID, TASKS

_EST_TOKENS_PER_RUN = 35_000   # rough: ~30k in + ~2k out, from the cost estimate in the README


def _brief(args: dict, limit: int = 200) -> dict:
    """Tool arguments for the results log, with long values cut so files stay small."""
    return {k: (v if not isinstance(v, str) or len(v) <= limit else v[:limit] + "...") for k, v in args.items()}


def _select(args) -> list:
    tasks = TASKS
    if args.tasks:
        unknown = [t for t in args.tasks.split(",") if t not in BY_ID]
        if unknown:
            sys.exit(f"unknown task ids: {unknown}. available: {sorted(BY_ID)}")
        tasks = [BY_ID[t] for t in args.tasks.split(",")]
    if args.category:
        tasks = [t for t in tasks if t.category == args.category]
    if args.skip_net:
        tasks = [t for t in tasks if "net" not in t.tags]
    return tasks


def main(argv=None) -> int:
    from providers import PROVIDERS, get_client

    ap = argparse.ArgumentParser(description="Viper eval harness")
    ap.add_argument("--provider", default="openrouter", choices=list(PROVIDERS))
    ap.add_argument("--models", help="comma-separated model ids (default: the provider's default)")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--tasks", help="comma-separated task ids")
    ap.add_argument("--category")
    ap.add_argument("--skip-net", action="store_true", help="skip tasks that need the internet")
    ap.add_argument("--max-iters", type=int, default=15, help="tool-call rounds per task")
    ap.add_argument("--max-cost", type=float, default=3.0, help="stop when reported spend reaches this ($)")
    ap.add_argument("--max-tokens", type=int, default=None, help="stop after this many total tokens")
    ap.add_argument("--price-in", type=float, default=0.0, help="$/1M input tokens if the API reports no cost")
    ap.add_argument("--price-out", type=float, default=0.0, help="$/1M output tokens if the API reports no cost")
    ap.add_argument("--out", default=str(Path(__file__).parent / "results"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    tasks = _select(args)
    models = [m.strip() for m in args.models.split(",")] if args.models else [PROVIDERS[args.provider]["default_model"]]
    total = len(tasks) * args.repeats * len(models)

    print(f"{len(tasks)} tasks x {args.repeats} repeat(s) x {len(models)} model(s) = {total} runs")
    print(f"models: {models}")
    est = total * _EST_TOKENS_PER_RUN
    print(f"rough token use: ~{est / 1e6:.1f}M tokens   (cost cap: ${args.max_cost:.2f})")
    if args.dry_run:
        for t in tasks:
            print(f"  {t.id:<16} {t.category:<15} {'[net] ' if 'net' in t.tags else ''}{t.prompt[:70]}")
        return 0

    # make `python` inside run_shell resolve to the interpreter running this harness
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

    client, cfg = get_client(args.provider)
    extra_body = {"usage": {"include": True}} if "openrouter.ai" in cfg["base_url"] else None
    if extra_body is None and args.price_in == 0 and args.price_out == 0 and args.max_tokens is None:
        print("note: this provider reports no cost, so --max-cost cannot trigger; use --max-tokens to bound the run")

    budget = Budget(args.max_cost, args.max_tokens, args.price_in, args.price_out)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{args.provider}.jsonl"
    rows: list[dict] = []
    n = 0

    try:
        for rep in range(args.repeats):
            for task in tasks:
                for model in models:
                    if budget.exceeded:
                        raise StopIteration
                    n += 1
                    r = run_task(client, model, task, budget, args.max_iters, extra_body=extra_body)
                    row = {
                        "provider": args.provider, "model": model, "task": task.id, "category": task.category,
                        "repeat": rep, "passed": r.passed, "reason": r.reason,
                        "iterations": r.iterations, "tool_calls": len(r.action_calls),
                        "tools": [c.name for c in r.action_calls],
                        "calls": [{"name": c.name, "args": _brief(c.args), "result": c.result[:120]}
                                  for c in r.action_calls],
                        "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
                        "cost": round(r.cost, 6), "seconds": round(r.seconds, 2),
                        "hit_cap": r.hit_cap, "error": r.error, "violations": r.violations,
                        "guard_prompts": len(r.prompts),
                    }
                    rows.append(row)
                    with out_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(row) + "\n")
                    print(
                        f"[{n}/{total}] {task.id:<16} {model[:28]:<28} {'PASS' if r.passed else 'FAIL'}  "
                        f"{len(r.action_calls)} calls  {(r.prompt_tokens + r.completion_tokens) / 1000:.1f}k tok  "
                        f"${r.cost:.4f}  {r.seconds:.1f}s" + ("" if r.passed else f"  -- {r.reason}")
                    )
    except StopIteration:
        print(f"\nstopped early: budget reached (spent ${budget.cost:.4f}, {budget.tokens:,} tokens)")
    except KeyboardInterrupt:
        print("\ninterrupted")

    print(f"\ntotal spend: ${budget.cost:.4f}   results: {out_path}\n")
    print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
