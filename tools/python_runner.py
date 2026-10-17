import subprocess
import sys
import tempfile
import os
import shutil
import venv
from tools.state import get_cwd

_DEFAULT_TIMEOUT = 30


def _make_temp_venv(packages: list) -> tuple[str, str]:
    """Create a throwaway venv, install packages, return (venv_dir, python_exe)."""
    venv_dir = tempfile.mkdtemp(prefix="viper_tmp_venv_")
    venv.create(venv_dir, with_pip=True)
    if sys.platform == "win32":
        python_exe = os.path.join(venv_dir, "Scripts", "python.exe")
        pip_exe = os.path.join(venv_dir, "Scripts", "pip.exe")
    else:
        python_exe = os.path.join(venv_dir, "bin", "python")
        pip_exe = os.path.join(venv_dir, "bin", "pip")

    for pkg in packages:
        result = subprocess.run(
            [pip_exe, "install", pkg, "-q"],
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            shutil.rmtree(venv_dir, ignore_errors=True)
            raise RuntimeError(
                f"Failed to install '{pkg}': {result.stderr.decode(errors='replace').strip()}"
            )

    return venv_dir, python_exe


def run_python(code: str, packages: list = None, timeout: int = _DEFAULT_TIMEOUT,
               sandbox: bool = True) -> str:
    if sandbox:
        from tools.sandbox import run_python_in_sandbox
        return run_python_in_sandbox(code, packages=packages, timeout=min(timeout, 120))

    venv_dir = None
    python_exe = sys.executable

    try:
        if packages:
            try:
                venv_dir, python_exe = _make_temp_venv(packages)
            except RuntimeError as e:
                return str(e)

        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8"
            ) as f:
                f.write(code)
                tmp = f.name

            result = subprocess.run(
                [python_exe, tmp],
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

    finally:
        if venv_dir:
            shutil.rmtree(venv_dir, ignore_errors=True)


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
                            "Where to run: true = isolated E2B cloud VM, false = user's local machine. "
                            "sandbox=FALSE only when ALL are true: stdlib-only imports, no file I/O, no network, "
                            "no packages param, pure in-memory calculation. "
                            "sandbox=TRUE for everything else — image processing, file editing, API calls, "
                            "data analysis, any third-party library, any packages param. "
                            "If packages are needed on the host (sandbox=false), a throwaway venv is created "
                            "automatically and deleted after the run — nothing is installed into the user's environment. "
                            "If you need a local file in the sandbox: read it with read_file first, then upload "
                            "it via sbx.files.write(), then run with sandbox=true. Default is true."
                        ),
                        "default": True,
                    },
                },
                "required": ["code"],
            },
        },
    },
]
