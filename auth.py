"""Viper account: Supabase session storage and refresh, free-trial counter, sandbox grants.

Sign-in itself happens in the browser (browser_auth.py). No prompts here; onboarding.py owns the UI.
"""

import json
import os
import time
from pathlib import Path

import httpx

try:
    from cryptography.fernet import Fernet as _Fernet
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False

# Public by design: the anon key can only do what Row Level Security allows.
SUPABASE_URL      = os.environ.get("VIPER_SUPABASE_URL", "https://iboawgqlhwqgrttgawsy.supabase.co")
SUPABASE_ANON_KEY = os.environ.get("VIPER_SUPABASE_ANON_KEY", "sb_publishable_tokHsdbnIXzzGw9_6iB_3g_ANEMeZkT")
# Where auth.html is hosted, e.g. https://your-site.example/auth
AUTH_PAGE_URL     = os.environ.get("VIPER_AUTH_URL", "https://viper-auth.vercel.app/")

FREE_TRIAL_QUERIES = 3

_HOME       = Path.home() / ".viper"
_KEY_PATH   = _HOME / ".key"
_PROFILE    = _HOME / "profile.viper"   # Fernet-encrypted
_PROFILE_JS = _HOME / "profile.json"    # fallback when cryptography is missing
_TRIAL      = _HOME / "trial.json"      # survives logout on purpose
_TIMEOUT    = 15


class AuthError(Exception):
    """The message is safe to show to the user."""


# ── local storage ─────────────────────────────────────────────────────────────
def _private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except Exception:
        pass


def get_or_create_key() -> bytes:
    """Fernet key shared by session files and the profile."""
    if _KEY_PATH.exists():
        return _KEY_PATH.read_bytes()
    key = _Fernet.generate_key()
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _KEY_PATH.write_bytes(key)
    _private(_KEY_PATH)
    return key


def load_profile() -> dict:
    try:
        if _CRYPTO_OK and _PROFILE.exists():
            return json.loads(_Fernet(get_or_create_key()).decrypt(_PROFILE.read_bytes()))
        if _PROFILE_JS.exists():
            return json.loads(_PROFILE_JS.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_profile(profile: dict) -> None:
    _HOME.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(profile).encode()
    if _CRYPTO_OK:
        path, data = _PROFILE, _Fernet(get_or_create_key()).encrypt(raw)
    else:
        path, data = _PROFILE_JS, raw
    path.write_bytes(data)
    _private(path)


def clear_profile() -> None:
    for p in (_PROFILE, _PROFILE_JS):
        p.unlink(missing_ok=True)


def get_saved_key(name: str) -> str | None:
    return load_profile().get("keys", {}).get(name)


def save_key(name: str, value: str) -> None:
    p = load_profile()
    p.setdefault("keys", {})[name] = value
    save_profile(p)


def remember_choice(provider: str, model: str | None) -> None:
    p = load_profile()
    p["provider"], p["model"] = provider, model
    save_profile(p)


# ── mode checks ───────────────────────────────────────────────────────────────
def backend_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY and AUTH_PAGE_URL)


def is_dev_mode() -> bool:
    """A .env next to agent.py means a source checkout: no login, no limits."""
    return (Path(__file__).parent / ".env").exists()


def is_logged_in() -> bool:
    p = load_profile()
    return bool(p.get("email") and p.get("tokens"))


# ── free trial (local, so only a soft limit) ──────────────────────────────────
def trial_used() -> int:
    try:
        return int(json.loads(_TRIAL.read_text(encoding="utf-8"))["queries"])
    except Exception:
        return 0


def trial_remaining() -> int:
    return max(0, FREE_TRIAL_QUERIES - trial_used())


def counts_against_trial() -> bool:
    return backend_configured() and not is_dev_mode() and not is_logged_in()


def consume_trial() -> int:
    """Record one free query and return how many are left."""
    _HOME.mkdir(parents=True, exist_ok=True)
    _TRIAL.write_text(json.dumps({"queries": trial_used() + 1}), encoding="utf-8")
    return trial_remaining()


def query_gate() -> tuple[bool, str]:
    if not counts_against_trial() or trial_remaining() > 0:
        return True, ""
    return False, (
        f"Free trial finished ({FREE_TRIAL_QUERIES}/{FREE_TRIAL_QUERIES} queries used). "
        "Sign in to keep going."
    )


# ── Supabase auth ─────────────────────────────────────────────────────────────
def _headers(token: str | None = None) -> dict:
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {token or SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
    }


def _post(path: str, body: dict | None, token: str | None = None, params: dict | None = None):
    try:
        return httpx.post(
            f"{SUPABASE_URL.rstrip('/')}{path}",
            json=body, headers=_headers(token), params=params, timeout=_TIMEOUT,
        )
    except httpx.HTTPError:
        raise AuthError("Can't reach the Viper backend. Check your connection and try again.")


def _msg(r) -> str:
    try:
        j = r.json()
        return str(j.get("msg") or j.get("error_description") or j.get("message") or j.get("error"))
    except Exception:
        return r.text[:120] or f"HTTP {r.status_code}"


def _store_session(data: dict, email: str | None = None) -> None:
    p = load_profile()
    p["email"] = email or (data.get("user") or {}).get("email") or p.get("email")
    p["tokens"] = {
        "access": data["access_token"],
        "refresh": data["refresh_token"],
        "expires_at": int(data.get("expires_at") or time.time() + int(data.get("expires_in", 3600))),
    }
    save_profile(p)


def verify_access_token(access_token: str) -> str:
    """Check a token handed over by the browser with Supabase. Returns the verified email."""
    try:
        r = httpx.get(
            f"{SUPABASE_URL.rstrip('/')}/auth/v1/user",
            headers=_headers(access_token), timeout=_TIMEOUT,
        )
    except httpx.HTTPError:
        raise AuthError("Can't reach the Viper backend to verify your sign-in.")
    if r.status_code >= 400:
        raise AuthError("That sign-in was not accepted. Please try again.")
    user = r.json()
    if not user.get("email_confirmed_at"):
        raise AuthError("Your email is not verified yet. Confirm it from your inbox, then sign in.")
    return str(user["email"]).lower()


def store_session(email: str, access_token: str, refresh_token: str, expires_at: int | None = None) -> None:
    _store_session(
        {"access_token": access_token, "refresh_token": refresh_token, "expires_at": expires_at},
        email,
    )


def current_session() -> dict | None:
    """Return {'email', 'access'} for a valid login, refreshing the token if needed."""
    p = load_profile()
    t = p.get("tokens")
    if not (p.get("email") and t):
        return None
    if t["expires_at"] - 60 > time.time():
        return {"email": p["email"], "access": t["access"]}
    try:
        r = _post("/auth/v1/token", {"refresh_token": t["refresh"]}, params={"grant_type": "refresh_token"})
    except AuthError:
        return {"email": p["email"], "access": t["access"]}   # offline: stay signed in locally
    if r.status_code in (400, 401, 403):
        p.pop("tokens", None)
        p.pop("email", None)
        save_profile(p)
        return None
    if r.status_code >= 400:
        return {"email": p["email"], "access": t["access"]}
    _store_session(r.json(), p["email"])
    return {"email": p["email"], "access": load_profile()["tokens"]["access"]}


def logout() -> str | None:
    """Sign out and wipe the profile (saved keys included). Returns the old email."""
    p = load_profile()
    email = p.get("email")
    token = (p.get("tokens") or {}).get("access")
    if token and backend_configured():
        try:
            _post("/auth/v1/logout", None, token=token, params={"scope": "local"})
        except AuthError:
            pass
    clear_profile()
    return email


# ── free E2B sandbox ──────────────────────────────────────────────────────────
def request_sandbox_key() -> tuple[str, str]:
    """Return (e2b_key, note). note is empty for the user's own key, set for a free grant."""
    own = os.environ.get("E2B_API_KEY") or get_saved_key("E2B_API_KEY")
    if own:
        return own, ""
    if not backend_configured():
        raise AuthError("E2B_API_KEY is not set. Set the variable or run /e2b <key>. Get a key at https://e2b.dev")
    s = current_session()
    if not s:
        raise AuthError("Sign in to use the free sandbox (viper login), or add your own key with /e2b <key>.")
    try:
        r = httpx.post(
            f"{SUPABASE_URL.rstrip('/')}/functions/v1/sandbox-grant",
            headers=_headers(s["access"]), timeout=_TIMEOUT,
        )
    except httpx.HTTPError:
        raise AuthError("Can't reach the Viper backend to start a free sandbox.")
    if r.status_code == 403:
        j = r.json()
        raise AuthError(
            f"Free sandbox sessions used ({j.get('used')}/{j.get('limit')}). "
            "Add your own E2B key with /e2b <key> or the E2B_API_KEY variable (https://e2b.dev)."
        )
    if r.status_code >= 400:
        raise AuthError(f"Could not start a free sandbox: {_msg(r)}")
    j = r.json()
    return j["key"], f"free sandbox session {j['used']}/{j['limit']}"
