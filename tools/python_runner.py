import subprocess
import sys
import tempfile
import os
from tools.state import get_cwd

_DEFAULT_TIMEOUT = 30


def run_python(code: str, packages: list = None, timeout: int = _DEFAULT_TIMEOUT,
               sandbox: bool = True) -> str:
    # Delegate to E2B sandbox when requested
    if sandbox:
        from tools.sandbox import run_python_in_sandbox
        return run_python_in_sandbox(code, packages=packages, timeout=min(timeout, 120))

    if packages:
        for pkg in packages:
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", pkg, "-q"],
                    capture_output=True,
                    timeout=60,
                )
            except Exception as e:
                return f"Failed to install '{pkg}': {e}"

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp = f.name

        result = subprocess.run(
            [sys.executable, tmp],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=get_cwd(),
        )

        parts = []
        if result.stdout.strip():
            parts.append(result.stdout.strip())
        if result.stderr.strip():
            parts.append(f"STDERR:\n{result.stderr.strip()}")
        if result.returncode != 0:
            parts.append(f"Exit code: {result.returncode}")

        return "\n\n".join(parts) if parts else "(ran successfully, no output)"

    except subprocess.TimeoutExpired:
        return f"Timeout: code exceeded {timeout}s limit."
    except Exception as e:
        return f"Error running code: {e}"
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Write and execute Python code to solve any problem. "
                "Use this when no other tool can do the job — file conversion, "
                "image processing, API calls, data analysis, web scraping, "
                "uploading files, sending requests, anything. "
                "The code runs in a subprocess and its stdout/stderr is returned. "
                "Optionally install packages before running."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The Python code to execute. Use print() to produce output.",
                    },
                    "packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of pip packages to install before running, e.g. ['requests', 'pillow']",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to run (default 30, max 120)",
                        "default": 30,
                    },
                    "sandbox": {
                        "type": "boolean",
                        "description": (
                            "Run in an isolated E2B cloud microVM. DEFAULT IS TRUE — always use the sandbox. "
                            "Only pass sandbox=false for trivial pure-Python calculations that pose no risk "
                            "and have no side effects on the filesystem. Shares the same sandbox filesystem "
                            "as sandboxed run_shell calls in the same session."
                        ),
                        "default": True,
                    },
                },
                "required": ["code"],
            },
        },
    },
]
