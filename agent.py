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

try:
    from cryptography.fernet import Fernet as _Fernet
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.text import Text

from providers import PROVIDERS, get_client
from system_prompt import SYSTEM_PROMPT
from tools import TOOL_DEFINITIONS, execute_tool
from tools.state import get_cwd, set_cwd
from tools.think import is_verbose as _think_verbose, set_verbose as _think_set_verbose
from tools.guardrails import EXTERNAL_TOOLS, detect_pii, detect_injection, tag_external, scan_python_code

console = Console(highlight=False)

# ── theme ─────────────────────────────────────────────────────────────────────
_GREEN  = "#39ff14"
_SILVER = "#94a3b8"
_AMBER  = "#fbbf24"
_RED    = "#ef4444"
_VIOLET = "#a78bfa"
_BLUE   = "#60a5fa"
_CYAN   = "#22d3ee"
_ORANGE = "#fb923c"

# ── tool icons ────────────────────────────────────────────────────────────────
_TOOL_ICONS: dict[str, str] = {
    "think":                        "🧠",
    "web_search":                   "🔍",
    "browser":                      "🌐",
    "run_shell":                    "🖥 ",
    "run_python":                   "🐍",
    "http_fetch":                   "📡",
    "read_file":                    "📄",
    "write_file":                   "✏ ",
    "edit_file":                    "🔧",
    "list_dir":                     "📂",
    "calculate":                    "🧮",
    "github_set_token":             "🔑",
    "github_get_repo":              "🐙",
    "github_list_issues":           "🐙",
    "github_get_issue":             "🐙",
    "github_comment_issue":         "💬",
    "github_get_file":              "🐙",
    "github_create_or_update_file": "🐙",
    "github_list_prs":              "🔀",
    "github_create_pr":             "🔀",
    "github_clone_repo":            "📦",
}
_DEFAULT_ICON = "⚙ "

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
    r"\brm\s+-[rRfF]"           # rm -rf / rm -fr
    r"|\brm\b.*\*"              # rm with wildcard
    r"|rmdir\s+/[sqSQ]"         # rmdir /s /q
    r"|\brmdir\b"               # plain rmdir (always destructive)
    r"|del\s+/[fFqQ]"           # del /f /q
    r"|\bdel\b.*\*"             # del with wildcard
    r"|\bRemove-Item\b"         # PowerShell Remove-Item
    r"|\bri\b.*-[rR]"           # ri alias with -Recurse
    r"|\bformat\s+[a-zA-Z]:"
    r"|\bdd\s+if="
    r"|DROP\s+(TABLE|DATABASE)"
    r"|mkfs\."
    r"|\b(poweroff|reboot|shutdown)\b",
    re.IGNORECASE,
)

# Deeper shell patterns — obfuscated execution, reverse shells, SSRF via shell
_DEEP_DANGEROUS_RE = re.compile(
    r"(?:curl|wget)\b[^|\n]{0,200}\|\s*(?:ba|z|da|fi|k)?sh\b"   # curl/wget | bash
    r"|bash\s+<\s*\(\s*(?:curl|wget)"                            # bash <(curl ...)
    r"|base64\b.*-d.*\|\s*(?:ba|z)?sh"                          # base64 -d | bash
    r"|\bnc\b.*-e\s+/(?:bin|usr)"                               # netcat reverse shell
    r"|:\(\)\s*\{\s*:\|:&\s*\}"                                 # fork bomb
    r"|(?:curl|wget)\b.*\$\{?\w*(?:KEY|TOKEN|SECRET|PASS)\w*\}?"  # env-var exfil via curl
    r"|>\s*/(?:etc|bin|sbin|usr/bin)/"                          # write to system dirs
    r"|Invoke-Expression\s*\(.*(?:Net\.WebClient|DownloadString)"  # PS download+exec
    r"|iex\s*\(.*(?:Net\.WebClient|DownloadString)",        # iex shorthand
    re.IGNORECASE,
)

# ── session rate limiter ───────────────────────────────────────────────────────
_session_tool_calls: int = 0
_MAX_SESSION_TOOL_CALLS: int = 200

# ── session storage ───────────────────────────────────────────────────────────
_SESSIONS_DIR = Path.home() / ".viper" / "sessions"
_KEY_PATH     = Path.home() / ".viper" / ".key"


def _get_or_create_key() -> bytes:
    """Return the Fernet encryption key for session files, generating it on first use."""
    if _KEY_PATH.exists():
        return _KEY_PATH.read_bytes()
    key = _Fernet.generate_key()
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _KEY_PATH.write_bytes(key)
    try:
        import stat as _stat
        _KEY_PATH.chmod(_stat.S_IRUSR | _stat.S_IWUSR)  # owner-only on Unix
    except Exception:
        pass
    return key


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
    payload = json.dumps(
        {"provider": provider, "model": model, "messages": messages}, indent=2
    ).encode()
    if _CRYPTO_OK:
        path = _SESSIONS_DIR / f"{ts}.viper"
        path.write_bytes(_Fernet(_get_or_create_key()).encrypt(payload))
    else:
        path = _SESSIONS_DIR / f"{ts}.json"
        path.write_bytes(payload)
    return path


def _load_last_session() -> dict | None:
    if not _SESSIONS_DIR.exists():
        return None
    # Prefer encrypted sessions; fall back to legacy plain JSON
    encrypted = sorted(_SESSIONS_DIR.glob("*.viper"))
    if encrypted and _CRYPTO_OK:
        data = _Fernet(_get_or_create_key()).decrypt(encrypted[-1].read_bytes())
        return json.loads(data.decode())
    plain = sorted(_SESSIONS_DIR.glob("*.json"))
    if plain:
        return json.loads(plain[-1].read_text(encoding="utf-8"))
    return None


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
def _spinner_text(label: str) -> Text:
    t = Text()
    t.append("  ")
    t.append(Spinner("dots", style=f"bold {_CYAN}").render(time.time()))
    t.append(f"  {label}", style=f"dim {_SILVER}")
    return t


def _stream(client, model: str, messages: list, use_tools: bool = True):
    """Stream API response. Returns (text, tool_calls)."""
    full_text = ""
    tc_map: dict[int, dict] = {}

    kwargs: dict = {"model": model, "messages": messages, "stream": True}
    if use_tools:
        kwargs["tools"] = TOOL_DEFINITIONS

    # Phase 1: spinner while waiting for first byte
    waiting_spinner = Spinner("dots", text=Text(f"  thinking…", style=f"dim {_SILVER}"), style=f"bold {_CYAN}")
    live = Live(waiting_spinner, console=console, refresh_per_second=20)
    live.start()

    got_first = False
    streaming_live = False

    try:
        for chunk in client.chat.completions.create(**kwargs):
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta.content:
                full_text += delta.content
                if not got_first:
                    # Switch from spinner to streaming panel
                    live.update(_panel(full_text))
                    got_first = True
                    streaming_live = True
                else:
                    live.update(_panel(full_text))

            if delta.tool_calls:
                if not got_first:
                    got_first = True  # tool-only response, stop spinner
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
        live.stop()
        msg = str(e).lower()
        if use_tools and any(w in msg for w in ("tool", "function", "unsupported")):
            _show_retry("without tools (provider doesn't support tool-calling)")
            return _stream(client, model, messages, use_tools=False)
        console.print(f"\n  [bold {_RED}]error:[/bold {_RED}] {e}\n")
        return "", []

    live.stop()
    if streaming_live:
        console.print()

    return full_text, [tc_map[i] for i in sorted(tc_map)]


# ── tool display ──────────────────────────────────────────────────────────────
def _tool_icon(name: str) -> str:
    return _TOOL_ICONS.get(name, _DEFAULT_ICON)


def _show_tool_call(name: str, args: dict) -> None:
    icon = _tool_icon(name)
    parts = []
    for k, v in list(args.items())[:2]:
        v_str = f'"{v[:55]}"' if isinstance(v, str) else repr(v)[:55]
        parts.append(f"[dim]{k}=[/dim][{_SILVER}]{v_str}[/{_SILVER}]")
    hint = ("  " + "  ".join(parts)) if parts else ""
    console.print(
        f"  {icon}  [bold {_AMBER}]{name}[/bold {_AMBER}]{hint}"
    )


def _show_tool_done(name: str, elapsed: float) -> None:
    icon = _tool_icon(name)
    console.print(
        f"  {icon}  [bold {_GREEN}]done[/bold {_GREEN}]  "
        f"[dim]{name}  [{elapsed:.2f}s][/dim]"
    )


def _show_tool_error(name: str, elapsed: float) -> None:
    icon = _tool_icon(name)
    console.print(
        f"  {icon}  [bold {_RED}]error[/bold {_RED}]  "
        f"[dim]{name}  [{elapsed:.2f}s][/dim]"
    )


def _show_thought(thought: str) -> None:
    lines = thought.strip().splitlines()
    console.print(f"  🧠  [bold {_VIOLET}]thinking[/bold {_VIOLET}]")
    for line in lines:
        console.print(f"     [{_VIOLET}]│[/{_VIOLET}] [dim]{line}[/dim]")
    console.print(f"     [{_VIOLET}]╰{'─' * 40}[/{_VIOLET}]")
    console.print()


def _show_acting(iteration: int) -> None:
    console.print(f"\n  [dim {_CYAN}]── acting · round {iteration} {'─' * 30}[/dim {_CYAN}]")


def _show_retry(reason: str) -> None:
    console.print(f"  🔄  [dim {_ORANGE}]retrying  {reason}[/dim {_ORANGE}]")


def _show_pii_found(source: str, types: list[str]) -> None:
    console.print(
        f"  🔒  [bold {_AMBER}]pii detected[/bold {_AMBER}]  "
        f"[dim]{source}  ·  {', '.join(types)}[/dim]"
    )


# ── dangerous command guard ───────────────────────────────────────────────────
def _confirm_python(code: str, dangers: list[str]) -> bool:
    preview = code[:300].replace("\n", " ↵ ")
    console.print(
        f"\n  🐍  [bold {_RED}]dangerous python patterns[/bold {_RED}]\n"
        f"  [dim {_RED}]{'─' * 50}[/dim {_RED}]\n"
        f"  [dim]{'; '.join(dangers)}[/dim]\n"
        f"  [dim]{preview}[/dim]\n"
    )
    try:
        ans = console.input(
            f"  [{_SILVER}]execute anyway? (yes/no)[/{_SILVER}]  [{_GREEN}]›[/{_GREEN}]  "
        ).strip().lower()
        console.print()
        return ans in ("yes", "y")
    except (KeyboardInterrupt, EOFError):
        return False
def _confirm_dangerous(command: str) -> bool:
    console.print(
        f"\n  🚨  [bold {_RED}]dangerous command[/bold {_RED}]\n"
        f"  [dim {_RED}]{'─' * 50}[/dim {_RED}]\n"
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

    # ── rate limiter ──────────────────────────────────────────────────────────
    global _session_tool_calls
    action_count = sum(1 for name, _, _ in calls if name != "think")
    _session_tool_calls += action_count
    if _session_tool_calls > _MAX_SESSION_TOOL_CALLS:
        console.print(
            f"\n  ⛔  [bold {_RED}]rate limit — {_MAX_SESSION_TOOL_CALLS} tool calls per session exceeded[/bold {_RED}]\n"
        )
        return [f"[BLOCKED — session rate limit of {_MAX_SESSION_TOOL_CALLS} tool calls exceeded]"] * len(calls)

    # ── pre-execution vetting (serial — needs user input) ─────────────────────
    vetted: list[bool] = []
    for name, args, _ in calls:
        if name == "run_shell":
            cmd = args.get("command", "")
            if _DANGEROUS_RE.search(cmd) or _DEEP_DANGEROUS_RE.search(cmd):
                vetted.append(_confirm_dangerous(cmd))
            else:
                vetted.append(True)
        elif name == "run_python":
            dangers = scan_python_code(args.get("code", ""))
            if dangers:
                vetted.append(_confirm_python(args.get("code", ""), dangers))
            else:
                vetted.append(True)
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

    def _run_one_with_spinner(idx: int, name: str, args: dict) -> tuple[int, str, float]:
        """Run a single tool while showing an inline spinner."""
        if not vetted[idx]:
            return idx, "Cancelled by user.", 0.0
        icon = _tool_icon(name)
        spinner = Spinner("dots", style=f"bold {_CYAN}")
        label = Text(f"  {icon}  {name}  ", style=f"{_AMBER}")
        label.append("running…", style=f"dim {_SILVER}")

        def _renderable():
            s = spinner.render(time.time())
            row = Text()
            row.append("  ")
            row.append_text(Text.assemble(s, " "))
            row.append_text(label)
            return row

        t0 = time.time()
        with Live(_renderable(), console=console, refresh_per_second=20, transient=True) as live:
            import threading
            done = threading.Event()

            def _worker():
                nonlocal _result
                _result = execute_tool(name, args)
                done.set()

            _result = ""
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            while not done.wait(timeout=0.05):
                live.update(_renderable())
            t.join()

        elapsed = time.time() - t0
        return idx, _truncate_result(_result), elapsed

    # Execute think calls first (silent, just display the thought)
    for i in think_indices:
        _, result, _ = _run_one(i, "think", calls[i][1])
        results[i] = result
        if _think_verbose():
            _show_thought(calls[i][1].get("thought", ""))

    # Execute action tools — spinner for single, parallel for multiple
    if len(action_indices) == 1:
        i = action_indices[0]
        idx, result, elapsed = _run_one_with_spinner(i, calls[i][0], calls[i][1])
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

    # ── security guardrails ────────────────────────────────────────────────────
    for i, (name, _args, _tid) in enumerate(calls):
        result = results[i]
        if result is None or name in ("think", "calculate"):
            continue

        # 1. Warn about PII in tool output (informational; model still sees it)
        pii = detect_pii(result)
        if pii.has_pii:
            _show_pii_found(name, pii.found)

        # 2. Detect prompt-injection in tool output; ask user before injecting
        #    adversarial content into the model's context.
        inj = detect_injection(result)
        if inj.suspicious:
            console.print(
                f"\n  🚨  [bold {_RED}]prompt injection detected — [{name}] output[/bold {_RED}]"
            )
            for cat, snippet in inj.matches[:3]:
                console.print(f"  [dim {_RED}]  [{cat}]: {snippet!r}[/dim {_RED}]")
            try:
                ans = console.input(
                    f"  [{_SILVER}]include in context? (yes/no)[/{_SILVER}]  [{_GREEN}]›[/{_GREEN}]  "
                ).strip().lower()
                console.print()
                if ans not in ("yes", "y"):
                    result = f"[BLOCKED — prompt injection detected in {name} output, content withheld]"
            except (EOFError, KeyboardInterrupt):
                result = f"[BLOCKED — prompt injection detected in {name} output, content withheld]"
                console.print()

        # 3. Tag all external tool output so the model knows it is untrusted
        if name in EXTERNAL_TOOLS:
            result = tag_external(name, result)

        results[i] = result

    return results


# ── multi-agent orchestration ─────────────────────────────────────────────────
def _run_multi_agent(request: str, state: dict) -> None:
    """Invoke sublayer orchestration: plan → parallel workers → aggregate."""
    from orchestrator import OrchestratorAgent  # lazy import keeps startup fast

    client = get_client(state["provider"])[0]
    orch   = OrchestratorAgent(client, state["model"], max_retries=2, max_workers=4)

    console.print(f"\n  🧩  [bold {_VIOLET}]orchestrating[/bold {_VIOLET}]  [dim]{request[:60]}[/dim]\n")

    def _on_plan(tasks):
        console.print(f"  📋  [dim]{len(tasks)} agents planned:[/dim]")
        for t in tasks:
            needs = "tools" if t.tools else "no tools"
            model_hint = t.model.split("/")[-1] if t.model else "?"
            console.print(
                f"       [{_AMBER}]{t.id}[/{_AMBER}]  {t.role}"
                f"  [dim]{model_hint}  ·  {needs}[/dim]"
            )
        console.print()

    def _on_result(result):
        icon  = "✅" if result.success else "❌"
        extra = f"  [dim](attempt {result.attempts})[/dim]" if result.attempts > 1 else ""
        console.print(
            f"  {icon}  [{_GREEN if result.success else _RED}]{result.role}[/]"
            f"  [dim]{result.task_id}[/dim]{extra}"
        )

    try:
        answer = orch.execute(request, on_plan=_on_plan, on_result=_on_result)
    except Exception as exc:
        console.print(f"\n  [bold {_RED}]orchestration error:[/bold {_RED}] {exc}\n")
        return

    console.print()
    console.print(_panel(answer))
    console.print()

    # Inject the final answer into the main conversation so context is preserved
    state["messages"].append({"role": "user",      "content": f"[/multi] {request}"})
    state["messages"].append({"role": "assistant",  "content": answer})


# ── slash commands ────────────────────────────────────────────────────────────
_HELP_TEXT = (
    f"  [bold {_GREEN}]/clear[/bold {_GREEN}]          wipe conversation history\n"
    f"  [bold {_GREEN}]/model[/bold {_GREEN}] [dim]<name>[/dim]    switch model mid-session\n"
    f"  [bold {_GREEN}]/tools[/bold {_GREEN}]          list available tools\n"
    f"  [bold {_GREEN}]/save[/bold {_GREEN}]  [dim][file][/dim]    save session to JSON\n"
    f"  [bold {_GREEN}]/cwd[/bold {_GREEN}]   [dim][path][/dim]    show or change working directory\n"
    f"  [bold {_GREEN}]/tokens[/bold {_GREEN}]         show context size\n"
    f"  [bold {_GREEN}]/think[/bold {_GREEN}]          toggle visible thinking on/off\n"
    f"  [bold {_GREEN}]/multi[/bold {_GREEN}] [dim]<request>[/dim] run multi-agent orchestration\n"
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

    elif name == "/multi":
        if not arg:
            console.print(f"  [dim]usage: /multi <request>[/dim]\n")
            return
        _run_multi_agent(arg, state)

    elif name == "/help":
        console.print(f"\n{_HELP_TEXT}\n")

    else:
        console.print(f"  [dim]unknown command: {name} — try /help[/dim]\n")


# ── main chat loop ─────────────────────────────────────────────────────────────
def chat(client, model: str, provider: str, resume: bool = False, cwd: str = None):
    if cwd:
        set_cwd(os.path.abspath(cwd))

    # Give spawn_agents tool access to the live client + model
    from tools.multiagent import init as _init_multiagent
    _init_multiagent(client, model)

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

        # ── PII guard on user input ────────────────────────────────────────────
        pii = detect_pii(user_input)
        if pii.has_pii:
            console.print(
                f"\n  ⚠️   [bold {_AMBER}]PII detected in your message[/bold {_AMBER}]  "
                f"[dim]{', '.join(pii.found)}[/dim]\n"
                f"  [dim]This will be sent to the LLM provider ({provider}). Proceed?[/dim]"
            )
            try:
                ans = console.input(
                    f"  [{_SILVER}](yes/no)[/{_SILVER}]  [{_GREEN}]›[/{_GREEN}]  "
                ).strip().lower()
                console.print()
                if ans not in ("yes", "y"):
                    console.print(f"  [dim]message cancelled[/dim]\n")
                    continue
            except (KeyboardInterrupt, EOFError):
                console.print()
                continue

        state["messages"].append({"role": "user", "content": user_input})
        console.print()

        # Agentic loop — capped at _MAX_ITERATIONS tool-call rounds
        for _iter in range(_MAX_ITERATIONS):
            state["messages"] = _prune_context(state["messages"])
            if _iter > 0:
                _show_acting(_iter + 1)
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
                        f"\n  ⛔  [bold {_RED}]hit {_MAX_ITERATIONS}-round cap — stopping[/bold {_RED}]\n"
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
