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
from rich.markup import escape as _escape
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.text import Text

import auth
import onboarding
import pickers
from auth import get_or_create_key as _get_or_create_key
from providers import PROVIDERS, get_client
from system_prompt import build_system_prompt
from tools import TOOL_DEFINITIONS, execute_tool
from tools.state import get_cwd, set_cwd
from tools.think import is_verbose as _think_verbose, set_verbose as _think_set_verbose
from tools.guardrails import (
    EXTERNAL_TOOLS, detect_pii, detect_injection, tag_external, scan_python_code,
    is_env_file, env_path_key, grant_env_access, take_env_grant,
)
from tools.attachments import (
    AttachmentError, build_message, find_references, load_attachment, resolve_path,
)

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
    "save_memory":                  "💾",
    "list_memories":                "💾",
    "delete_memory":                "💾",
    "search_memory":                "💾",
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
    "use_skill":                    "📘",
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
_MAX_ITERATIONS        = 50       # tool-call rounds per user turn

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

# .env files the user has refused this session — the model isn't asked about them again
_env_denied: set[str] = set()

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


def _read_session_file(path: Path) -> dict | None:
    """One saved session, or None if it cannot be read (wrong key, damaged file, no cryptography)."""
    try:
        raw = path.read_bytes()
        if path.suffix == ".viper":
            if not _CRYPTO_OK:
                return None
            raw = _Fernet(_get_or_create_key()).decrypt(raw)
        data = json.loads(raw.decode())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _session_time(stem: str) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.strptime(stem[:15], "%Y%m%d_%H%M%S"))
    except ValueError:
        return stem


def _list_sessions(limit: int = 30) -> list[dict]:
    """Saved sessions, newest first: path, when, provider, model, message count, first-question preview."""
    if not _SESSIONS_DIR.exists():
        return []
    files = sorted(
        [*_SESSIONS_DIR.glob("*.viper"), *_SESSIONS_DIR.glob("*.json")],
        key=lambda p: p.stem, reverse=True,
    )
    out: list[dict] = []
    for path in files:
        if len(out) >= limit:
            break
        data = _read_session_file(path)
        msgs = (data or {}).get("messages") or []
        convo = [m for m in msgs if m.get("role") in ("user", "assistant")]
        if not convo:
            continue   # unreadable, or nothing was said in it
        first = next(
            (str(m.get("content") or "") for m in convo
             if m["role"] == "user" and not str(m.get("content") or "").startswith("[/")),
            "",
        )
        out.append({
            "path": path,
            "when": _session_time(path.stem),
            "provider": data.get("provider"),
            "model": data.get("model"),
            "count": len(convo),
            "preview": " ".join(first.split())[:70],
        })
    return out


def _load_into_state(state: dict, session: dict) -> bool:
    """Replace the current chat with a saved session. The chat you are leaving is saved first."""
    data = _read_session_file(session["path"])
    if data is None:
        console.print(f"  [bold {_RED}]could not read that session (damaged, or saved with another key)[/bold {_RED}]\n")
        return False
    if any(m.get("role") != "system" for m in state["messages"]):
        left = _save_session(state["messages"], state["provider"], state["model"])
        console.print(f"  [dim]current chat saved → {left}[/dim]")
    system_msg = {"role": "system", "content": build_system_prompt()}
    messages = data.get("messages") or [system_msg]
    if messages[0].get("role") == "system":
        messages[0] = system_msg     # a saved session carries the old prompt
    else:
        messages.insert(0, system_msg)
    state["messages"] = messages
    console.print(f"  [dim]loaded {session['when']} · {session['count']} messages[/dim]")
    if (data.get("provider"), data.get("model")) != (state["provider"], state["model"]):
        console.print(
            f"  [dim]it was started with {data.get('provider')} · {data.get('model')}. "
            f"still using {state['provider']} · {state['model']} (change with /model-select)[/dim]"
        )
    console.print()
    return True


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
def _confirm_env_access(path: str) -> bool:
    """Human-in-the-loop gate for reading a .env file. Creates a one-time grant on approval."""
    key = env_path_key(path)
    if key in _env_denied:
        return False
    console.print(
        f"\n  🔑  [bold {_AMBER}]the agent wants to read a secrets file[/bold {_AMBER}]\n"
        f"  [dim {_AMBER}]{'─' * 50}[/dim {_AMBER}]\n"
        f"  [dim]{_escape(str(path)[:200])}[/dim]\n"
        f"  [dim]yes    = full contents (secret values are sent to the LLM provider)[/dim]\n"
        f"  [dim]masked = key names only, values hidden[/dim]\n"
        f"  [dim]no     = refuse[/dim]\n"
    )
    try:
        ans = console.input(
            f"  [{_SILVER}]allow? (yes/masked/no)[/{_SILVER}]  [{_GREEN}]›[/{_GREEN}]  "
        ).strip().lower()
        console.print()
    except (KeyboardInterrupt, EOFError):
        ans = ""
    if ans in ("yes", "y"):
        grant_env_access(path, "full")
        return True
    if ans in ("masked", "mask", "m"):
        grant_env_access(path, "masked")
        return True
    _env_denied.add(key)
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


# ── attachments ───────────────────────────────────────────────────────────────
def _ask_yes_no(question: str) -> bool:
    try:
        ans = console.input(
            f"  [{_SILVER}]{question} (yes/no)[/{_SILVER}]  [{_GREEN}]›[/{_GREEN}]  "
        ).strip().lower()
        console.print()
        return ans in ("yes", "y")
    except (KeyboardInterrupt, EOFError):
        console.print()
        return False


def _prepare_message(user_input: str, state: dict) -> str:
    """Build the user message, attaching `@path` files and anything staged with /attach.

    Every file goes through the same guards as other external content: the .env approval,
    a PII warning (the file is sent to the provider), and the injection scan. A file the user
    declines, or that can't be read, is left out — and the model is told, so it can't pretend
    to have seen it.
    """
    staged = [Path(p) for p in state.pop("staged_attachments", [])]
    refs, warnings = find_references(user_input, [get_cwd(), os.getcwd()])
    for w in warnings:
        console.print(f"  [{_AMBER}]{_escape(w)}[/{_AMBER}]")
    paths = list(dict.fromkeys([*staged, *refs]))
    if not paths:
        return user_input

    loaded, skipped = [], []
    suppressed = state.get("pii_suppressed", set())
    provider = state.get("provider", "the LLM provider")

    for p in paths:
        label = _escape(p.name)
        env = is_env_file(p)
        if env and not _confirm_env_access(str(p)):
            skipped.append(f"{p.name} (the user declined to share it)")
            console.print(f"  📎  [bold {_AMBER}]{label}[/bold {_AMBER}]  [dim]not attached — you declined[/dim]")
            continue
        try:
            att = load_attachment(p)
        except AttachmentError as e:
            if env:
                take_env_grant(str(p))          # don't leave an unused approval behind
            skipped.append(f"{p.name} ({e})")
            console.print(f"  📎  [bold {_RED}]{label}[/bold {_RED}]  [dim]not attached — {_escape(str(e))}[/dim]")
            continue

        if not env:                              # a .env was already approved at its own prompt
            pii = [t for t in detect_pii(att.text).found if t not in suppressed]
            if pii:
                console.print(
                    f"\n  ⚠️   [bold {_AMBER}]{label} contains PII[/bold {_AMBER}]  [dim]{', '.join(pii)}[/dim]\n"
                    f"  [dim]its contents will be sent to the LLM provider ({provider}).[/dim]\n"
                    f"  [dim](use /pii allow {pii[0]} to stop asking this session)[/dim]"
                )
                if not _ask_yes_no("send it anyway?"):
                    skipped.append(f"{p.name} (the user declined: it contains {', '.join(pii)})")
                    continue

        inj = detect_injection(att.text)
        if inj.suspicious:
            console.print(f"\n  🚨  [bold {_RED}]prompt injection detected — {label}[/bold {_RED}]")
            for cat, snippet in inj.matches[:3]:
                console.print(f"  [dim {_RED}]  [{_escape(cat)}]: {_escape(repr(snippet))}[/dim {_RED}]")
            if not _ask_yes_no("include in context?"):
                skipped.append(f"{p.name} (the user withheld it: possible prompt injection)")
                continue

        loaded.append(att)
        cut = f"  ·  {att.omitted_chars:,} chars cut" if att.omitted_chars else ""
        console.print(
            f"  📎  [{_GREEN}]{label}[/{_GREEN}]  [dim]{att.kind}  ·  {len(att.text):,} chars{cut}[/dim]"
        )
    return build_message(user_input, loaded, skipped)


# ── parallel tool runner ──────────────────────────────────────────────────────
def _run_tools(calls: list[tuple[str, dict, str]], state: dict | None = None) -> list[str]:
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
        elif name == "read_file" and is_env_file(args.get("path", "")):
            vetted.append(_confirm_env_access(args["path"]))
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

    # Execute action tools — streaming for single local shell, spinner for single other,
    # parallel executor for multiple
    if len(action_indices) == 1:
        i = action_indices[0]
        name, args, _ = calls[i]
        if vetted[i] and name == "run_shell" and not args.get("sandbox", True):
            # Live streaming display for long-running local shell commands
            import tools.shell as _shell_mod
            from rich.markup import escape as _resc
            t0 = time.time()
            console.print()
            def _shell_line_cb(line: str) -> None:
                console.print(f"  [dim {_SILVER}]{_resc(line)}[/dim {_SILVER}]")
            _shell_mod._stream_cb = _shell_line_cb
            try:
                result = execute_tool(name, args)
            finally:
                _shell_mod._stream_cb = None
            elapsed = time.time() - t0
            _show_tool_done(name, elapsed)
            results[i] = _truncate_result(result)
        else:
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
        suppressed_types: set = (state or {}).get("pii_suppressed", set())
        active_pii = [t for t in pii.found if t not in suppressed_types]
        if active_pii:
            _show_pii_found(name, active_pii)

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

        # 4. If a memory was saved/deleted, refresh system prompt so the new fact
        #    is visible to the model in the same session without a restart.
        if name in ("save_memory", "delete_memory") and state:
            state["messages"][0]["content"] = build_system_prompt()

        results[i] = result

    return results


# ── multi-agent orchestration ─────────────────────────────────────────────────
def _run_multi_agent(request: str, state: dict) -> None:
    """Invoke sublayer orchestration: plan → parallel workers → aggregate."""
    from orchestrator import OrchestratorAgent  # lazy import keeps startup fast

    client = state["client"]
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
    f"  [bold {_GREEN}]/sessions[/bold {_GREEN}]       pick a saved session and load it\n"
    f"  [bold {_GREEN}]/model-select[/bold {_GREEN}]   pick a provider, then a model from its list\n"
    f"  [bold {_GREEN}]/model[/bold {_GREEN}] [dim]<name>[/dim]    switch model by name on the current provider\n"
    f"  [bold {_GREEN}]/apikey[/bold {_GREEN}]         pick a provider, then add, replace or remove its key\n"
    f"  [bold {_GREEN}]/tools[/bold {_GREEN}]          list available tools\n"
    f"  [bold {_GREEN}]/skills[/bold {_GREEN}]         list skills  ·  [bold {_GREEN}]/skill[/bold {_GREEN}] [dim]<name>[/dim] load one into this chat\n"
    f"  [bold {_GREEN}]/save[/bold {_GREEN}]  [dim][file][/dim]    save session to JSON\n"
    f"  [bold {_GREEN}]/cwd[/bold {_GREEN}]   [dim][path][/dim]    show or change working directory\n"
    f"  [bold {_GREEN}]/tokens[/bold {_GREEN}]         show context size\n"
    f"  [bold {_GREEN}]/think[/bold {_GREEN}]          toggle visible thinking on/off\n"
    f"  [bold {_GREEN}]/multi[/bold {_GREEN}] [dim]<request>[/dim] run multi-agent orchestration\n"
    f"  [bold {_GREEN}]/pii[/bold {_GREEN}]   [dim][allow|block|reset] <Type>[/dim] manage PII warning suppression\n"
    f"  [bold {_GREEN}]/attach[/bold {_GREEN}] [dim]<path>[/dim]     attach a file to your next message (or type @path inline)\n"
    f"  [bold {_GREEN}]/detach[/bold {_GREEN}]         clear files staged with /attach\n"
    f"  [bold {_GREEN}]/memory[/bold {_GREEN}]         list saved memories  ·  [bold {_GREEN}]/memory forget[/bold {_GREEN}] [dim]<key>[/dim] remove one  ·  [bold {_GREEN}]/memory clear[/bold {_GREEN}] wipe all\n"
    f"  [bold {_GREEN}]/e2b[/bold {_GREEN}]   [dim]<key>[/dim]      use your own E2B key for the sandbox\n"
    f"  [bold {_GREEN}]/help[/bold {_GREEN}]           show this message"
)


def _set_model(state: dict, model: str) -> None:
    """Change the model on the current provider."""
    from tools.multiagent import init as _init_multiagent
    state["model"] = model
    _init_multiagent(state["client"], model)
    auth.remember_choice(state["provider"], model)


def _switch_provider(state: dict, provider: str, model: str, api_key: str | None) -> None:
    """Build a client for another provider (or the same one with a new key). Raises ValueError if it can't."""
    from tools.multiagent import init as _init_multiagent
    client, _cfg = get_client(provider, api_key)
    state.update(client=client, provider=provider, model=model)
    _init_multiagent(client, model)
    auth.remember_choice(provider, model)


def _need_terminal(what: str) -> bool:
    if pickers.interactive():
        return True
    console.print(f"  [dim]{what} needs an interactive terminal[/dim]\n")
    return False


def _handle_slash(raw: str, state: dict) -> None:
    parts = raw.strip().split(None, 1)
    name = parts[0].lower()
    arg  = parts[1].strip() if len(parts) > 1 else ""

    if name == "/clear":
        state["messages"][:] = [state["messages"][0]]
        console.print(f"  [dim]history cleared[/dim]\n")

    elif name == "/model":
        if not arg:
            console.print(f"  [dim]current model: {state['provider']} · {state['model']}  (/model-select to pick another)[/dim]\n")
        else:
            _set_model(state, arg)
            console.print(f"  [dim]model → {_escape(arg)}[/dim]\n")

    elif name in ("/model-select", "/models"):
        if _need_terminal("/model-select"):
            picked = pickers.pick_provider_model(state["provider"], state["model"])
            if picked is None:
                console.print("  [dim]no change[/dim]\n")
            else:
                provider, model = picked
                if provider == state["provider"]:
                    _set_model(state, model)
                else:
                    ok, key = pickers.resolve_key(provider)
                    if not ok:
                        console.print(f"  [dim]no key for {provider}, staying on {state['provider']}[/dim]\n")
                        return
                    try:
                        _switch_provider(state, provider, model, key)
                    except ValueError as e:
                        console.print(f"  [bold {_RED}]{_escape(str(e))}[/bold {_RED}]\n")
                        return
                console.print(f"  [dim]now using {state['provider']} · {_escape(state['model'])}[/dim]\n")

    elif name in ("/apikey", "/api-key", "/keys", "/api"):
        if _need_terminal("/apikey"):
            active_env = PROVIDERS[state["provider"]].get("api_key_env")

            def _changed(env: str, value: str | None) -> None:
                if env != active_env:
                    return
                if value is None:
                    console.print(f"  [dim]this chat keeps the key already loaded; it will not be there next start[/dim]")
                    return
                try:
                    _switch_provider(state, state["provider"], state["model"], value)
                    console.print(f"  [dim]now using the new {state['provider']} key[/dim]")
                except ValueError as e:
                    console.print(f"  [bold {_RED}]{_escape(str(e))}[/bold {_RED}]")

            pickers.manage_keys(_changed)
            console.print()

    elif name == "/sessions":
        if _need_terminal("/sessions"):
            sessions = _list_sessions()
            if not sessions:
                console.print("  [dim]no saved sessions yet. a chat is saved when you exit or run /save[/dim]\n")
            else:
                chosen = pickers.pick_session(sessions)
                if chosen is None:
                    console.print("  [dim]no change[/dim]\n")
                else:
                    _load_into_state(state, chosen)

    elif name == "/tools":
        console.print(f"\n  [dim]tools ({len(TOOL_DEFINITIONS)}):[/dim]")
        for t in TOOL_DEFINITIONS:
            n = t["function"]["name"]
            d = t["function"]["description"].split(".")[0][:60]
            console.print(f"  [{_AMBER}]{n:<18}[/{_AMBER}] [dim]{d}[/dim]")
        console.print()

    elif name == "/skills":
        from tools.skills import discover
        found = discover()
        console.print(f"\n  [dim]skills ({len(found)}):[/dim]")
        for s in found.values():
            console.print(f"  [{_AMBER}]{s.name:<20}[/{_AMBER}] [dim]{_escape(s.description[:70])}[/dim]")
        console.print()

    elif name == "/skill":
        from tools.skills import use_skill
        if not arg:
            console.print(f"  [dim]usage: /skill <name>  (see /skills)[/dim]\n")
        else:
            text = use_skill(arg)
            if text.startswith("# Skill:"):
                state["messages"].append({
                    "role": "user",
                    "content": f"[/skill {arg}] Use this playbook for the next task.\n\n{text}",
                })
                state["messages"].append({"role": "assistant", "content": f"Skill '{arg}' loaded."})
                console.print(f"  [dim]skill {_escape(arg)} loaded into this chat[/dim]\n")
            else:
                console.print(f"  [bold {_RED}]{_escape(text)}[/bold {_RED}]\n")

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

    elif name == "/pii":
        suppressed: set = state.setdefault("pii_suppressed", set())
        parts2 = arg.split(None, 1)
        subcmd = parts2[0].lower() if parts2 else ""
        pii_type = parts2[1].strip() if len(parts2) > 1 else ""

        if not subcmd or subcmd == "status":
            if suppressed:
                console.print(
                    f"  [dim]PII warnings suppressed for: "
                    f"[bold {_AMBER}]{', '.join(sorted(suppressed))}[/bold {_AMBER}][/dim]\n"
                )
            else:
                console.print(f"  [dim]all PII warnings active[/dim]\n")

        elif subcmd == "allow":
            if not pii_type:
                console.print(
                    f"  [dim]usage: /pii allow <Type>  (e.g. /pii allow Email)[/dim]\n"
                    f"  [dim]types: SSN, Credit Card, Phone, Email, API Key, Password[/dim]\n"
                )
            else:
                suppressed.add(pii_type)
                console.print(
                    f"  [dim]PII warning for [bold {_AMBER}]{pii_type}[/bold {_AMBER}] suppressed this session[/dim]\n"
                )

        elif subcmd == "block":
            if not pii_type:
                console.print(f"  [dim]usage: /pii block <Type>[/dim]\n")
            else:
                suppressed.discard(pii_type)
                console.print(
                    f"  [dim]PII warning for [bold {_AMBER}]{pii_type}[/bold {_AMBER}] re-enabled[/dim]\n"
                )

        elif subcmd == "reset":
            suppressed.clear()
            console.print(f"  [dim]all PII warnings restored[/dim]\n")

        else:
            console.print(f"  [dim]unknown subcommand: {subcmd} — try /pii allow Email[/dim]\n")

    elif name == "/attach":
        staged: list = state.setdefault("staged_attachments", [])
        if not arg:
            if staged:
                for sp in staged:
                    console.print(f"  [dim]📎 {_escape(str(sp))}[/dim]")
                console.print()
            else:
                console.print(
                    f"  [dim]usage: /attach <path>  — or type @path in a message\n"
                    f"  supported: text/code/CSV/JSON, PDF, Word (.docx), Excel (.xlsx)[/dim]\n"
                )
            return
        sp = resolve_path(arg.strip().strip('"').strip("'"), [get_cwd(), os.getcwd()])
        if sp is None:
            console.print(f"  [bold {_RED}]file not found: {_escape(arg)}[/bold {_RED}]\n")
            return
        if sp not in staged:
            staged.append(sp)
        console.print(f"  [dim]📎 {_escape(sp.name)} staged — it goes with your next message[/dim]\n")

    elif name == "/detach":
        n_staged = len(state.get("staged_attachments", []))
        state["staged_attachments"] = []
        console.print(f"  [dim]cleared {n_staged} staged file{'s' if n_staged != 1 else ''}[/dim]\n")

    elif name == "/memory":
        from tools.memory import list_memories, delete_memory, _MEMORY_FILE, _save
        sub = arg.split(None, 1)
        subcmd = sub[0].lower() if sub else ""
        subarg = sub[1].strip() if len(sub) > 1 else ""

        if not subcmd or subcmd == "list":
            text = list_memories()
            console.print(f"\n  [dim]memories:[/dim]")
            for line in text.splitlines():
                console.print(f"  [{_VIOLET}]•[/{_VIOLET}] [dim]{_escape(line)}[/dim]")
            console.print()

        elif subcmd == "forget":
            if not subarg:
                console.print(f"  [dim]usage: /memory forget <key>[/dim]\n")
            else:
                result = delete_memory(subarg)
                console.print(f"  [dim]{_escape(result)}[/dim]\n")
                # Refresh system prompt so model no longer sees the deleted memory
                state["messages"][0]["content"] = build_system_prompt()

        elif subcmd == "clear":
            if _MEMORY_FILE.exists():
                _save({"entries": []})
            console.print(f"  [dim]all memories cleared[/dim]\n")
            state["messages"][0]["content"] = build_system_prompt()

        else:
            console.print(f"  [dim]usage: /memory  |  /memory forget <key>  |  /memory clear[/dim]\n")

    elif name == "/e2b":
        if not arg:
            console.print(f"  [dim]usage: /e2b <your E2B API key>  (get one at https://e2b.dev)[/dim]\n")
        else:
            try:
                auth.save_key("E2B_API_KEY", arg.strip())
            except auth.AuthError as e:
                console.print(f"  [yellow]{_escape(str(e))}[/yellow]  [dim]using the E2B key for this session only[/dim]\n")
            else:
                where = "saved to your account" if auth.keys_persist() else "kept for this session only (viper login to save it)"
                console.print(f"  [dim]E2B key {where}; sandbox calls now use it[/dim]\n")

    elif name == "/help":
        console.print(f"\n{_HELP_TEXT}\n")

    else:
        console.print(f"  [dim]unknown command: {name} — try /help[/dim]\n")


# ── main chat loop ─────────────────────────────────────────────────────────────
def _admit_query() -> bool:
    """Block the query when the free trial is used up, offering sign-in."""
    ok, why = auth.query_gate()
    if ok:
        return True
    console.print(f"\n  [bold {_AMBER}]{why}[/bold {_AMBER}]")
    if onboarding.offer_login():
        return True
    console.print(f"  [dim]nothing was sent. run viper login any time.[/dim]\n")
    return False


def _count_query() -> None:
    if auth.counts_against_trial():
        left = auth.consume_trial()
        console.print(f"  [dim]free queries left: {left}[/dim]")


def chat(client, model: str, provider: str, resume: bool = False, cwd: str = None, identity: str = ""):
    if cwd:
        set_cwd(os.path.abspath(cwd))

    # Give spawn_agents tool access to the live client + model
    from tools.multiagent import init as _init_multiagent
    _init_multiagent(client, model)

    system_msg = {"role": "system", "content": build_system_prompt()}
    messages = [system_msg]

    if resume:
        session = _load_last_session()
        if session:
            messages = session.get("messages", [system_msg])
            # a saved session carries the old prompt; swap in the current prompt and skills index
            if messages and messages[0].get("role") == "system":
                messages[0] = system_msg
            else:
                messages.insert(0, system_msg)
            console.print(f"  [dim]resumed · {len(messages)} messages[/dim]")
        else:
            console.print(f"  [dim]no saved session found[/dim]")

    state = {"messages": messages, "model": model, "provider": provider, "client": client}

    console.print()
    console.print(_BANNER)
    console.print()
    who = "" if not identity else (
        f"free trial · {auth.trial_remaining()} queries left  ·  " if identity == "guest"
        else f"{identity}  ·  "
    )
    console.print(
        f"  [dim]{who}{provider}  ·  {model}  ·  "
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
            saved = _save_session(state["messages"], state["provider"], state["model"])
            console.print(f"\n  [dim]session saved → {saved}[/dim]\n  [dim]goodbye[/dim]\n")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "bye", ":q"):
            saved = _save_session(state["messages"], state["provider"], state["model"])
            console.print(f"\n  [dim]session saved → {saved}[/dim]\n  [dim]goodbye[/dim]\n")
            break

        if user_input.startswith("/"):
            if user_input.lower().startswith("/multi "):
                if not _admit_query():
                    continue
                _count_query()
            _handle_slash(user_input, state)
            continue

        if not _admit_query():
            continue

        # ── PII guard on user input ────────────────────────────────────────────
        pii = detect_pii(user_input)
        active_pii = [t for t in pii.found if t not in state.get("pii_suppressed", set())]
        if active_pii:
            console.print(
                f"\n  ⚠️   [bold {_AMBER}]PII detected in your message[/bold {_AMBER}]  "
                f"[dim]{', '.join(active_pii)}[/dim]\n"
                f"  [dim]This will be sent to the LLM provider ({state['provider']}). Proceed?[/dim]\n"
                f"  [dim](use /pii allow {active_pii[0]} to stop asking this session)[/dim]"
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

        _count_query()
        content = _prepare_message(user_input, state)
        state["messages"].append({"role": "user", "content": content})
        console.print()

        # Agentic loop — capped at _MAX_ITERATIONS tool-call rounds
        for _iter in range(_MAX_ITERATIONS):
            state["messages"] = _prune_context(state["messages"])
            if _iter > 0:
                _show_acting(_iter + 1)
            text, tool_calls = _stream(state["client"], state["model"], state["messages"])

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

                results = _run_tools(calls, state)

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
  viper                 first run: sign in with Google in your browser (or try 3 free queries)
  viper login           same as --login
  viper logout          sign out of this computer (saved keys stay in your account)
  python agent.py --provider openrouter --model mistralai/mistral-7b-instruct:free
  python agent.py --provider anthropic --model anthropic/claude-3.5-haiku
  python agent.py --provider ollama --model llama3.2
  python agent.py --resume
  python agent.py --cwd /path/to/project
  python agent.py --list-providers
        """,
    )
    parser.add_argument("--provider", "-p", default=None, choices=list(PROVIDERS),
                        help="Skip the provider menu (default: pick interactively)")
    parser.add_argument("command", nargs="?", choices=["login", "logout"],
                        help="'viper login' and 'viper logout' work like --login and --logout")
    parser.add_argument("--login", action="store_true", help="Sign in with Google through your browser")
    parser.add_argument("--logout", action="store_true", help="Sign out (saved keys stay in your account)")
    parser.add_argument("--model", "-m", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--resume", "-r", action="store_true", help="Resume last saved session")
    parser.add_argument("--cwd", default=None, help="Set working directory at startup")
    parser.add_argument("--list-providers", "-l", action="store_true")
    args = parser.parse_args()

    console.print("\n [bold green]Welcome to Viper[/bold green] [dim]— minimal multi-provider terminal AI agent[/dim]\n")

    if args.list_providers:
        _list_providers()
        return

    if args.login or args.command == "login":
        onboarding.cmd_login()
        return
    if args.logout or args.command == "logout":
        onboarding.cmd_logout()
        return

    try:
        start = onboarding.startup(args.provider, args.model, args.api_key)
    except KeyboardInterrupt:
        console.print(f"\n  [dim]goodbye[/dim]\n")
        return
    if start is None:
        return

    try:
        client, config = get_client(start.provider, start.api_key)
    except ValueError as e:
        console.print(f"\n  [bold {_RED}]error:[/bold {_RED}] {e}\n")
        sys.exit(1)

    model = start.model or config["default_model"]

    try:
        chat(client, model, start.provider, resume=args.resume, cwd=args.cwd, identity=start.identity)
    except KeyboardInterrupt:
        console.print(f"\n  [dim]goodbye[/dim]\n")


if __name__ == "__main__":
    main()
