"""spawn_agents tool — lets the main agent trigger multi-agent orchestration."""

# Client + model are injected at runtime by __init__.py after agent starts.
_client = None
_model  = None


def init(client, model: str) -> None:
    global _client, _model
    _client = client
    _model  = model


def spawn_agents(request: str) -> str:
    """Decompose request into parallel worker agents and return aggregated result."""
    if _client is None:
        return "[spawn_agents] not initialised — call tools.multiagent.init(client, model) first"

    from rich.console import Console
    from orchestrator import OrchestratorAgent

    console = Console(highlight=False)

    def _on_plan(tasks):
        console.print(f"\n  🧩  [bold #a78bfa]spawning {len(tasks)} agents[/bold #a78bfa]")
        for t in tasks:
            model_hint = (t.model or "?").split("/")[-1]
            tools_hint = "tools" if t.tools else "no tools"
            console.print(
                f"       [bold #fbbf24]{t.id}[/bold #fbbf24]  {t.role}"
                f"  [dim]{model_hint}  ·  {tools_hint}[/dim]"
            )
        console.print()

    def _on_result(result):
        icon = "✅" if result.success else "❌"
        retry = f"  [dim](retried {result.attempts - 1}x)[/dim]" if result.attempts > 1 else ""
        color = "#39ff14" if result.success else "#ef4444"
        console.print(f"  {icon}  [bold {color}]{result.role}[/bold {color}]  [dim]{result.task_id} done[/dim]{retry}")

    orch = OrchestratorAgent(_client, _model)
    return orch.execute(request, on_plan=_on_plan, on_result=_on_result)


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "spawn_agents",
            "description": (
                "Decompose a complex request into parallel specialist agents and return their "
                "combined result. Use this when the task has multiple independent parts that "
                "can run simultaneously (e.g. research + code + review, or classify + summarize "
                "+ translate). Do NOT use for simple single-step tasks or sequential workflows "
                "where step B needs step A's output."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "request": {
                        "type": "string",
                        "description": "Full description of the task to distribute across agents.",
                    }
                },
                "required": ["request"],
            },
        },
    }
]
