"""Sublayer orchestration for Viper — main agent spawns parallel worker agents."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from tools import TOOL_DEFINITIONS, execute_tool


# ── model roster ───────────────────────────────────────────────────────────────
# All OpenRouter models. LLM picks the best one per task during planning.
MODEL_ROSTER: dict[str, str] = {
    "mistralai/mistral-7b-instruct:free":           "summarization, rewriting, translation, light reasoning",
    "meta-llama/llama-3.1-8b-instruct:free":        "classification, tagging, entity extraction, short Q&A",
    "google/gemma-2-9b-it:free":                    "sentiment analysis, simple classification, chat",
    "deepseek/deepseek-r1-distill-llama-70b:free":  "math, step-by-step reasoning, logic problems",
    "meta-llama/llama-3.3-70b-instruct:free":       "complex analysis, long-form writing, planning",
    "deepseek/deepseek-chat:free":                  "code generation, debugging, technical explanation",
    "anthropic/claude-3.5-haiku":                   "fast general tasks, structured output, tool use",
    "anthropic/claude-3.5-sonnet":                  "hard reasoning, research synthesis, creative writing",
    "openai/gpt-4o-mini":                           "general purpose, fast, cost-effective",
    "openai/gpt-4o":                                "complex reasoning, vision, high-stakes decisions",
    "perplexity/llama-3.1-sonar-large-128k-online": "real-time web search, up-to-date facts",
}


# ── shared state ───────────────────────────────────────────────────────────────
class SharedState:
    """Thread-safe key-value store visible to all workers and the orchestrator."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._lock = threading.Lock()

    def write(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def read(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)


# ── task / result ──────────────────────────────────────────────────────────────
@dataclass
class AgentTask:
    id: str
    role: str           # e.g. "researcher", "code_writer", "critic"
    goal: str           # self-contained instruction for this agent
    context: dict = field(default_factory=dict)   # read-only context from orchestrator
    tools: list | None = None  # None = all tools; [] = no tools (pure reasoning)
    model: str | None = None   # None = inherit orchestrator's model


@dataclass
class AgentResult:
    task_id: str
    role: str
    success: bool
    output: str
    error: str = ""
    attempts: int = 1


# ── worker agentic loop ────────────────────────────────────────────────────────
_MAX_WORKER_ITERS = 10
_MAX_TOOL_CHARS = 4_000


def _trim(s: str, n: int = _MAX_TOOL_CHARS) -> str:
    return s if len(s) <= n else s[:n] + f"\n[...{len(s) - n} chars omitted]"


def run_worker(client, task: AgentTask) -> str:
    """
    Run one worker agent to completion and return its output as a string.

    Model comes from task.model — set by the orchestrator before dispatch.
    Each worker has isolated message history, system prompt, and tool subset.
    """
    model = task.model  # always set by OrchestratorAgent.plan() or manually
    tool_defs = task.tools if task.tools is not None else TOOL_DEFINITIONS

    ctx_note = ""
    if task.context:
        ctx_note = f"\n\nContext provided by the orchestrator:\n{json.dumps(task.context, indent=2)}"

    system = (
        f"You are a specialized {task.role} agent. "
        f"Work autonomously until your goal is complete — do not ask for clarification. "
        f"End your final message with a concise summary of what you produced."
        f"{ctx_note}"
    )

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user",   "content": task.goal},
    ]

    for _ in range(_MAX_WORKER_ITERS):
        kwargs: dict = {"model": model, "messages": messages}
        if tool_defs:
            kwargs["tools"] = tool_defs

        resp = client.chat.completions.create(**kwargs)
        msg  = resp.choices[0].message

        # No tool calls → worker finished
        if not msg.tool_calls:
            return msg.content or "(worker produced no text)"

        # Append assistant turn
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id":   tc.id,
                    "type": "function",
                    "function": {
                        "name":      tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ],
        })

        # Execute tools and append results
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}

            result = execute_tool(tc.function.name, args)
            messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      _trim(result),
            })

    return f"[Worker hit {_MAX_WORKER_ITERS}-iteration cap — partial work above]"


# ── orchestrator ───────────────────────────────────────────────────────────────
class OrchestratorAgent:
    """
    Decomposes a request into parallel subtasks, runs specialized workers,
    handles errors / retries, shares state, then aggregates into a final answer.

    Usage:
        orch = OrchestratorAgent(client, model)
        answer = orch.execute(
            user_request,
            on_plan   = lambda tasks:  print(f"Planned {len(tasks)} tasks"),
            on_result = lambda result: print(f"  [{result.role}] done"),
        )
    """

    def __init__(
        self,
        client,
        model: str,
        worker_model: str | None = None,  # cheaper model for workers; None = same as model
        max_retries: int = 2,
        max_workers: int = 4,
    ) -> None:
        self.client       = client
        self.model        = model         # used for planning + aggregation
        self.worker_model = worker_model or model  # used for each worker
        self.max_retries  = max_retries
        self.max_workers  = max_workers
        self.state        = SharedState()

    # ── planning ───────────────────────────────────────────────────────────────
    def plan(self, user_request: str) -> list[AgentTask]:
        """Ask the LLM to decompose the request into 2–5 parallel subtasks.
        The LLM also picks the best model for each task from MODEL_ROSTER."""
        roster_lines = "\n".join(
            f'  "{mid}" — {desc}' for mid, desc in MODEL_ROSTER.items()
        )
        prompt = f"""You are an orchestrator. Decompose the request into 2-5 independent subtasks that can run in parallel.

User request: {user_request}

Available models (all on OpenRouter). Pick the best one for each task:
{roster_lines}

Respond with a JSON array ONLY (no prose). Each element:
  "id"          – short unique id like "t1", "t2"
  "role"        – agent role (e.g. "researcher", "code_writer", "critic", "data_analyst")
  "goal"        – self-contained instruction for this agent; include all context it needs
  "needs_tools" – true if the agent needs web/file/shell tools; false for pure reasoning
  "model"       – exact model id from the list above that best fits this task

Example:
[
  {{"id":"t1","role":"researcher","goal":"Search for X and summarize key findings","needs_tools":true,"model":"perplexity/llama-3.1-sonar-large-128k-online"}},
  {{"id":"t2","role":"summarizer","goal":"Condense the findings into 3 bullet points","needs_tools":false,"model":"mistralai/mistral-7b-instruct:free"}}
]"""

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user",   "content": prompt},
            ],
        )
        raw = (resp.choices[0].message.content or "[]").strip()

        # Strip markdown code fences if the model wrapped the JSON
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        plans = json.loads(raw)
        tasks = []
        for p in plans:
            tool_defs = TOOL_DEFINITIONS if p.get("needs_tools", True) else []
            # Use LLM-chosen model if valid, otherwise fall back to worker_model
            chosen = p.get("model", "")
            model  = chosen if chosen in MODEL_ROSTER else self.worker_model
            tasks.append(AgentTask(
                id=p["id"],
                role=p["role"],
                goal=p["goal"],
                context={"original_request": user_request},
                tools=tool_defs,
                model=model,
            ))
        return tasks

    # ── parallel execution + retries ───────────────────────────────────────────
    def run(self, tasks: list[AgentTask]) -> list[AgentResult]:
        """Execute tasks in parallel; retry failed ones with exponential back-off."""
        results: list[AgentResult] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._run_with_retry, task): task for task in tasks}
            for fut in as_completed(futures):
                results.append(fut.result())
        return results

    def _run_with_retry(self, task: AgentTask) -> AgentResult:
        last_error = ""
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                time.sleep(2 ** (attempt - 1))  # 1 s, 2 s, 4 s …

            try:
                # Inject a live state snapshot so each worker sees prior outputs
                task.context["shared_state"] = self.state.snapshot()
                output = run_worker(self.client, task)
                self.state.write(task.id, output)  # publish result for later workers / aggregation
                return AgentResult(task.id, task.role, True, output, attempts=attempt + 1)

            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)

        return AgentResult(
            task.id, task.role, False, "",
            error=last_error, attempts=self.max_retries + 1,
        )

    # ── aggregation ────────────────────────────────────────────────────────────
    def aggregate(self, user_request: str, results: list[AgentResult]) -> str:
        """Synthesize all worker outputs into one final answer."""
        good   = [r for r in results if r.success]
        failed = [r for r in results if not r.success]

        parts = [f"Original request: {user_request}\n"]
        for r in good:
            parts.append(f"=== {r.role} ({r.task_id}) ===\n{r.output}\n")
        if failed:
            parts.append(
                "=== FAILED TASKS ===\n"
                + "\n".join(f"  {r.task_id} ({r.role}): {r.error}" for r in failed)
            )
        parts.append("\nSynthesize the above into one clear, complete answer for the user.")

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a synthesizer. Combine worker results into one answer."},
                {"role": "user",   "content": "\n".join(parts)},
            ],
        )
        return resp.choices[0].message.content or ""

    # ── one-shot entrypoint ────────────────────────────────────────────────────
    def execute(
        self,
        user_request: str,
        on_plan:   Callable[[list[AgentTask]], None]   | None = None,
        on_result: Callable[[AgentResult], None]        | None = None,
    ) -> str:
        """
        Full pipeline: plan → parallel workers → aggregate.

        Callbacks (both optional):
          on_plan(tasks)    – called once after planning, before any worker starts
          on_result(result) – called as each worker finishes (from a thread-pool thread)
        """
        tasks = self.plan(user_request)
        if on_plan:
            on_plan(tasks)

        for task in tasks:
            task.context["shared_state"] = self.state.snapshot()

        results: list[AgentResult] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._run_with_retry, task): task for task in tasks}
            for fut in as_completed(futures):
                result = fut.result()
                results.append(result)
                if on_result:
                    on_result(result)

        return self.aggregate(user_request, results)
