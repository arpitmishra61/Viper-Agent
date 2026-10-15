import os
import re
import subprocess

from tools.state import get_cwd, set_cwd

_DEFAULT_TIMEOUT = 60
_CD_RE = re.compile(r'^cd\s*(.*)', re.IGNORECASE)


def run_shell(command: str, cwd: str = None, timeout: int = _DEFAULT_TIMEOUT,
              sandbox: bool = True) -> str:
    # Delegate to E2B sandbox when requested
    if sandbox:
        from tools.sandbox import run_shell_in_sandbox
        return run_shell_in_sandbox(command, timeout=min(timeout, 300))

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

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=min(timeout, 300),
            cwd=effective_cwd,
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
                            "Run in an isolated E2B cloud microVM. DEFAULT IS TRUE — always use the sandbox. "
                            "Only pass sandbox=false for read-only local state checks that must inspect the "
                            "user's actual machine (e.g. checking a locally-installed tool version). "
                            "NEVER pass sandbox=false for git, GitHub, cloning, builds, installs, or any "
                            "command that modifies files or directories. The sandbox persists for the session "
                            "— files created in one sandboxed call are visible to all subsequent sandboxed calls."
                        ),
                        "default": True,
                    },
                },
                "required": ["command"],
            },
        },
    },
]
