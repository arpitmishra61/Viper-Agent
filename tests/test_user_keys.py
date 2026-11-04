"""Saved API keys live in the Viper account (Supabase `user-keys` function), not on disk."""

import httpx
import pytest

import auth


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body if body is not None else {}, ""

    def json(self):
        return self._body


class _FakeBackend:
    """Stands in for the user-keys edge function."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.calls: list[tuple[str, dict, dict]] = []   # (url, body, headers)
        self.fail_set_for: set[str] = set()

    def post(self, url, json=None, headers=None, timeout=None, **_):
        if not url.endswith("/functions/v1/user-keys"):
            return _Resp(204)                            # e.g. /auth/v1/logout
        self.calls.append((url, json, headers))
        action, name = json["action"], json.get("name")
        if action == "list":
            return _Resp(200, {"names": sorted(self.store)})
        if action == "get":
            if name in self.store:
                return _Resp(200, {"value": self.store[name]})
            return _Resp(404, {"error": "not_found"})
        if action == "set":
            if name in self.fail_set_for:
                return _Resp(500, {"error": "db_error"})
            self.store[name] = json["value"]
            return _Resp(200, {"ok": True})
        if action == "delete":
            self.store.pop(name, None)
            return _Resp(200, {"ok": True})
        return _Resp(400, {"error": "bad_action"})


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / ".viper"
    monkeypatch.setattr(auth, "_HOME", home)
    monkeypatch.setattr(auth, "_KEY_PATH", home / ".key")
    monkeypatch.setattr(auth, "_PROFILE", home / "profile.viper")
    monkeypatch.setattr(auth, "_PROFILE_JS", home / "profile.json")
    monkeypatch.setattr(auth, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(auth, "SUPABASE_ANON_KEY", "anon")
    monkeypatch.setattr(auth, "AUTH_PAGE_URL", "https://auth.example/")
    auth._session_keys.clear()
    yield
    auth._session_keys.clear()


@pytest.fixture
def backend(monkeypatch):
    fake = _FakeBackend()
    monkeypatch.setattr(auth.httpx, "post", fake.post)
    return fake


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(auth, "current_session", lambda: {"email": "a@b.co", "access": "user-token"})
    monkeypatch.setattr(auth, "is_logged_in", lambda: True)


class TestGuest:
    def test_key_is_kept_for_the_run_only(self, backend):
        auth.save_key("OPENROUTER_API_KEY", "sk-1")
        assert auth.get_saved_key("OPENROUTER_API_KEY") == "sk-1"
        assert "OPENROUTER_API_KEY" in auth.saved_key_names()
        assert backend.calls == []                      # nothing sent anywhere
        assert not auth.keys_persist()
        assert "keys" not in auth.load_profile()        # nothing written to disk

    def test_unknown_key_is_none(self, backend):
        assert auth.get_saved_key("HF_TOKEN") is None


class TestSignedIn:
    def test_save_goes_to_the_account_with_the_user_token(self, backend, signed_in):
        auth.save_key("HF_TOKEN", "hf_1")
        url, body, headers = backend.calls[-1]
        assert url == "https://example.supabase.co/functions/v1/user-keys"
        assert body == {"action": "set", "name": "HF_TOKEN", "value": "hf_1"}
        assert headers["Authorization"] == "Bearer user-token"
        assert backend.store == {"HF_TOKEN": "hf_1"}
        assert "keys" not in auth.load_profile()
        assert auth.keys_persist()

    def test_key_comes_back_on_a_fresh_run(self, backend, signed_in):
        backend.store["OPENROUTER_API_KEY"] = "sk-remote"
        assert "OPENROUTER_API_KEY" in auth.saved_key_names()
        assert auth.get_saved_key("OPENROUTER_API_KEY") == "sk-remote"

    def test_missing_key_is_none_not_an_error(self, backend, signed_in):
        assert auth.get_saved_key("E2B_API_KEY") is None

    def test_function_not_deployed_is_reported(self, signed_in, monkeypatch):
        monkeypatch.setattr(
            auth.httpx, "post",
            lambda *a, **k: _Resp(404, {"code": "NOT_FOUND", "message": "Requested function was not found"}),
        )
        with pytest.raises(auth.AuthError):
            auth.get_saved_key("E2B_API_KEY")

    def test_secret_not_configured_has_a_clear_message(self, signed_in, monkeypatch):
        monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: _Resp(503, {"error": "keys_not_configured"}))
        with pytest.raises(auth.AuthError, match="KEYS_ENCRYPTION_SECRET"):
            auth.save_key("HF_TOKEN", "x")

    def test_failed_save_still_works_this_run(self, signed_in, monkeypatch):
        monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: _Resp(500, {"error": "db_error"}))
        with pytest.raises(auth.AuthError):
            auth.save_key("HF_TOKEN", "hf_1")
        assert auth.get_saved_key("HF_TOKEN") == "hf_1"

    def test_network_error_becomes_auth_error(self, signed_in, monkeypatch):
        def boom(*a, **k):
            raise httpx.ConnectError("down")
        monkeypatch.setattr(auth.httpx, "post", boom)
        with pytest.raises(auth.AuthError):
            auth.saved_key_names()


class TestMigration:
    def _local_keys(self, **keys):
        auth.save_profile({"email": "a@b.co", "keys": keys, "provider": "openrouter"})

    def test_local_keys_move_to_the_account(self, backend, signed_in):
        self._local_keys(OPENROUTER_API_KEY="sk-old", E2B_API_KEY="e2b_old")
        assert auth.migrate_local_keys() == 2
        assert backend.store == {"OPENROUTER_API_KEY": "sk-old", "E2B_API_KEY": "e2b_old"}
        profile = auth.load_profile()
        assert "keys" not in profile
        assert profile["provider"] == "openrouter"       # the rest of the profile stays

    def test_failed_ones_stay_on_disk(self, backend, signed_in):
        backend.fail_set_for = {"E2B_API_KEY"}
        self._local_keys(OPENROUTER_API_KEY="sk-old", E2B_API_KEY="e2b_old")
        with pytest.raises(auth.AuthError):
            auth.migrate_local_keys()
        assert auth.load_profile()["keys"] == {"E2B_API_KEY": "e2b_old"}
        assert backend.store == {"OPENROUTER_API_KEY": "sk-old"}

    def test_nothing_happens_when_signed_out(self, backend):
        self._local_keys(OPENROUTER_API_KEY="sk-old")
        assert auth.migrate_local_keys() == 0
        assert auth.load_profile()["keys"] == {"OPENROUTER_API_KEY": "sk-old"}
        assert backend.calls == []


class TestDelete:
    def test_removes_account_copy_and_run_cache(self, backend, signed_in):
        auth.save_key("HF_TOKEN", "hf_1")
        auth.delete_key("HF_TOKEN")
        assert backend.store == {}
        assert auth.get_saved_key("HF_TOKEN") is None

    def test_removes_a_leftover_local_copy(self, backend, signed_in):
        auth.save_profile({"email": "a@b.co", "keys": {"HF_TOKEN": "old", "E2B_API_KEY": "e"}})
        auth.delete_key("HF_TOKEN")
        assert auth.load_profile()["keys"] == {"E2B_API_KEY": "e"}

    def test_guest_delete_never_calls_the_backend(self, backend):
        auth.save_key("HF_TOKEN", "hf_1")
        auth.delete_key("HF_TOKEN")
        assert backend.calls == []
        assert auth.get_saved_key("HF_TOKEN") is None


class TestLogoutAndPrompt:
    def test_logout_keeps_account_keys_and_clears_the_run_cache(self, backend, signed_in):
        auth.save_key("HF_TOKEN", "hf_1")
        auth.save_profile({"email": "a@b.co", "tokens": {"access": "t", "refresh": "r", "expires_at": 0}})
        auth.logout()
        assert backend.store == {"HF_TOKEN": "hf_1"}     # never deleted server-side
        assert not any(c[1]["action"] == "delete" for c in backend.calls)
        assert auth._session_keys == {}
        assert auth.load_profile() == {}

    def test_e2b_prompt_is_asked_once(self):
        assert not auth.e2b_prompt_done()
        auth.mark_e2b_prompt_done()
        assert auth.e2b_prompt_done()
