#!/usr/bin/env python3
"""Viper — terminal agent. Minimal, fast, multi-provider."""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from providers import PROVIDERS, get_client
from system_prompt import SYSTEM_PROMPT
from tools import TOOL_DEFINITIONS, execute_tool
from tools.state import get_cwd, set_cwd
from tools.think import is_verbose as _think_verbose, set_verbose as _think_set_verbose

console = Console(highlight=False)

# ── theme ─────────────────────────────────────────────────────────────────────
_GREEN  = "#39ff14"
_SILVER = "#94a3b8"
_AMBER  = "#fbbf24"
_RED    = "#ef4444"
_VIOLET = "#a78bfa"

_BANNER = """\
[bold #39ff14] ██╗   ██╗██╗██████╗ ███████╗██████╗ [/bold #39ff14]
[bold #39ff14] ██║   ██║██║██╔══██╗██╔════╝██╔══██╗[/bold #39ff14]
[bold #39ff14] ██║   ██║██║██████╔╝█████╗  ██████╔╝[/bold #39ff14]
[bold #39ff14] ╚██╗ ██╔╝██║██╔═══╝ ██╔══╝  ██╔══██╗[/bold #39ff14]
[bold #39ff14]  ╚████╔╝ ██║██║     ███████╗██║  ██║ [/bold #39ff14]
[bold #39ff14]   ╚═══╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝[/bold #39ff14]"""

# ── limits ────────────────────────────────────────────────────────────────────
_MAX_TOOL_OUTPUT_CHARS = 8_000   # ~2k tokens per tool result
_MAX_CONTEXT_CHARS     = 100_000  # ~25k tokens; prune when exceeded
_MAX_ITERATIONS        = 20       # tool-call rounds per user turn

# ── dangerous shell patterns ──────────────────────────────────────────────────
_DANGEROUS_RE = re.compile(
    r"\brm\s+-[rRfF]"
    r"|rmdir\s+/[sqSQ]"
    r"|del\s+/[fFqQ]"
    r"|\bformat\s+[a-zA-Z]:"
    r"|\bdd\s+if="
    r"|DROP\s+(TABLE|DATABASE)"
    r"|mkfs\."
    r"|\b(poweroff|reboot|shutdown)\b",
    re.IGNORECASE,
)

# ── session storage ───────────────────────────────────────────────────────────
_SESSIONS_DIR = Path.home() / ".viper" / "sessions"


# ── token helpers ─────────────────────────────────────────────────────────────
def _estimate_tokens(messages: list) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages) // 4


def _context_chars(messages: list) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages)


def _prune_context(messages: list) -> list:
    """Drop oldest non-system messages until under the char limit."""
    while _context_chars(messages) > _MAX_CONTEXT_CHARS:
        for i, m in enumerate(messages):
            if m["role"] != "system":
                messages.pop(i)
                break
        else:
            break
    return messages


def _truncate_result(result: str) -> str:
    if len(result) <= _MAX_TOOL_OUTPUT_CHARS:
        return result
    cut = _MAX_TOOL_OUTPUT_CHARS
    return result[:cut] + f"\n\n[truncated — {len(result) - cut} more chars omitted]"


# ── session persistence ───────────────────────────────────────────────────────
def _save_session(messages: list, provider: str, model: str) -> Path:
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = _SESSIONS_DIR / f"{ts}.json"
    path.write_text(
        json.dumps({"provider": provider, "model": model, "messages": messages}, indent=2),
        encoding="utf-8",
    )
    return path


def _load_last_session() -> dict | None:
    if not _SESSIONS_DIR.exists():
        return None
    files = sorted(_SESSIONS_DIR.glob("*.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


# ── rendering ─────────────────────────────────────────────────────────────────
def _panel(text: str) -> Panel:
    body = Markdown(text) if text.strip() else Text("…", style="dim")
    return Panel(
        body,
        border_style=_GREEN,
        title=f"[bold {_GREEN}]viper[/bold {_GREEN}]",
        title_align="left",
        padding=(0, 1),
    )


# ── streaming ─────────────────────────────────────────────────────────────────
def _stream(client, model: str, messages: list, use_tools: bool = True):
    """Stream API response. Returns (text, tool_calls)."""
    full_text = ""
    tc_map: dict[int, dict] = {}

    kwargs: dict = {"model": model, "messages": messages, "stream": True}
    if use_tools:
        kwargs["tools"] = TOOL_DEFINITIONS

    live = Live(console=console, refresh_per_second=20)
    started = False

    try:
        for chunk in client.chat.completions.create(**kwargs):
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta.content:
                full_text += delta.content
                if not started:
                    live.start()
                    started = True
                live.update(_panel(full_text))

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    i = tc.index
                    if i not in tc_map:
                        tc_map[i] = {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    if tc.id:
                        tc_map[i]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tc_map[i]["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            tc_map[i]["function"]["arguments"] += tc.function.arguments

    except Exception as e:
        if started:
            live.stop()
        msg = str(e).lower()
        if use_tools and any(w in msg for w in ("tool", "function", "unsupported")):
            console.print("[dim] ↻  retrying without tools[/dim]")
            return _stream(client, model, messages, use_tools=False)
        console.print(f"\n  [bold {_RED}]error:[/bold {_RED}] {e}\n")
        return "", []

    if started:
        live.stop()
        console.print()

    return full_text, [tc_map[i] for i in sorted(tc_map)]


# ── tool display ──────────────────────────────────────────────────────────────
def _show_tool_call(name: str, args: dict) -> None:
    parts = []
    for k, v in list(args.items())[:2]:
        v_str = f'"{v[:60]}"' if isinstance(v, str) else repr(v)[:60]
        parts.append(f"{k}={v_str}")
    console.print(
        f"  [bold {_AMBER}]⚡[/bold {_AMBER}]  "
        f"[{_AMBER}]{name}[/{_AMBER}]"
        f"  [dim]{', '.join(parts)}[/dim]"
    )


def _show_tool_done(name: str, elapsed: float) -> None:
    console.print(
        f"  [bold green]✓[/bold green]  "
        f"[dim]{name}  {elapsed:.1f}s[/dim]"
    )


def _show_thought(thought: str) -> None:
    console.print(f"  [bold {_VIOLET}]◆ thinking[/bold {_VIOLET}]")
    for line in thought.strip().splitlines():
        console.print(f"  [{_VIOLET}]│[/{_VIOLET}] [dim]{line}[/dim]")
    console.print()


# ── dangerous command guard ───────────────────────────────────────────────────
def _confirm_dangerous(command: str) -> bool:
    console.print(
        f"\n  [bold {_RED}]⚠  dangerous command[/bold {_RED}]\n"
        f"  [dim]{command[:200]}[/dim]\n"
    )
    try:
        ans = console.input(
            f"  [{_SILVER}]run it? (yes/no)[/{_SILVER}]  [{_GREEN}]›[/{_GREEN}]  "
        ).strip().lower()
        console.print()
        return ans in ("yes", "y")
    except (KeyboardInterrupt, EOFError):
        return False


# ── parallel tool runner ──────────────────────────────────────────────────────
def _run_tools(calls: list[tuple[str, dict, str]]) -> list[str]:
    """Execute tool calls. Runs in parallel when there are multiple."""
    n = len(calls)
    if n == 0:
        return []

    # Dangerous check upfront (serial, needs user input)
    vetted: list[bool] = []
    for name, args, _ in calls:
        if name == "run_shell" and _DANGEROUS_RE.search(args.get("command", "")):
            vetted.append(_confirm_dangerous(args.get("command", "")))
        else:
            vetted.append(True)

    # Separate think calls (display differently, always serial) from action calls
    think_indices = {i for i, (name, _, _) in enumerate(calls) if name == "think"}
    action_indices = [i for i in range(n) if i not in think_indices]

    # Show starts for action tools only
    for i in action_indices:
        name, args, _ = calls[i]
        if vetted[i]:
            _show_tool_call(name, args)

    results: list[str] = [None] * n

    def _run_one(idx: int, name: str, args: dict) -> tuple[int, str, float]:
        if not vetted[idx]:
            return idx, "Cancelled by user.", 0.0
        t0 = time.time()
        result = execute_tool(name, args)
        return idx, _truncate_result(result), time.time() - t0

    # Execute think calls first (silent, just display the thought)
    for i in think_indices:
        _, result, _ = _run_one(i, "think", calls[i][1])
        results[i] = result
        if _think_verbose():
            _show_thought(calls[i][1].get("thought", ""))

    # Execute action tools (parallel when multiple)
    if len(action_indices) == 1:
        i = action_indices[0]
        idx, result, elapsed = _run_one(i, calls[i][0], calls[i][1])
        _show_tool_done(calls[i][0], elapsed)
        results[i] = result
    elif action_indices:
        with ThreadPoolExecutor(max_workers=min(len(action_indices), 4)) as ex:
            futures = {
                ex.submit(_run_one, i, calls[i][0], calls[i][1]): i
                for i in action_indices
            }
            for fut in as_completed(futures):
                idx, result, elapsed = fut.result()
                _show_tool_done(calls[idx][0], elapsed)
                results[idx] = result

    return results


# ── slash commands ────────────────────────────────────────────────────────────
_HELP_TEXT = (
    f"  [bold {_GREEN}]/clear[/bold {_GREEN}]          wipe conversation history\n"
    f"  [bold {_GREEN}]/model[/bold {_GREEN}] [dim]<name>[/dim]    switch model mid-session\n"
    f"  [bold {_GREEN}]/tools[/bold {_GREEN}]          list available tools\n"
    f"  [bold {_GREEN}]/save[/bold {_GREEN}]  [dim][file][/dim]    save session to JSON\n"
    f"  [bold {_GREEN}]/cwd[/bold {_GREEN}]   [dim][path][/dim]    show or change working directory\n"
    f"  [bold {_GREEN}]/tokens[/bold {_GREEN}]         show context size\n"
    f"  [bold {_GREEN}]/think[/bold {_GREEN}]          toggle visible thinking on/off\n"
    f"  [bold {_GREEN}]/help[/bold {_GREEN}]           show this message"
)


def _handle_slash(raw: str, state: dict) -> None:
    parts = raw.strip().split(None, 1)
    name = parts[0].lower()
    arg  = parts[1].strip() if len(parts) > 1 else ""

    if name == "/clear":
        state["messages"][:] = [state["messages"][0]]
        console.print(f"  [dim]history cleared[/dim]\n")

    elif name == "/model":
        if not arg:
            console.print(f"  [dim]current model: {state['model']}[/dim]\n")
        else:
            state["model"] = arg
            console.print(f"  [dim]model → {arg}[/dim]\n")

    elif name == "/tools":
        console.print(f"\n  [dim]tools ({len(TOOL_DEFINITIONS)}):[/dim]")
        for t in TOOL_DEFINITIONS:
            n = t["function"]["name"]
            d = t["function"]["description"].split(".")[0][:60]
            console.print(f"  [{_AMBER}]{n:<18}[/{_AMBER}] [dim]{d}[/dim]")
        console.print()

    elif name == "/save":
        saved = _save_session(state["messages"], state["provider"], state["model"])
        if arg:
            dest = Path(arg)
            saved.rename(dest)
            saved = dest
        console.print(f"  [dim]saved → {saved}[/dim]\n")

    elif name == "/cwd":
        if not arg:
            console.print(f"  [dim]{get_cwd()}[/dim]\n")
        else:
            target = os.path.abspath(os.path.expanduser(arg))
            if os.path.isdir(target):
                set_cwd(target)
                console.print(f"  [dim]cwd → {target}[/dim]\n")
            else:
                console.print(f"  [bold {_RED}]not a directory: {arg}[/bold {_RED}]\n")

    elif name == "/tokens":
        n = _estimate_tokens(state["messages"])
        c = _context_chars(state["messages"])
        pct = int(c / _MAX_CONTEXT_CHARS * 100)
        console.print(
            f"  [dim]~{n:,} tokens  ·  {c:,} chars  ·  "
            f"{len(state['messages'])} messages  ·  {pct}% of limit[/dim]\n"
        )

    elif name == "/think":
        new_state = not _think_verbose()
        _think_set_verbose(new_state)
        status = f"[bold {_VIOLET}]on[/bold {_VIOLET}]" if new_state else "off"
        console.print(f"  [dim]thinking display {status}[/dim]\n")

    elif name == "/help":
        console.print(f"\n{_HELP_TEXT}\n")

    else:
        console.print(f"  [dim]unknown command: {name} — try /help[/dim]\n")


# ── main chat loop ─────────────────────────────────────────────────────────────
def chat(client, model: str, provider: str, resume: bool = False, cwd: str = None):
    if cwd:
        set_cwd(os.path.abspath(cwd))

    system_msg = {"role": "system", "content": SYSTEM_PROMPT}
    messages = [system_msg]

    if resume:
        session = _load_last_session()
        if session:
            messages = session.get("messages", [system_msg])
            console.print(f"  [dim]resumed · {len(messages)} messages[/dim]")
        else:
            console.print(f"  [dim]no saved session found[/dim]")

    state = {"messages": messages, "model": model, "provider": provider}

    console.print()
    console.print(_BANNER)
    console.print()
    console.print(
        f"  [dim]{provider}  ·  {model}  ·  "
        f"cwd: {get_cwd()}  ·  /help  ·  ctrl+c to quit[/dim]"
    )
    console.print(Rule(style=f"dim {_GREEN}"))
    console.print()

    while True:
        try:
            user_input = console.input(
                f"  [{_SILVER}]you[/{_SILVER}]  [{_GREEN}]›[/{_GREEN}]  "
            ).strip()
        except (KeyboardInterrupt, EOFError):
            saved = _save_session(state["messages"], provider, state["model"])
            console.print(f"\n  [dim]session saved → {saved}[/dim]\n  [dim]goodbye[/dim]\n")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "bye", ":q"):
            saved = _save_session(state["messages"], provider, state["model"])
            console.print(f"\n  [dim]session saved → {saved}[/dim]\n  [dim]goodbye[/dim]\n")
            break

        if user_input.startswith("/"):
            _handle_slash(user_input, state)
            continue

        state["messages"].append({"role": "user", "content": user_input})
        console.print()

        # Agentic loop — capped at _MAX_ITERATIONS tool-call rounds
        for _iter in range(_MAX_ITERATIONS):
            state["messages"] = _prune_context(state["messages"])
            text, tool_calls = _stream(client, state["model"], state["messages"])

            if tool_calls:
                state["messages"].append({
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": tool_calls,
                })

                calls: list[tuple[str, dict, str]] = []
                for tc in tool_calls:
                    name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        args = {}
                    calls.append((name, args, tc["id"]))

                results = _run_tools(calls)

                for (name, args, tc_id), result in zip(calls, results):
                    state["messages"].append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": result,
                    })

                console.print()

                if _iter == _MAX_ITERATIONS - 1:
                    console.print(
                        f"  [bold {_RED}]⚠  hit {_MAX_ITERATIONS}-iteration cap[/bold {_RED}]\n"
                    )
                    break

            else:
                if text:
                    state["messages"].append({"role": "assistant", "content": text})
                n_tok = _estimate_tokens(state["messages"])
                console.print(f"  [dim]~{n_tok:,} tokens in context[/dim]")
                console.print(Rule(style=f"dim {_GREEN}"))
                console.print()
                break


# ── provider listing ───────────────────────────────────────────────────────────
def _list_providers():
    console.print()
    console.print(Rule(f"[bold {_GREEN}]providers[/bold {_GREEN}]", style=f"dim {_GREEN}"))
    console.print()
    for name, cfg in PROVIDERS.items():
        key_info = cfg.get("api_key_env", "no key needed")
        console.print(f"  [{_GREEN}]{name:<14}[/{_GREEN}] {cfg['description']}")
        console.print(f"  {'':<14} model:  [dim]{cfg['default_model']}[/dim]")
        console.print(f"  {'':<14} key:    [dim]{key_info}[/dim]\n")


# ── entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        prog="viper",
        description="Viper — minimal multi-provider terminal agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python agent.py
  python agent.py --provider openrouter --model mistralai/mistral-7b-instruct:free
  python agent.py --provider anthropic --model anthropic/claude-3.5-haiku
  python agent.py --provider ollama --model llama3.2
  python agent.py --resume
  python agent.py --cwd /path/to/project
  python agent.py --list-providers
        """,
    )
    parser.add_argument("--provider", "-p", default="openrouter", choices=list(PROVIDERS))
    parser.add_argument("--model", "-m", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--resume", "-r", action="store_true", help="Resume last saved session")
    parser.add_argument("--cwd", default=None, help="Set working directory at startup")
    parser.add_argument("--list-providers", "-l", action="store_true")
    args = parser.parse_args()

    if args.list_providers:
        _list_providers()
        return

    try:
        client, config = get_client(args.provider, args.api_key)
    except ValueError as e:
        console.print(f"\n  [bold {_RED}]error:[/bold {_RED}] {e}\n")
        sys.exit(1)

    model = args.model or config["default_model"]

    try:
        chat(client, model, args.provider, resume=args.resume, cwd=args.cwd)
    except KeyboardInterrupt:
        console.print(f"\n  [dim]goodbye[/dim]\n")


if __name__ == "__main__":
    main()
