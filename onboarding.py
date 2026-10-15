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


# ── sign up / sign in (browser) ───────────────────────────────────────────────
def _notify(url: str, opened: bool) -> None:
    if opened:
        console.print("  [dim]opening your browser to finish signing in...[/dim]")
    else:
        console.print("  [yellow]could not open a browser automatically[/yellow]")
    console.print(f"  [dim]if nothing opens, visit:[/dim] {url}")
    console.print("  [dim]waiting for you to finish (5 minutes, ctrl+c to cancel)...[/dim]")


def login_flow(mode: str | None = None) -> str | None:
    """Sign up or sign in through the browser. Returns the email, or None if cancelled."""
    if not auth.backend_configured():
        console.print("  [red]login is not configured for this build[/red]\n")
        return None
    if mode is None:
        mode = questionary.select(
            "Welcome to Viper.",
            choices=[
                questionary.Choice("Sign up", value="signup"),
                questionary.Choice("Sign in", value="signin"),
                questionary.Choice("Cancel", value="cancel"),
            ],
        ).ask()
        if mode in (None, "cancel"):
            return None
    try:
        email = browser_auth.browser_login(mode, _notify)
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
        f"Sign out {p['email']}? Saved API keys on this machine will be removed.", default=False
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
        choices = [
            questionary.Choice("Sign up", value="signup"),
            questionary.Choice("Sign in", value="signin"),
        ]
        if left > 0:
            choices.append(questionary.Choice(
                f"Continue free ({left} of {auth.FREE_TRIAL_QUERIES} queries left)", value="free"))
        choices.append(questionary.Choice("Quit", value="quit"))
        pick = questionary.select("You are not signed in.", choices=choices).ask()
        if pick in (None, "quit"):
            return None
        if pick == "free":
            return "guest"
        email = login_flow(pick)
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


def _ask_key(provider: str, env_name: str) -> str | None:
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
        auth.save_key(env_name, key)
        return key
    return None


def _provider_step(provider_arg: str | None, model_arg: str | None, key_arg: str | None) -> Start | None:
    profile = auth.load_profile()
    saved = profile.get("keys", {})

    def key_source(cfg: dict) -> str | None:
        env = cfg.get("api_key_env")
        if env and os.environ.get(env):
            return "key from environment"
        if env and saved.get(env):
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
        key = os.environ.get(env) or saved.get(env)
        if not key:
            if not sys.stdin.isatty():
                console.print(f"  [red]no API key found. Set {env} or run viper interactively.[/red]\n")
                return None
            key = _ask_key(provider, env)
            if not key:
                return None

    model = model_arg or (profile.get("model") if profile.get("provider") == provider else None)
    auth.remember_choice(provider, model)
    return Start(provider, model, key, "")


def startup(provider_arg: str | None, model_arg: str | None, key_arg: str | None) -> Start | None:
    if auth.is_dev_mode():
        return Start(provider_arg or "openrouter", model_arg, key_arg, "")
    identity = _identity_step()
    if identity is None:
        return None
    start = _provider_step(provider_arg, model_arg, key_arg)
    if start is not None:
        start.identity = identity
    return start
