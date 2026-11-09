import os
import re
import subprocess
import sys

from tools.guardrails import check_command
from tools.state import get_cwd, set_cwd

_DEFAULT_TIMEOUT = 60
_CD_RE = re.compile(r'^cd\s*(.*)', re.IGNORECASE)
# Trailing & (background operator) — works on Unix; we emulate it on Windows
_BG_RE = re.compile(r'&\s*$')

# When set by agent.py, run_shell calls this for each output line in real time.
# None = use the original capture_output approach.
_stream_cb = None  # callable(line: str) | None


def run_shell(command: str, cwd: str = None, timeout: int = _DEFAULT_TIMEOUT,
              sandbox: bool = True) -> str:
    # Delegate to E2B sandbox when requested
    if sandbox:
        from tools.sandbox import run_shell_in_sandbox
        return run_shell_in_sandbox(command, timeout=min(timeout, 300))

    # Same sensitive-file protection the file tools have — fires before anything runs
    blocked = check_command(command)
    if blocked:
        return blocked

    effective_cwd = cwd or get_cwd()
    cmd = command.strip()

    # Intercept bare `cd` so working directory persists across calls
    m = _CD_RE.match(cmd)
    if m and not cwd:
        target = m.group(1).strip().strip('"').strip("'") or os.path.expanduser("~")
        target = os.path.expanduser(target)
        new_path = os.path.normpath(os.path.join(effective_cwd, target))
        if os.path.isdir(new_path):
            set_cwd(new_path)
            return f"Changed directory to: {new_path}"
        return f"Directory not found: {new_path}"

    # Detect background operator (&) and launch detached instead of blocking
    if _BG_RE.search(cmd):
        bg_cmd = _BG_RE.sub("", cmd).strip()
        return _run_background(bg_cmd, effective_cwd)

    cb = _stream_cb  # snapshot — avoid TOCTOU if another thread clears it mid-run
    if cb is not None:
        return _run_streaming(command, effective_cwd, min(timeout, 300), cb)
    return _run_captured(command, effective_cwd, min(timeout, 300))


def _run_background(command: str, cwd: str) -> str:
    """Launch a process detached from the agent — returns immediately with the PID."""
    try:
        kwargs: dict = {
            "shell": True,
            "cwd": cwd,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(command, **kwargs)
        return (
            f"Started in background (PID {proc.pid}): {command}\n"
            "Process is detached — it will keep running after this session ends. "
            "To stop it, use: kill {proc.pid} (Unix) or taskkill /PID {proc.pid} /F (Windows)"
        ).replace("{proc.pid}", str(proc.pid))
    except Exception as e:
        return f"Error starting background process: {e}"


def _run_captured(command: str, cwd: str, timeout: int) -> str:
    """Original non-streaming path: capture stdout+stderr separately."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True,
            text=True, timeout=timeout, cwd=cwd,
        )
        parts = []
        if result.stdout.strip():
            parts.append(result.stdout.strip())
        if result.stderr.strip():
            parts.append(f"STDERR:\n{result.stderr.strip()}")
        if result.returncode != 0:
            parts.append(f"Exit code: {result.returncode}")
        return "\n\n".join(parts) if parts else "(command completed, no output)"
    except subprocess.TimeoutExpired:
        return f"Timeout: command exceeded {timeout}s"
    except Exception as e:
        return f"Error running shell command: {e}"


def _run_streaming(command: str, cwd: str, timeout: int, cb) -> str:
    """Streaming path: merge stdout+stderr and call cb() for each line as it arrives."""
    import threading

    try:
        proc = subprocess.Popen(
            command, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            cwd=cwd,
        )
        lines: list[str] = []

        def _read():
            for raw in proc.stdout:
                line = raw.rstrip("\n").rstrip("\r")
                lines.append(line)
                cb(line)

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
        reader.join(timeout=timeout)

        if reader.is_alive():
            proc.kill()
            reader.join(timeout=2)
            lines.append(f"[Timeout: command exceeded {timeout}s — process killed]")
        else:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

        if proc.returncode not in (None, 0):
            lines.append(f"Exit code: {proc.returncode}")
        return "\n".join(lines) if lines else "(command completed, no output)"
    except Exception as e:
        return f"Error running shell command: {e}"


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Run any shell command and return its output. "
                "Use this for git, npm, pip, docker, file ops, or anything you'd type in a terminal. "
                "Working directory persists across calls — use cd to navigate. "
                "On Windows this runs through cmd.exe. Prefer this over run_python for shell operations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute, e.g. 'git status' or 'npm run build'",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Override working directory for this command only",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to wait (default 60, max 300)",
                        "default": 60,
                    },
                    "sandbox": {
                        "type": "boolean",
                        "description": (
                            "Where to run: true = isolated E2B cloud VM, false = user's local machine. "
                            "Ask yourself: does the user want this result on their local machine? "
                            "sandbox=FALSE (host): creating project files/dirs, running a local script, "
                            "git status/log/diff, ls, pwd, which, version checks — anything the user wants "
                            "to persist locally. Prefer write_file/edit_file tools over run_shell for file creation. "
                            "sandbox=TRUE (cloud VM): pip install, npm install, cargo build, uv add, conda install "
                            "(ANY package install), git clone <url>, running downloaded/untrusted scripts. "
                            "Default is true — override to false only when the user wants the output on their machine."
                        ),
                        "default": True,
                    },
                },
                "required": ["command"],
            },
        },
    },
]
