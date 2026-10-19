# Viper evals

22 tasks, each with a deterministic checker (no LLM judge). The runner drives the real tools and
the real guardrail pipeline (`agent._run_tools`) against any provider/model.

```powershell
# from terminal-agent/, using the project venv
python -m evals.run_evals --dry-run                                   # list tasks, no API calls
python -m evals.run_evals --provider ollama --models llama3.2         # free, local
python -m evals.run_evals --provider openrouter --models openrouter/free --skip-net
python -m evals.run_evals --provider openrouter --models <model-a>,<model-b> --repeats 2 --max-cost 3
python -m evals.report evals/results/*.jsonl                          # compare runs
```

Model ids: see https://openrouter.ai/models (prices are shown there too).

## Cost control
- `--max-cost` (default $3) stops the whole run once reported spend reaches it. OpenRouter reports
  real per-request cost; for providers that don't, pass `--price-in/--price-out` or use `--max-tokens`.
- `--max-iters` (default 15) bounds tool-call rounds per task.
- Loop order is repeat -> task -> model, so a budget stop leaves balanced coverage across models.
- Results are written per run, so nothing is lost on a stop or crash.

## Safety while evaluating
- Each run uses a throwaway workspace; the cwd is restored afterwards.
- Yes/no guardrail prompts are answered by the task (`answer=`), never by a human.
- `run_shell`/`run_python` calls that touch `~/.ssh`, `~/.aws`, the metadata IP, etc. are **not executed**;
  they are recorded as violations. Host package installs are blocked the same way.
- `sandbox=True` calls are simulated, so no E2B credit is used.
- Remaining risk: a model can still run other commands on the host inside the workspace. The
  dangerous-command guard prompts and the task answers "no", but treat the machine as you would for
  any agent run.

## Adding a task
Add a `Task(...)` to `evals/tasks.py` with a checker returning `(passed, reason)`. `tests/test_evals.py`
checks every task is well-formed and that doing nothing never crashes a checker.

## Caveat
`evals/harness.py` re-implements the loop from `agent.chat()` (non-streaming, to get token usage).
If `chat()`'s loop changes, mirror it here.
