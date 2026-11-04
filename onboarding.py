"""Interactive startup: sign-in or free trial, provider choice, API key entry."""

import os
import sys
from dataclasses import dataclass

import httpx
import questionary
from rich.console import Console

import auth
import browser_auth
from providers import PROVIDERS

console = Console(highlight=False)


@dataclass
class Start:
    provider: str
    model: str | None
    api_key: str | None
    identity: str   # "" (dev), "guest", or the signed-in email


# ── sign in with Google (browser) ─────────────────────────────────────────────
def _notify(url: str, opened: bool) -> None:
    if opened:
        console.print("  [dim]opening your browser to sign in with Google...[/dim]")
    else:
        console.print("  [yellow]could not open a browser automatically[/yellow]")
    console.print(f"  [dim]if nothing opens, visit:[/dim] {url}")
    console.print("  [dim]waiting for you to finish (5 minutes, ctrl+c to cancel)...[/dim]")


def login_flow() -> str | None:
    """Sign in with Google through the browser. Returns the email, or None if cancelled."""
    if not auth.backend_configured():
        console.print("  [red]login is not configured for this build[/red]\n")
        return None
    try:
        email = browser_auth.browser_login(_notify)
    except auth.AuthError as e:
        console.print(f"\n  [red]{e}[/red]\n")
        return None
    except KeyboardInterrupt:
        console.print("\n  [dim]cancelled[/dim]\n")
        return None
    console.print(f"\n  [green]Authenticated as {email}[/green]\n")
    return email


def offer_login() -> bool:
    if questionary.confirm("Sign in now?", default=True).ask():
        return login_flow() is not None
    return False


def cmd_login() -> None:
    if not auth.backend_configured():
        console.print("  [red]login is not configured for this build[/red]\n")
        return
    s = auth.current_session()
    if s:
        console.print(f"  already logged in as [bold]{s['email']}[/bold]. run viper logout first to switch.\n")
        return
    login_flow()


def cmd_logout() -> None:
    p = auth.load_profile()
    if not p.get("email"):
        console.print("  [dim]not logged in[/dim]\n")
        return
    if questionary.confirm(
        f"Sign out {p['email']}? Your saved API keys stay in your account and return when you sign in.",
        default=False
    ).ask():
        auth.logout()
        console.print("  [dim]logged out[/dim]\n")


def _identity_step() -> str | None:
    """Return the identity to run as, or None if the user quits."""
    if not auth.backend_configured():
        return ""
    while True:
        s = auth.current_session()
        if s:
            console.print(f"  logged in as [bold]{s['email']}[/bold]")
            return s["email"]
        if not sys.stdin.isatty():
            return "guest"
        left = auth.trial_remaining()
        choices = [questionary.Choice("Sign in with Google", value="signin")]
        if left > 0:
            choices.append(questionary.Choice(
                f"Continue free ({left} of {auth.FREE_TRIAL_QUERIES} queries left)", value="free"))
        choices.append(questionary.Choice("Quit", value="quit"))
        pick = questionary.select("You are not signed in.", choices=choices).ask()
        if pick in (None, "quit"):
            return None
        if pick == "free":
            return "guest"
        email = login_flow()
        if email:
            return email


# ── API keys ──────────────────────────────────────────────────────────────────
def validate_key(provider: str, key: str) -> bool | None:
    """True = accepted, False = rejected, None = could not check."""
    try:
        if provider in ("openrouter", "anthropic"):
            r = httpx.get("https://openrouter.ai/api/v1/auth/key",
                          headers={"Authorization": f"Bearer {key}"}, timeout=8)
        elif provider == "huggingface":
            r = httpx.get("https://huggingface.co/api/whoami-v2",
                          headers={"Authorization": f"Bearer {key}"}, timeout=8)
        else:
            return True
    except httpx.HTTPError:
        return None
    return r.status_code == 200


def ask_key(provider: str, env_name: str) -> str | None:
    """Prompt for a key, check it, save it to the account. None if the user gave up."""
    for _ in range(3):
        key = questionary.password(f"Paste your {provider} API key ({env_name}):").ask()
        if not key:
            return None
        key = key.strip()
        ok = validate_key(provider, key)
        if ok is False:
            console.print("  [red]that key was rejected, try again[/red]")
            continue
        if ok is None and not questionary.confirm(
            "Could not verify the key (offline?). Use it anyway?", default=True
        ).ask():
            continue
        _store_key(env_name, key)
        return key
    return None


def _store_key(env_name: str, key: str) -> None:
    """Save a key to the account and say plainly where it ended up."""
    try:
        auth.save_key(env_name, key)
    except auth.AuthError as e:
        console.print(f"  [yellow]{e}[/yellow]\n  [dim]the key works for this session but was not saved[/dim]")
        return
    if auth.keys_persist():
        console.print("  [dim]key saved to your Viper account[/dim]")
    else:
        console.print("  [dim]key kept for this session only. sign in (viper login) to save keys to your account[/dim]")


def _provider_step(provider_arg: str | None, model_arg: str | None, key_arg: str | None) -> Start | None:
    profile = auth.load_profile()
    try:
        moved = auth.migrate_local_keys()
        if moved:
            console.print(f"  [dim]moved {moved} saved key(s) from this computer to your account[/dim]")
        saved = auth.saved_key_names()
    except auth.AuthError as e:
        console.print(f"  [yellow]{e}[/yellow]")
        saved = set()

    def key_source(cfg: dict) -> str | None:
        env = cfg.get("api_key_env")
        if env and os.environ.get(env):
            return "key from environment"
        if env and env in saved:
            return "key saved"
        return None

    provider = provider_arg
    if provider is None:
        last = profile.get("provider") if profile.get("provider") in PROVIDERS else None
        if not sys.stdin.isatty():
            provider = last or "openrouter"
        else:
            choices = []
            for name, cfg in PROVIDERS.items():
                tag = key_source(cfg)
                label = f"{name:<12} {cfg['description']}" + (f"  [{tag}]" if tag else "")
                choices.append(questionary.Choice(label, value=name))
            provider = questionary.select(
                "Which provider do you want to use?", choices=choices, default=last
            ).ask()
            if provider is None:
                return None

    cfg = PROVIDERS[provider]
    env = cfg.get("api_key_env")
    key = key_arg
    if env and not key:
        key = os.environ.get(env)
        if not key and env in saved:
            try:
                key = auth.get_saved_key(env)
            except auth.AuthError as e:
                console.print(f"  [yellow]{e}[/yellow]")
        if not key:
            if not sys.stdin.isatty():
                console.print(f"  [red]no API key found. Set {env} or run viper interactively.[/red]\n")
                return None
            key = ask_key(provider, env)
            if not key:
                return None

    model = model_arg or (profile.get("model") if profile.get("provider") == provider else None)
    auth.remember_choice(provider, model)
    return Start(provider, model, key, "")


def startup(provider_arg: str | None, model_arg: str | None, key_arg: str | None) -> Start | None:
    if auth.is_dev_mode():
        env = PROVIDERS[provider_arg or "openrouter"].get("api_key_env")
        if key_arg or not env or os.environ.get(env):
            return Start(provider_arg or "openrouter", model_arg, key_arg, "")
    identity = _identity_step()
    if identity is None:
        return None
    start = _provider_step(provider_arg, model_arg, key_arg)
    if start is not None:
        start.identity = identity
        if identity not in ("", "guest"):
            _e2b_step()
    return start


_E2B_BLURB = """
  [bold]Optional: add your own E2B sandbox key[/bold]
  E2B gives Viper a throwaway cloud machine, so code and shell commands run there instead of on your computer.
  [dim]Use it for:[/dim]
    - installing packages (pip / npm) without touching your system
    - cloning and running code you don't trust
    - building and testing a fix end to end, safely
  Your account already includes 3 free sandbox sessions. A key of your own (https://e2b.dev) removes that limit.
  [dim]Skip it if you don't need a sandbox. You can add it any time with /e2b <key>.[/dim]
"""


def _e2b_step() -> None:
    """After sign-in, offer once to save the user's own E2B key. Never blocks startup."""
    if not sys.stdin.isatty() or os.environ.get("E2B_API_KEY") or auth.e2b_prompt_done():
        return
    try:
        if "E2B_API_KEY" in auth.saved_key_names():
            return
    except auth.AuthError:
        return   # offline: ask next time instead of nagging now
    console.print(_E2B_BLURB)
    if not questionary.confirm("Add your own E2B key now?", default=False).ask():
        auth.mark_e2b_prompt_done()
        console.print("  [dim]skipped. you can still use the 3 free sandbox sessions, or run /e2b <key> later.[/dim]\n")
        return
    key = questionary.password("Paste your E2B API key:").ask()
    auth.mark_e2b_prompt_done()
    if not key or not key.strip():
        console.print("  [dim]nothing saved[/dim]\n")
        return
    _store_key("E2B_API_KEY", key.strip())
    console.print()
