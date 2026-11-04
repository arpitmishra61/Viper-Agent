"""/sessions, /model-select and /apikey: the logic around the menus (the questionary screens are stubbed)."""

import json

import pytest
from cryptography.fernet import Fernet

import agent
import auth
import pickers


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "_SESSIONS_DIR", tmp_path / "sessions")
    key = Fernet.generate_key()
    monkeypatch.setattr(agent, "_get_or_create_key", lambda: key)
    monkeypatch.setattr(pickers, "interactive", lambda: True)
    monkeypatch.setattr(auth, "remember_choice", lambda p, m: remembered.append((p, m)))
    remembered.clear()
    monkeypatch.setattr("tools.multiagent.init", lambda client, model: None)


remembered: list = []


def _session(tmp_path, stamp, messages, provider="openrouter", model="m"):
    """Write a session file with a fixed timestamp name, the way _save_session does."""
    d = tmp_path / "sessions"
    d.mkdir(exist_ok=True)
    payload = json.dumps({"provider": provider, "model": model, "messages": messages}).encode()
    path = d / f"{stamp}.viper"
    path.write_bytes(Fernet(agent._get_or_create_key()).encrypt(payload))
    return path


def _talk(question="hello there"):
    return [
        {"role": "system", "content": "old prompt"},
        {"role": "user", "content": question},
        {"role": "assistant", "content": "hi"},
    ]


class TestListSessions:
    def test_newest_first_with_details(self, tmp_path):
        _session(tmp_path, "20260101_090000", _talk("first question"))
        _session(tmp_path, "20260301_120000", _talk("later question") + [{"role": "user", "content": "more"}], model="big")
        got = agent._list_sessions()
        assert [s["when"] for s in got] == ["2026-03-01 12:00", "2026-01-01 09:00"]
        assert got[0]["preview"] == "later question"
        assert got[0]["count"] == 3          # user, assistant, user (system is not counted)
        assert got[0]["model"] == "big"

    def test_skips_empty_unreadable_and_skill_preambles(self, tmp_path):
        _session(tmp_path, "20260101_000001", [{"role": "system", "content": "s"}])
        (tmp_path / "sessions" / "20260101_000002.viper").write_bytes(b"not a real session")
        _session(tmp_path, "20260101_000003", [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "[/skill debugging] playbook"},
            {"role": "assistant", "content": "Skill loaded."},
            {"role": "user", "content": "why does it crash?"},
        ])
        got = agent._list_sessions()
        assert len(got) == 1
        assert got[0]["preview"] == "why does it crash?"

    def test_limit_and_no_folder(self, tmp_path):
        assert agent._list_sessions() == []
        for i in range(5):
            _session(tmp_path, f"2026010{i}_000000", _talk())
        assert len(agent._list_sessions(limit=3)) == 3


class TestLoadSession:
    def _state(self, messages):
        return {"messages": messages, "provider": "openrouter", "model": "m", "client": object()}

    def test_replaces_chat_and_swaps_in_current_prompt(self, tmp_path):
        _session(tmp_path, "20260101_090000", _talk("old chat"), provider="ollama", model="llama")
        state = self._state([{"role": "system", "content": "current prompt"}])
        assert agent._load_into_state(state, agent._list_sessions()[0])
        assert state["messages"][0]["content"] != "old prompt"
        assert [m["role"] for m in state["messages"]] == ["system", "user", "assistant"]
        assert state["provider"] == "openrouter" and state["model"] == "m"   # keeps what you have keys for

    def test_saves_the_chat_you_are_leaving(self, tmp_path):
        _session(tmp_path, "20260101_090000", _talk("old chat"))
        current = [{"role": "system", "content": "s"}, {"role": "user", "content": "work in progress"}]
        state = self._state(current)
        agent._load_into_state(state, agent._list_sessions()[0])
        assert any("work in progress" == s["preview"] for s in agent._list_sessions())

    def test_an_untouched_chat_is_not_saved(self, tmp_path):
        _session(tmp_path, "20260101_090000", _talk("old chat"))
        state = self._state([{"role": "system", "content": "s"}])
        agent._load_into_state(state, agent._list_sessions()[0])
        assert len(list((tmp_path / "sessions").glob("*.viper"))) == 1

    def test_unreadable_session_changes_nothing(self, tmp_path):
        bad = tmp_path / "sessions"
        bad.mkdir()
        (bad / "20260101_090000.viper").write_bytes(b"garbage")
        state = self._state(_talk("keep me"))
        before = list(state["messages"])
        assert not agent._load_into_state(state, {"path": bad / "20260101_090000.viper", "when": "x", "count": 1})
        assert state["messages"] == before


class TestSlashWiring:
    def _state(self):
        return {"messages": [{"role": "system", "content": "s"}], "provider": "openrouter",
                "model": "openrouter/free", "client": "old-client"}

    def test_sessions_command_loads_the_pick(self, tmp_path, monkeypatch):
        _session(tmp_path, "20260101_090000", _talk("old chat"))
        monkeypatch.setattr(pickers, "pick_session", lambda sessions: sessions[0])
        state = self._state()
        agent._handle_slash("/sessions", state)
        assert [m["role"] for m in state["messages"]] == ["system", "user", "assistant"]

    def test_sessions_command_with_nothing_saved(self):
        state = self._state()
        agent._handle_slash("/sessions", state)
        assert len(state["messages"]) == 1

    def test_model_select_same_provider_only_changes_the_model(self, monkeypatch):
        monkeypatch.setattr(pickers, "pick_provider_model", lambda p, m: ("openrouter", "mistralai/x"))
        state = self._state()
        agent._handle_slash("/model-select", state)
        assert state["model"] == "mistralai/x" and state["client"] == "old-client"
        assert remembered[-1] == ("openrouter", "mistralai/x")

    def test_model_select_other_provider_builds_a_new_client(self, monkeypatch):
        monkeypatch.setattr(pickers, "pick_provider_model", lambda p, m: ("huggingface", "Qwen/Qwen2.5-7B-Instruct"))
        monkeypatch.setattr(pickers, "resolve_key", lambda provider: (True, "hf_test"))
        state = self._state()
        agent._handle_slash("/model-select", state)
        assert state["provider"] == "huggingface"
        assert state["model"] == "Qwen/Qwen2.5-7B-Instruct"
        assert state["client"] != "old-client"
        assert str(state["client"].base_url).startswith("https://router.huggingface.co")

    def test_model_select_without_a_key_stays_put(self, monkeypatch):
        monkeypatch.setattr(pickers, "pick_provider_model", lambda p, m: ("huggingface", "x"))
        monkeypatch.setattr(pickers, "resolve_key", lambda provider: (False, None))
        state = self._state()
        agent._handle_slash("/model-select", state)
        assert state["provider"] == "openrouter" and state["client"] == "old-client"

    def test_model_select_cancelled(self, monkeypatch):
        monkeypatch.setattr(pickers, "pick_provider_model", lambda p, m: None)
        state = self._state()
        agent._handle_slash("/model-select", state)
        assert state["model"] == "openrouter/free"

    def test_changing_the_active_providers_key_rebuilds_the_client(self, monkeypatch):
        monkeypatch.setattr(pickers, "manage_keys", lambda cb: cb("OPENROUTER_API_KEY", "sk-or-new"))
        state = self._state()
        agent._handle_slash("/apikey", state)
        assert state["client"] != "old-client"
        assert state["client"].api_key == "sk-or-new"

    def test_changing_another_providers_key_leaves_the_client_alone(self, monkeypatch):
        monkeypatch.setattr(pickers, "manage_keys", lambda cb: (cb("HF_TOKEN", "hf_new"), cb("E2B_API_KEY", "e2b_new")))
        state = self._state()
        agent._handle_slash("/apikey", state)
        assert state["client"] == "old-client"

    def test_removing_the_active_key_keeps_the_running_client(self, monkeypatch):
        monkeypatch.setattr(pickers, "manage_keys", lambda cb: cb("OPENROUTER_API_KEY", None))
        state = self._state()
        agent._handle_slash("/apikey", state)
        assert state["client"] == "old-client"

    @pytest.mark.parametrize("alias", ["/api", "/api key edit", "/api-key", "/keys"])
    def test_apikey_aliases(self, monkeypatch, alias):
        called = []
        monkeypatch.setattr(pickers, "manage_keys", lambda cb: called.append(1))
        agent._handle_slash(alias, self._state())
        assert called

    def test_menus_refuse_to_run_without_a_terminal(self, monkeypatch):
        monkeypatch.setattr(pickers, "interactive", lambda: False)
        monkeypatch.setattr(pickers, "manage_keys", lambda cb: pytest.fail("should not open"))
        monkeypatch.setattr(pickers, "pick_session", lambda s: pytest.fail("should not open"))
        monkeypatch.setattr(pickers, "pick_provider_model", lambda p, m: pytest.fail("should not open"))
        for cmd in ("/apikey", "/sessions", "/model-select"):
            agent._handle_slash(cmd, self._state())

    def test_model_by_name_still_works(self):
        state = self._state()
        agent._handle_slash("/model some/model", state)
        assert state["model"] == "some/model"


class TestPickerHelpers:
    def test_mask_key_never_shows_the_whole_key(self):
        assert pickers.mask_key("sk-or-v1-abcdef123456") == "sk-or...3456"
        assert "abcdef" not in pickers.mask_key("sk-or-v1-abcdef123456")
        assert pickers.mask_key("short") == "set (5 chars)"

    def test_openrouter_and_anthropic_share_one_key_slot(self):
        slots = {s["env"]: s for s in pickers.key_slots()}
        assert slots["OPENROUTER_API_KEY"]["providers"] == ["openrouter", "anthropic"]
        assert "HF_TOKEN" in slots and "E2B_API_KEY" in slots
        assert len(slots) == 3                 # ollama needs no key, so it has no slot

    def test_model_list_always_has_the_default_and_the_current_model(self, monkeypatch):
        monkeypatch.setattr(pickers, "_ollama_models", lambda url: [])
        assert pickers.models_for("openrouter")[0] == "openrouter/free"
        assert pickers.models_for("openrouter", current="custom/model")[0] == "custom/model"
        assert pickers.models_for("ollama") == ["llama3.2"]            # Ollama not running: fall back to the default

    def test_ollama_lists_what_is_installed(self, monkeypatch):
        monkeypatch.setattr(pickers, "_ollama_models", lambda url: ["qwen2.5:7b", "llama3.2"])
        assert pickers.models_for("ollama") == ["qwen2.5:7b", "llama3.2"]

    def test_offline_live_lists_are_empty_not_errors(self, monkeypatch):
        def boom(*a, **k):
            raise pickers.httpx.ConnectError("down")
        monkeypatch.setattr(pickers.httpx, "get", boom)
        assert pickers._openrouter_models() == []
        assert pickers._ollama_models("http://localhost:11434/v1") == []

    def test_resolve_key_prefers_environment_then_account_then_asks(self, monkeypatch):
        monkeypatch.setenv("HF_TOKEN", "from-env")
        assert pickers.resolve_key("huggingface") == (True, "from-env")
        monkeypatch.delenv("HF_TOKEN")
        monkeypatch.setattr(auth, "get_saved_key", lambda name: "from-account")
        assert pickers.resolve_key("huggingface") == (True, "from-account")
        monkeypatch.setattr(auth, "get_saved_key", lambda name: None)
        monkeypatch.setattr(pickers.onboarding, "ask_key", lambda provider, env: "typed")
        assert pickers.resolve_key("huggingface") == (True, "typed")
        monkeypatch.setattr(pickers.onboarding, "ask_key", lambda provider, env: None)
        assert pickers.resolve_key("huggingface") == (False, None)

    def test_providers_without_keys_need_nothing(self):
        assert pickers.resolve_key("ollama") == (True, None)
