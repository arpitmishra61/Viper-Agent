"""Tests for the run_shell sensitive-file guard (guardrails.check_command + tools/shell.py).

Every secret here is fake and lives in a tmp dir. Both the process cwd and the shell cwd
are pointed at that dir so a test can never touch a real project's .env.
"""

import os

import pytest
from tools import execute_tool, state
from tools.guardrails import check_command
from tools.shell import run_shell

SECRET = "FAKE_SECRET_abc123xyz"


# ── check_command: must block ─────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    # .env in all the usual shapes
    "cat .env",
    "type .env",
    "type .\\.env",
    "cat ./.env",
    "cat /srv/app/.env",
    "cat -- .env",
    "more .env",
    "Get-Content .env",
    "cat '.env'",
    'cat ".env"',
    "grep KEY .env",
    "source .env",
    "cat .env.local",
    "cat .env.production",
    "cp .env /tmp/x",
    "echo hi && cat .env",
    "ls | cat .env",
    "python -c \"print(open('.env').read())\"",
    "node -e \"console.log(require('fs').readFileSync('.env','utf8'))\"",
    "CAT .ENV",
    # keys and credential files
    "cat ~/.ssh/id_rsa",
    "type %USERPROFILE%\\.ssh\\id_rsa",
    "cat $HOME/.ssh/id_ed25519",
    "ls ~/.ssh",
    "cd .ssh",
    "cat ~/.aws/credentials",
    "ls ~/.aws",
    "cat $HOME/.gnupg/secring.gpg",
    "cat ~/.netrc",
    "cat authorized_keys",
    "cp id_ecdsa /tmp/x",
    "cat known_hosts",
    "cat ~/.viper/.key",
    "type %USERPROFILE%\\.viper\\.key",
    "type %APPDATA%\\Microsoft\\Credentials\\x",
    # wildcards that would expand to a sensitive name
    "cat .e*",
    "type .en?",
    "cat id_*",
    "cat *_rsa",
    "cat .ss*/id_rsa",
])
def test_blocks_sensitive_access(cmd):
    result = check_command(cmd)
    assert result is not None and result.startswith("Blocked"), f"should block: {cmd}"


# ── check_command: must allow ─────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "ls -la",
    "dir",
    "git status",
    "git log --oneline",
    "echo hello",
    "echo credentials",
    "cat README.md",
    "type notes.txt",
    "cd sub",
    "git add .env.example",
    "cat .env.sample",
    "cat .env.template",
    "python -m venv .venv",
    "source .venv/bin/activate",
    "pip install python-dotenv",
    "cat environment.yml",
    "node -e \"console.log(process.env.HOME)\"",
    "python -c \"import os; print(os.environ['HOME'])\"",
    "cat id_rsa.pub",
    "ssh user@example.com",
    "cat *.txt",
    "ls *",
    "dir /s",
    "grep -r TODO src",
])
def test_allows_normal_commands(cmd):
    assert check_command(cmd) is None, f"should allow: {cmd}"


def test_message_names_what_was_matched():
    assert ".env" in check_command("cat .env")
    assert "id_rsa" in check_command("cat ~/.ssh/id_rsa") or ".ssh" in check_command("cat ~/.ssh/id_rsa")


def test_empty_command():
    assert check_command("") is None


# ── known gaps: runtime-built names can't be caught from the command text ─────

class TestKnownGaps:
    @pytest.mark.xfail(strict=True, reason="name assembled at runtime isn't visible in the command text")
    def test_string_concatenation(self):
        assert check_command("python -c \"print(open('.en'+'v').read())\"") is not None

    @pytest.mark.xfail(strict=True, reason="shell quote-splitting hides the literal name")
    def test_quote_splitting(self):
        assert check_command('cat .e""nv') is not None


# ── run_shell: the guard must stop the command before anything runs ───────────

@pytest.fixture
def workspace(tmp_path):
    old_proc, old_state = os.getcwd(), state.get_cwd()
    (tmp_path / ".env").write_text(f"API_SECRET={SECRET}\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("harmless notes\n", encoding="utf-8")
    os.chdir(tmp_path)
    state.set_cwd(str(tmp_path))
    yield tmp_path
    os.chdir(old_proc)
    state.set_cwd(old_state)


class TestRunShellIntegration:
    @pytest.mark.parametrize("cmd", ["type .env", "cat .env", "more .env", "type .e*"])
    def test_cannot_read_env_and_secret_never_returned(self, workspace, cmd):
        out = run_shell(cmd, sandbox=False)
        assert out.startswith("Blocked")
        assert SECRET not in out

    def test_python_one_liner_cannot_read_env(self, workspace):
        out = run_shell("python -c \"print(open('.env').read())\"", sandbox=False)
        assert out.startswith("Blocked") and SECRET not in out

    def test_normal_command_still_works(self, workspace):
        assert "harmless notes" in run_shell("type notes.txt" if os.name == "nt" else "cat notes.txt", sandbox=False)

    def test_blocked_command_does_not_execute_any_part_of_a_chain(self, workspace):
        out = run_shell("echo X > marker.txt && type .env", sandbox=False)
        assert out.startswith("Blocked")
        assert not (workspace / "marker.txt").exists()

    def test_blocked_with_explicit_cwd(self, workspace):
        out = run_shell("type .env", cwd=str(workspace), sandbox=False)
        assert out.startswith("Blocked") and SECRET not in out

    def test_env_file_untouched(self, workspace):
        run_shell("type .env", sandbox=False)
        assert SECRET in (workspace / ".env").read_text(encoding="utf-8")

    def test_through_execute_tool_dispatch(self, workspace):
        out = execute_tool("run_shell", {"command": "type .env", "sandbox": False})
        assert out.startswith("Blocked") and SECRET not in out

    def test_file_tool_and_shell_agree(self, workspace):
        # the same file is refused by both routes
        from tools.files import read_file
        assert read_file(".env").startswith("Blocked")
        assert run_shell("type .env", sandbox=False).startswith("Blocked")
