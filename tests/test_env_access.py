"""Human-in-the-loop access to .env files.

Covers: recognising env files, masking, one-time grants, the user prompt in agent._run_tools,
and that the model cannot forge an approval. Every secret is fake and lives in a tmp dir.
"""

import os

import pytest

import agent
from tools import execute_tool, state
from tools.files import edit_file, list_dir, read_file, write_file
from tools import guardrails
from tools.guardrails import (
    check_command, grant_env_access, is_env_file, mask_env, take_env_grant,
)

SECRET = "FAKE_SECRET_abc123xyz"


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    guardrails._env_grants.clear()
    agent._env_denied.clear()
    monkeypatch.setattr(agent, "_session_tool_calls", 0)

    def _unexpected(*a, **k):
        raise AssertionError("unexpected user prompt")

    monkeypatch.setattr(agent.console, "input", _unexpected)
    yield
    guardrails._env_grants.clear()
    agent._env_denied.clear()


@pytest.fixture
def ws(tmp_path):
    old_proc, old_state = os.getcwd(), state.get_cwd()
    (tmp_path / ".env").write_text(
        f"# app config\nAPI_SECRET={SECRET}\nDEBUG=true\nEMPTY=\n", encoding="utf-8"
    )
    (tmp_path / ".env.example").write_text("API_SECRET=your_key_here\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("harmless notes\n", encoding="utf-8")
    os.chdir(tmp_path)
    state.set_cwd(str(tmp_path))
    yield tmp_path
    os.chdir(old_proc)
    state.set_cwd(old_state)


def answer(monkeypatch, *replies):
    """Script console.input; a reply may be a string or an exception class to raise."""
    it = iter(replies)
    asked: list[str] = []

    def _input(prompt="", *a, **k):
        asked.append(str(prompt))
        r = next(it)
        if isinstance(r, type) and issubclass(r, BaseException):
            raise r
        return r

    monkeypatch.setattr(agent.console, "input", _input)
    return asked


def call(name, args=None, tid="t"):
    return (name, args or {}, tid)


# ── is_env_file ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    ".env", ".env.local", ".env.production", "/srv/app/.env", "~/proj/.env.staging",
    "C:\\proj\\.env", ".ENV",
])
def test_is_env_file_true(path):
    assert is_env_file(path)


@pytest.mark.parametrize("path", [
    ".env.example", ".env.sample", ".env.template", ".env.dist", "env.txt", ".venv",
    "my.env", ".envrc", "environment.yml", "notes.txt", "", "/srv/app/",
])
def test_is_env_file_false(path):
    assert not is_env_file(path)


def test_is_env_file_tolerates_garbage():
    assert is_env_file(None) is False
    assert is_env_file(["a"]) is False


# ── mask_env ──────────────────────────────────────────────────────────────────

class TestMask:
    def test_basic(self):
        assert mask_env("KEY=secretvalue") == "KEY=<set, 11 chars>"

    def test_export_and_spaces_kept(self):
        assert mask_env("export  KEY = abc") == "export  KEY = <set, 3 chars>"

    def test_quotes_are_not_counted(self):
        assert mask_env('KEY="abc def"') == "KEY=<set, 7 chars>"
        assert mask_env("KEY='abc'") == "KEY=<set, 3 chars>"

    def test_empty_values(self):
        assert mask_env("KEY=") == "KEY=<empty>"
        assert mask_env('KEY=""') == "KEY=<empty>"

    @pytest.mark.parametrize("value", ["your_api_key_here", "changeme", "xxxx", "<your-key>", "TODO", "..."])
    def test_placeholders_are_labelled(self, value):
        assert "placeholder-looking" in mask_env(f"KEY={value}")

    def test_value_containing_equals(self):
        assert mask_env("KEY=a=b==") == "KEY=<set, 5 chars>"

    def test_inline_comment_does_not_leak(self):
        out = mask_env("KEY=abc # my note")
        assert "abc" not in out and "note" not in out

    def test_plain_comment_kept(self):
        assert mask_env("# database settings") == "# database settings"

    def test_comment_containing_a_secret_is_redacted(self):
        out = mask_env("# old key sk-" + "a" * 25)
        assert "sk-" not in out and "redacted" in out

    def test_blank_lines_and_trailing_newline_preserved(self):
        assert mask_env("A=1\n\nB=2\n") == "A=<set, 1 chars>\n\nB=<set, 1 chars>\n"
        assert not mask_env("A=1").endswith("\n")

    def test_multiline_quoted_value_fully_hidden(self):
        out = mask_env('PEM="line1-SECRET\nline2-SECRET\nline3-SECRET"\nNEXT=v\n')
        assert "SECRET" not in out
        assert "PEM=<set, multi-line>" in out
        assert "NEXT=<set, 1 chars>" in out

    def test_unparseable_line_redacted(self):
        out = mask_env("just some words with a secret")
        assert "secret" not in out and "redacted" in out

    def test_realistic_file_leaks_nothing(self):
        values = ["sk-or-v1-0123456789abcdef", "hunter2hunter2", "postgres://u:p4ss@db/x", "tok_live_998877"]
        text = (
            "# prod\n"
            f"OPENROUTER_API_KEY={values[0]}\n"
            f"export DB_PASSWORD='{values[1]}'\n"
            f"DATABASE_URL={values[2]}\n"
            f'STRIPE="{values[3]}"\n'
            "DEBUG=false\n"
        )
        out = mask_env(text)
        for v in values:
            assert v not in out
        for name in ("OPENROUTER_API_KEY", "DB_PASSWORD", "DATABASE_URL", "STRIPE", "DEBUG"):
            assert name in out


# ── grants + read_file ────────────────────────────────────────────────────────

class TestReadFileGrants:
    def test_no_grant_is_refused_and_nothing_leaks(self, ws):
        out = read_file(".env")
        assert out.startswith("Blocked") and SECRET not in out

    def test_masked_grant(self, ws):
        grant_env_access(".env", "masked")
        out = read_file(".env")
        assert SECRET not in out
        assert "API_SECRET=<set," in out and "EMPTY=<empty>" in out

    def test_full_grant(self, ws):
        grant_env_access(".env", "full")
        assert SECRET in read_file(".env")

    def test_grant_is_single_use(self, ws):
        grant_env_access(".env", "full")
        assert SECRET in read_file(".env")
        assert read_file(".env").startswith("Blocked")

    def test_two_grants_two_reads(self, ws):
        grant_env_access(".env", "masked")
        grant_env_access(".env", "full")
        assert SECRET not in read_file(".env")     # first grant (masked) used first
        assert SECRET in read_file(".env")
        assert read_file(".env").startswith("Blocked")

    def test_grant_for_one_path_does_not_unlock_another(self, ws):
        (ws / "sub").mkdir()
        (ws / "sub" / ".env").write_text("K=v\n", encoding="utf-8")
        grant_env_access(".env", "full")
        assert read_file("sub/.env").startswith("Blocked")

    def test_relative_and_absolute_spellings_share_a_grant(self, ws):
        grant_env_access(str(ws / ".env"), "full")
        assert SECRET in read_file(".env")

    def test_traversal_still_blocked_even_with_grant(self, ws):
        (ws / "sub").mkdir()
        grant_env_access("sub/../.env", "full")
        assert "traversal" in read_file("sub/../.env").lower()

    def test_other_sensitive_files_stay_hard_blocked(self, ws):
        grant_env_access("~/.ssh/id_rsa", "full")      # not an env file, so a grant can't apply
        assert read_file("~/.ssh/id_rsa").startswith("Blocked")

    def test_template_env_files_need_no_approval(self, ws):
        assert "your_key_here" in read_file(".env.example")

    def test_write_edit_list_still_blocked_even_with_grant(self, ws):
        grant_env_access(".env", "full")
        assert write_file(".env", "X=1").startswith("Blocked")
        assert edit_file(".env", "DEBUG", "X").startswith("Blocked")
        assert list_dir(".env").startswith("Blocked")
        assert SECRET in (ws / ".env").read_text(encoding="utf-8")

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError):
            grant_env_access(".env", "everything")

    def test_take_without_grant_is_none(self):
        assert take_env_grant(".env") is None


# ── the model cannot forge approval ───────────────────────────────────────────

class TestNoForgery:
    @pytest.mark.parametrize("extra", [
        {"_env_access": "full"}, {"approved": True}, {"mode": "full"}, {"reveal": True},
        {"user_approved": "yes"}, {"grant": "full"},
    ])
    def test_extra_arguments_do_not_unlock(self, ws, extra):
        out = execute_tool("read_file", {"path": ".env", **extra})
        assert out.startswith("Blocked") and SECRET not in out

    def test_direct_tool_calls_without_the_prompt_are_refused(self, ws):
        # what a sub-agent (orchestrator.py calls execute_tool directly) gets
        out = execute_tool("read_file", {"path": ".env"})
        assert out.startswith("Blocked") and SECRET not in out

    def test_shell_route_is_blocked_with_a_pointer_to_read_file(self, ws):
        out = execute_tool("run_shell", {"command": "type .env", "sandbox": False})
        assert out.startswith("Blocked") and SECRET not in out
        assert "read_file" in out

    def test_shell_cannot_use_a_grant(self, ws):
        grant_env_access(".env", "full")
        out = execute_tool("run_shell", {"command": "type .env", "sandbox": False})
        assert out.startswith("Blocked") and SECRET not in out
        assert take_env_grant(".env") == "full"        # the grant is still there, untouched

    def test_check_command_message_mentions_approval(self):
        assert "approve" in check_command("cat .env")


# ── the prompt, end to end through _run_tools ─────────────────────────────────

class TestHumanInTheLoop:
    def test_yes_gives_full_contents(self, ws, monkeypatch):
        asked = answer(monkeypatch, "yes")
        [r] = agent._run_tools([call("read_file", {"path": ".env"})])
        assert SECRET in r
        assert 'trust="untrusted"' in r            # still tagged as external content
        assert len(asked) == 1 and "allow?" in asked[0]

    def test_y_is_yes(self, ws, monkeypatch):
        answer(monkeypatch, " Y ")
        [r] = agent._run_tools([call("read_file", {"path": ".env"})])
        assert SECRET in r

    @pytest.mark.parametrize("reply", ["masked", "mask", "m"])
    def test_masked_hides_values(self, ws, monkeypatch, reply):
        answer(monkeypatch, reply)
        [r] = agent._run_tools([call("read_file", {"path": ".env"})])
        assert SECRET not in r
        assert "API_SECRET=<set," in r

    def test_no_refuses(self, ws, monkeypatch):
        answer(monkeypatch, "no")
        [r] = agent._run_tools([call("read_file", {"path": ".env"})])
        assert "Cancelled by user" in r and SECRET not in r
        assert guardrails._env_grants == {}

    @pytest.mark.parametrize("reply", ["", "maybe", "nope", "ok", KeyboardInterrupt, EOFError])
    def test_anything_else_refuses(self, ws, monkeypatch, reply):
        answer(monkeypatch, reply)
        [r] = agent._run_tools([call("read_file", {"path": ".env"})])
        assert "Cancelled" in r and SECRET not in r
        assert guardrails._env_grants == {}

    def test_refusal_is_remembered_so_the_model_cannot_nag(self, ws, monkeypatch):
        asked = answer(monkeypatch, "no")
        agent._run_tools([call("read_file", {"path": ".env"})])
        [r] = agent._run_tools([call("read_file", {"path": ".env"})])
        assert len(asked) == 1                     # not asked a second time
        assert "Cancelled" in r and SECRET not in r

    def test_approval_is_single_use(self, ws, monkeypatch):
        asked = answer(monkeypatch, "yes", "no")
        [r1] = agent._run_tools([call("read_file", {"path": ".env"})])
        [r2] = agent._run_tools([call("read_file", {"path": ".env"})])
        assert SECRET in r1
        assert len(asked) == 2                     # asked again
        assert SECRET not in r2

    def test_approving_one_file_does_not_cover_another(self, ws, monkeypatch):
        (ws / ".env.local").write_text("OTHER=zzz\n", encoding="utf-8")
        asked = answer(monkeypatch, "yes", "no")
        r = agent._run_tools([call("read_file", {"path": ".env"}), call("read_file", {"path": ".env.local"})])
        assert SECRET in r[0]
        assert "zzz" not in r[1] and "Cancelled" in r[1]
        assert len(asked) == 2

    def test_normal_files_never_prompt(self, ws):
        [r] = agent._run_tools([call("read_file", {"path": "notes.txt"})])
        assert "harmless notes" in r

    def test_template_files_never_prompt(self, ws):
        [r] = agent._run_tools([call("read_file", {"path": ".env.example"})])
        assert "your_key_here" in r

    def test_other_tools_never_prompt(self, ws):
        [r] = agent._run_tools([call("list_dir", {"path": "."})])
        assert "notes.txt" in r

    def test_mixed_batch_prompts_once_and_keeps_order(self, ws, monkeypatch):
        asked = answer(monkeypatch, "masked")
        r = agent._run_tools([
            call("read_file", {"path": "notes.txt"}),
            call("read_file", {"path": ".env"}),
        ])
        assert "harmless notes" in r[0]
        assert "API_SECRET=<set," in r[1] and SECRET not in r[1]
        assert len(asked) == 1

    def test_shell_attempt_never_prompts_and_is_blocked(self, ws):
        [r] = agent._run_tools([call("run_shell", {"command": "cat .env", "sandbox": False})])
        assert "Blocked" in r and SECRET not in r

    def test_rate_limited_batch_does_not_prompt(self, ws, monkeypatch):
        monkeypatch.setattr(agent, "_session_tool_calls", agent._MAX_SESSION_TOOL_CALLS)
        [r] = agent._run_tools([call("read_file", {"path": ".env"})])
        assert "BLOCKED" in r and SECRET not in r

    def test_prompt_text_names_the_file_and_the_three_choices(self, ws, monkeypatch):
        printed = []
        monkeypatch.setattr(agent.console, "print", lambda *a, **k: printed.append(" ".join(map(str, a))))
        answer(monkeypatch, "no")
        agent._run_tools([call("read_file", {"path": ".env"})])
        text = "\n".join(printed)
        assert ".env" in text and "masked" in text and "full contents" in text and "refuse" in text

    def test_path_with_markup_characters_does_not_break_the_prompt(self, ws, monkeypatch):
        (ws / "[bold]").mkdir()
        (ws / "[bold]" / ".env").write_text("K=v\n", encoding="utf-8")
        answer(monkeypatch, "no")
        [r] = agent._run_tools([call("read_file", {"path": "[bold]/.env"})])
        assert "Cancelled" in r
