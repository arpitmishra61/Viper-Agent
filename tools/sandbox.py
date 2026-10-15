"""E2B cloud sandbox — persistent microVM for isolated shell and Python execution.

One sandbox is created on first use and reused for the session lifetime.
Files written in one sandboxed call (e.g. a cloned repo) are visible to all
subsequent sandboxed calls because they share the same filesystem.
"""

import atexit
import re

_sbx = None
_sbx_cwd = "/home/user"   # sandbox working directory, independent of local cwd
_CD_RE = re.compile(r"^cd\s*(.*)", re.IGNORECASE)


# ── sandbox lifecycle ──────────────────────────────────────────────────────────

def _api_key() -> tuple[str, bool]:
    """Return (key, is_free_grant). A free grant counts against the user's quota."""
    import auth
    try:
        key, note = auth.request_sandbox_key()
    except auth.AuthError as e:
        from rich.console import Console
        Console(highlight=False).print(f"  [yellow]sandbox:[/yellow] {e}")
        raise RuntimeError(
            f"Sandbox unavailable: {e} Tell the user this. Do not retry and do not "
            "fall back to local execution."
        )
    if note:
        from rich.console import Console
        Console(highlight=False).print(f"  [dim]sandbox: {note}[/dim]")
    return key, bool(note)


def _get_sandbox():
    """Return the live sandbox, creating it on first call."""
    global _sbx
    if _sbx is None:
        try:
            from e2b import Sandbox
        except ImportError:
            raise RuntimeError(
                "e2b package not installed. Run: pip install \"viper-agent[sandbox]\""
            )
        key, free = _api_key()
        # free grants get a shorter lifetime than a user's own key
        _sbx = Sandbox(api_key=key, timeout=1800 if free else 3600)
        atexit.register(_kill_sandbox)
    return _sbx


def _kill_sandbox() -> None:
    global _sbx
    if _sbx is not None:
        try:
            _sbx.kill()
        except Exception:
            pass
        _sbx = None


def get_sandbox_cwd() -> str:
    return _sbx_cwd


# ── sandboxed execution ────────────────────────────────────────────────────────

def run_shell_in_sandbox(command: str, timeout: int = 60) -> str:
    """Run a shell command inside the E2B sandbox, preserving cwd across calls."""
    global _sbx_cwd
    cmd = command.strip()

    # Intercept `cd` so sandbox cwd persists across tool calls
    m = _CD_RE.match(cmd)
    if m:
        target = (m.group(1).strip() or "/home/user").strip("'\"")
        try:
            sbx = _get_sandbox()
            # Resolve the new path inside the sandbox
            result = sbx.commands.run(
                f"cd '{_sbx_cwd}' && cd {target} && pwd",
                timeout=10,
            )
            if result.exit_code == 0:
                _sbx_cwd = result.stdout.strip()
                return f"Changed directory to: {_sbx_cwd}  [sandbox]"
            return f"Directory not found: {target}  [sandbox]"
        except Exception as e:
            return f"Sandbox error: {e}"

    try:
        sbx = _get_sandbox()
        result = sbx.commands.run(
            f"cd '{_sbx_cwd}' && {cmd}",
            timeout=min(timeout, 300),
        )
    except Exception as e:
        msg = str(e).lower()
        if "timeout" in msg:
            return f"Timeout: command exceeded {timeout}s in sandbox"
        return f"Sandbox error: {e}"

    parts = []
    if result.stdout.strip():
        parts.append(result.stdout.strip())
    if result.stderr.strip():
        parts.append(f"STDERR:\n{result.stderr.strip()}")
    if result.exit_code != 0:
        parts.append(f"Exit code: {result.exit_code}")
    suffix = "  [sandbox]"
    return ("\n\n".join(parts) + suffix) if parts else f"(command completed, no output){suffix}"


def run_python_in_sandbox(code: str, packages: list = None, timeout: int = 30) -> str:
    """Execute Python code inside the E2B sandbox."""
    try:
        sbx = _get_sandbox()
    except RuntimeError as e:
        return str(e)

    # Install requested packages inside the sandbox
    if packages:
        for pkg in packages:
            try:
                result = sbx.commands.run(f"pip install '{pkg}' -q", timeout=60)
                if result.exit_code != 0:
                    return f"Failed to install '{pkg}' in sandbox: {result.stderr.strip()}"
            except Exception as e:
                return f"Failed to install '{pkg}' in sandbox: {e}"

    # Write code as a file and run it (avoids shell quoting issues with complex code)
    try:
        sbx.files.write("/tmp/_viper_run.py", code)
        result = sbx.commands.run(
            f"cd '{_sbx_cwd}' && python3 /tmp/_viper_run.py",
            timeout=min(timeout, 120),
        )
    except Exception as e:
        msg = str(e).lower()
        if "timeout" in msg:
            return f"Timeout: code exceeded {timeout}s in sandbox"
        return f"Sandbox error: {e}"

    parts = []
    if result.stdout.strip():
        parts.append(result.stdout.strip())
    if result.stderr.strip():
        parts.append(f"STDERR:\n{result.stderr.strip()}")
    if result.exit_code != 0:
        parts.append(f"Exit code: {result.exit_code}")
    suffix = "  [sandbox]"
    return ("\n\n".join(parts) + suffix) if parts else f"(ran successfully, no output){suffix}"
