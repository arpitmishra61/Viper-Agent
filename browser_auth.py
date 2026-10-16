"""Browser sign-in handoff.

Flow: open auth.html with a random state and the port of a one-shot localhost server. After
the user signs in, the page submits the session to http://127.0.0.1:<port>/callback. The server
checks the state (and the Origin, when the browser sends one), verifies the token with Supabase,
stores the session and shuts down.

There is no PKCE here: PKCE protects an authorization code that is exchanged later, and this
handoff delivers the session directly. The state blocks forged callbacks and the token check
blocks anything Supabase did not issue.
"""

import html
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse

import auth

TIMEOUT_SECONDS = 300
_MAX_BODY = 16 * 1024


class _Result:
    def __init__(self) -> None:
        self.email: str | None = None


def _page(title: str, message: str, ok: bool) -> bytes:
    colour = "#39ff14" if ok else "#ef4444"
    return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Viper</title>
<body style="font:16px/1.5 system-ui,sans-serif;background:#0c1210;color:#e4efe8;display:grid;place-items:center;min-height:100vh;margin:0">
<main style="max-width:26rem;padding:2rem;text-align:center">
<h1 style="font-size:1.4rem;color:{colour}">{html.escape(title)}</h1>
<p style="color:#8fa196">{html.escape(message)}</p></main>""".encode()


def _handler(state: str, expected_origin: str, result: _Result):
    class Handler(BaseHTTPRequestHandler):
        timeout = 10   # drop idle speculative connections instead of hanging a thread

        def log_message(self, *args) -> None:
            pass

        def _reply(self, code: int, title: str, message: str, ok: bool = False) -> None:
            body = _page(title, message, ok)
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._reply(404, "Viper is waiting", "Finish signing in on the Viper page.")

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/callback":
                return self._reply(404, "Not found", "Unknown path.")
            origin = self.headers.get("Origin")
            if origin and origin != expected_origin:
                return self._reply(403, "Request refused", "This request did not come from the Viper sign-in page.")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if not 0 < length <= _MAX_BODY:
                return self._reply(400, "Request refused", "Missing or oversized request.")
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            field = lambda k: (form.get(k) or [""])[0]

            if not secrets.compare_digest(field("state"), state):
                return self._reply(400, "Request refused", "This sign-in belongs to a different Viper session. Start again from your terminal.")
            access, refresh = field("access_token"), field("refresh_token")
            if not access or not refresh:
                return self._reply(400, "Sign-in incomplete", "The page did not send a session. Try signing in again.")
            try:
                email = auth.verify_access_token(access)
            except auth.AuthError as e:
                return self._reply(400, "Sign-in not accepted", str(e))

            try:
                expires_at = int(field("expires_at")) or None
            except ValueError:
                expires_at = None
            auth.store_session(email, access, refresh, expires_at)
            self._reply(200, "You're signed in", "You can close this browser tab and return to your terminal.", ok=True)
            result.email = email

    return Handler


def browser_login(notify: Callable[[str, bool], None]) -> str:
    """Run the browser sign-in with Google. Returns the verified email."""
    state = secrets.token_urlsafe(32)
    result = _Result()
    parsed = urlparse(auth.AUTH_PAGE_URL)
    expected_origin = f"{parsed.scheme}://{parsed.netloc}"

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state, expected_origin, result))
    except OSError as e:
        raise auth.AuthError(f"Could not start the local sign-in server: {e}")
    server.daemon_threads = True
    server.timeout = 0.5

    sep = "&" if "?" in auth.AUTH_PAGE_URL else "?"
    url = auth.AUTH_PAGE_URL + sep + urlencode({"state": state, "port": server.server_address[1]})
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    notify(url, bool(opened))

    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        while result.email is None and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if result.email is None:
        raise auth.AuthError(
            f"Sign-in was not completed within {TIMEOUT_SECONDS // 60} minutes. Run viper login to try again."
        )
    return result.email
