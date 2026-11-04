"""Interactive menus behind /sessions, /model-select and /apikey.

UI only. agent.py owns the chat state and applies whatever these return, so nothing here imports agent.
Every menu returns None (or does nothing) when the user cancels with Esc / Ctrl+C.
"""

import os
import sys
from typing import Callable

import httpx
import questionary
from rich.console import Console
from rich.markup import escape

import auth
import onboarding
from providers import PROVIDERS

console = Console(highlight=False)

BACK = "__back__"
_SEARCH = "__search__"
_CUSTOM = "__custom__"
_OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"


def interactive() -> bool:
    return sys.stdin.isatty()


def mask_key(value: str) -> str:
    """Enough to recognise a key, never enough to use it."""
    if len(value) <= 12:
        return f"set ({len(value)} chars)"
    return f"{value[:5]}...{value[-4:]}"


def _saved_names() -> set[str]:
    try:
        return auth.saved_key_names()
    except auth.AuthError as e:
        console.print(f"  [yellow]{escape(str(e))}[/yellow]")
        return set()


# ── /sessions ─────────────────────────────────────────────────────────────────
def _session_label(s: dict) -> str:
    model = s.get("model") or "?"
    return f"{s['when']}  {model[:28]:<28}  {s['count']:>3} msgs  {s['preview']}"


def pick_session(sessions: list[dict]) -> dict | None:
    """Choose one of agent._list_sessions(). Type to filter the list."""
    choices = [questionary.Choice(_session_label(s), value=i) for i, s in enumerate(sessions)]
    choices.append(questionary.Choice("Cancel", value=BACK))
    pick = questionary.select(
        "Pick a session to load (type to filter):",
        choices=choices, use_search_filter=True, use_jk_keys=False,
    ).ask()
    return None if pick in (None, BACK) else sessions[pick]


# ── /model-select ─────────────────────────────────────────────────────────────
def _openrouter_models() -> list[str]:
    """Every model OpenRouter lists right now. Public endpoint, no key needed. [] when offline."""
    try:
        r = httpx.get(_OPENROUTER_MODELS, timeout=10)
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []) if m.get("id"))
    except Exception:
        return []


def _ollama_models(base_url: str) -> list[str]:
    """Models installed in the local Ollama. [] when Ollama is not running."""
    try:
        r = httpx.get(base_url.rstrip("/").removesuffix("/v1") + "/api/tags", timeout=3)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", []) if m.get("name")]
    except Exception:
        return []


def models_for(provider: str, current: str | None = None) -> list[str]:
    cfg = PROVIDERS[provider]
    if provider == "ollama":
        models = _ollama_models(cfg["base_url"]) or [cfg["default_model"]]
    else:
        models = list(cfg.get("models") or [cfg["default_model"]])
    if cfg["default_model"] not in models:
        models.insert(0, cfg["default_model"])
    if current and current not in models:
        models.insert(0, current)
    return list(dict.fromkeys(models))


def _search_live(provider: str) -> str | None:
    ids = _openrouter_models()
    if provider == "anthropic":
        ids = [i for i in ids if i.startswith("anthropic/")]
    if not ids:
        console.print("  [yellow]could not load the model list (offline?). Type a model id instead.[/yellow]")
        return None
    pick = questionary.autocomplete(
        f"Search {len(ids)} models (type, Tab to complete, Enter to pick):",
        choices=ids, match_middle=True,
        validate=lambda v: v in ids or "choose a model from the list",
    ).ask()
    return (pick or "").strip() or None


def pick_model(provider: str, current: str | None = None) -> str | None:
    live = provider in ("openrouter", "anthropic")
    while True:
        choices = [
            questionary.Choice(m + ("   (current)" if m == current else ""), value=m)
            for m in models_for(provider, current)
        ]
        if live:
            choices.append(questionary.Choice("Search all OpenRouter models...", value=_SEARCH))
        choices.append(questionary.Choice("Type a model id...", value=_CUSTOM))
        choices.append(questionary.Choice("Cancel", value=BACK))
        pick = questionary.select(f"Which {provider} model?", choices=choices).ask()
        if pick in (None, BACK):
            return None
        if pick == _SEARCH:
            found = _search_live(provider)
            if found:
                return found
        elif pick == _CUSTOM:
            typed = (questionary.text("Model id:").ask() or "").strip()
            if typed:
                return typed
        else:
            return pick


def _key_tag(cfg: dict, saved: set[str]) -> str:
    env = cfg.get("api_key_env")
    if not env:
        return "no key needed"
    if os.environ.get(env):
        return "key from environment"
    if env in saved:
        return "key saved"
    return "no key yet"


def pick_provider_model(current_provider: str, current_model: str | None) -> tuple[str, str] | None:
    """Provider menu, then that provider's models. Returns (provider, model) or None."""
    saved = _saved_names()
    choices = []
    for name, cfg in PROVIDERS.items():
        mark = "  (current)" if name == current_provider else ""
        label = f"{name:<12} {cfg['description']}  [{_key_tag(cfg, saved)}]{mark}"
        choices.append(questionary.Choice(label, value=name))
    choices.append(questionary.Choice("Cancel", value=BACK))
    provider = questionary.select("Which provider?", choices=choices, default=current_provider).ask()
    if provider in (None, BACK):
        return None
    model = pick_model(provider, current_model if provider == current_provider else None)
    return None if model is None else (provider, model)


def resolve_key(provider: str) -> tuple[bool, str | None]:
    """Get the key a provider needs: environment, then your account, then ask.

    Returns (ok, key). ok is False when the user gave up; key is None for providers that need none.
    """
    env = PROVIDERS[provider].get("api_key_env")
    if not env:
        return True, None
    key = os.environ.get(env)
    if not key:
        try:
            key = auth.get_saved_key(env)
        except auth.AuthError as e:
            console.print(f"  [yellow]{escape(str(e))}[/yellow]")
    if not key:
        key = onboarding.ask_key(provider, env)
    return (bool(key), key)


# ── /apikey ───────────────────────────────────────────────────────────────────
def key_slots() -> list[dict]:
    """One entry per key you can set. openrouter and anthropic share OPENROUTER_API_KEY, so they share a slot."""
    slots: dict[str, dict] = {}
    for name, cfg in PROVIDERS.items():
        env = cfg.get("api_key_env")
        if env:
            slot = slots.setdefault(env, {"env": env, "providers": [], "check_as": name})
            slot["providers"].append(name)
    slots["E2B_API_KEY"] = {"env": "E2B_API_KEY", "providers": ["e2b sandbox"], "check_as": "e2b"}
    return list(slots.values())


def _slot_label(slot: dict) -> str:
    return " / ".join(slot["providers"])


def _slot_status(slot: dict, saved: set[str]) -> str:
    env = slot["env"]
    parts = []
    if env in saved:
        parts.append("saved")
    if os.environ.get(env):
        parts.append("from environment")
    return " + ".join(parts) or "not set"


def _edit_slot(slot: dict, saved: set[str], on_change: Callable[[str, str | None], None]) -> None:
    env, label = slot["env"], _slot_label(slot)
    if os.environ.get(env):
        console.print(
            f"  [dim]{env} is also set in your environment or .env. That value wins at startup, "
            f"so edit it there to change what Viper uses by default.[/dim]"
        )
    if env in saved:
        try:
            current = auth.get_saved_key(env)
        except auth.AuthError as e:
            console.print(f"  [yellow]{escape(str(e))}[/yellow]")
            return
        shown = mask_key(current) if current else "saved"
        action = questionary.select(
            f"{label}: saved key {shown}",
            choices=[
                questionary.Choice("Replace the key", value="set"),
                questionary.Choice("Remove the saved key", value="remove"),
                questionary.Choice("Back", value=BACK),
            ],
        ).ask()
    else:
        action = questionary.select(
            f"{label}: no key saved",
            choices=[questionary.Choice("Add a key", value="set"), questionary.Choice("Back", value=BACK)],
        ).ask()

    if action == "set":
        key = onboarding.ask_key(slot["check_as"], env)
        if key:
            on_change(env, key)
    elif action == "remove":
        if questionary.confirm(f"Remove the saved {label} key?", default=False).ask():
            try:
                auth.delete_key(env)
            except auth.AuthError as e:
                console.print(f"  [yellow]{escape(str(e))}[/yellow]")
                return
            console.print("  [dim]key removed[/dim]")
            on_change(env, None)


def manage_keys(on_change: Callable[[str, str | None], None]) -> None:
    """Pick a provider, then add, replace or remove its key. Repeats until you go back.

    on_change(env_name, new_key_or_None) is called after every change so the caller can refresh its client.
    """
    if not auth.keys_persist():
        console.print("  [dim]not signed in: keys you add last for this run only (viper login to keep them)[/dim]")
    slots = key_slots()
    while True:
        saved = _saved_names()
        choices = [
            questionary.Choice(f"{_slot_label(s):<24} {s['env']:<20} [{_slot_status(s, saved)}]", value=i)
            for i, s in enumerate(slots)
        ]
        choices.append(questionary.Choice("Done", value=BACK))
        pick = questionary.select("API keys. Pick a provider to add or edit its key:", choices=choices).ask()
        if pick in (None, BACK):
            return
        _edit_slot(slots[pick], saved, on_change)
