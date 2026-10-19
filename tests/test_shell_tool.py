"""Tests for tools/shell.py — local execution only (sandbox=False), never touches E2B."""

import os
import sys

import pytest
from tools import state
from tools.shell import run_shell

PY = f'"{sys.executable}"'


@pytest.fixture(autouse=True)
def _restore_cwd():
    old = state.get_cwd()
    yield
    state.set_cwd(old)


def _same_dir(a: str, b: str) -> bool:
    return os.path.samefile(a, b)


class TestExecution:
    def test_echo(self):
        assert "hello" in run_shell("echo hello", sandbox=False)

    def test_nonzero_exit_code_reported(self):
        assert "Exit code: 3" in run_shell("exit 3", sandbox=False)

    def test_stderr_reported(self):
        out = run_shell(f"{PY} -c \"import sys; sys.stderr.write('boom')\"", sandbox=False)
        assert "STDERR" in out and "boom" in out

    def test_no_output_message(self):
        out = run_shell(f"{PY} -c \"pass\"", sandbox=False)
        assert "no output" in out

    def test_timeout(self):
        out = run_shell(f"{PY} -c \"import time; time.sleep(10)\"", timeout=1, sandbox=False)
        assert out.startswith("Timeout")

    def test_cwd_override_applies_to_one_call_only(self, tmp_path):
        before = state.get_cwd()
        out = run_shell(f"{PY} -c \"import os; print(os.getcwd())\"", cwd=str(tmp_path), sandbox=False)
        assert _same_dir(out.strip(), str(tmp_path))
        assert state.get_cwd() == before


class TestCdPersistence:
    def test_cd_into_subdir_persists(self, tmp_path):
        (tmp_path / "sub").mkdir()
        state.set_cwd(str(tmp_path))
        assert "Changed directory" in run_shell("cd sub", sandbox=False)
        assert _same_dir(state.get_cwd(), str(tmp_path / "sub"))
        # next call runs inside the new directory
        out = run_shell(f"{PY} -c \"import os; print(os.getcwd())\"", sandbox=False)
        assert _same_dir(out.strip(), str(tmp_path / "sub"))

    def test_cd_parent(self, tmp_path):
        (tmp_path / "sub").mkdir()
        state.set_cwd(str(tmp_path / "sub"))
        run_shell("cd ..", sandbox=False)
        assert _same_dir(state.get_cwd(), str(tmp_path))

    def test_cd_missing_dir_leaves_cwd_unchanged(self, tmp_path):
        state.set_cwd(str(tmp_path))
        assert "Directory not found" in run_shell("cd nope", sandbox=False)
        assert state.get_cwd() == str(tmp_path)

    def test_bare_cd_goes_home(self, tmp_path):
        state.set_cwd(str(tmp_path))
        run_shell("cd", sandbox=False)
        assert _same_dir(state.get_cwd(), os.path.expanduser("~"))

    def test_quoted_path_with_spaces(self, tmp_path):
        (tmp_path / "my dir").mkdir()
        state.set_cwd(str(tmp_path))
        run_shell('cd "my dir"', sandbox=False)
        assert _same_dir(state.get_cwd(), str(tmp_path / "my dir"))


class TestKnownBugs:
    """Documented defects — strict xfail so they turn red (XPASS) once fixed."""

    @pytest.mark.xfail(strict=True, reason="_CD_RE '^cd\\s*(.*)' also matches commands like 'cdk' / 'cdparanoia'")
    def test_commands_starting_with_cd_are_not_intercepted(self):
        out = run_shell("cdk --version", sandbox=False)
        assert "Directory not found" not in out and "Changed directory" not in out

    @pytest.mark.xfail(strict=True, reason="'cd x && cmd' is treated as one directory name")
    def test_cd_and_chain(self, tmp_path):
        (tmp_path / "sub").mkdir()
        state.set_cwd(str(tmp_path))
        out = run_shell("cd sub && echo CHAIN_OK", sandbox=False)
        assert "Directory not found" not in out
        assert "CHAIN_OK" in out
