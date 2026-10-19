"""Tests for encrypted session save/load in agent.py."""

import json

import pytest
from cryptography.fernet import Fernet, InvalidToken

import agent

MESSAGES = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "my secret is hunter2-UNIQUE-MARKER"},
    {"role": "assistant", "content": "ok"},
]


@pytest.fixture(autouse=True)
def _isolated_sessions(tmp_path, monkeypatch):
    """Point sessions at a tmp dir and use a throwaway key — never touch ~/.viper."""
    monkeypatch.setattr(agent, "_SESSIONS_DIR", tmp_path / "sessions")
    key = Fernet.generate_key()
    monkeypatch.setattr(agent, "_get_or_create_key", lambda: key)
    return key


def test_save_creates_encrypted_viper_file():
    path = agent._save_session(MESSAGES, "openrouter", "m")
    assert path.suffix == ".viper"
    assert path.exists()


def test_saved_file_is_not_plaintext():
    path = agent._save_session(MESSAGES, "openrouter", "m")
    raw = path.read_bytes()
    assert b"UNIQUE-MARKER" not in raw
    assert b"hunter2" not in raw


def test_round_trip():
    agent._save_session(MESSAGES, "openrouter", "some-model")
    data = agent._load_last_session()
    assert data == {"provider": "openrouter", "model": "some-model", "messages": MESSAGES}


def test_load_when_no_sessions_dir():
    assert agent._load_last_session() is None


def test_load_when_dir_empty():
    agent._SESSIONS_DIR.mkdir(parents=True)
    assert agent._load_last_session() is None


def test_latest_session_wins():
    d = agent._SESSIONS_DIR
    d.mkdir(parents=True)
    f = Fernet(agent._get_or_create_key())
    (d / "20250101_000000.viper").write_bytes(f.encrypt(json.dumps({"model": "old"}).encode()))
    (d / "20250202_000000.viper").write_bytes(f.encrypt(json.dumps({"model": "new"}).encode()))
    assert agent._load_last_session()["model"] == "new"


def test_legacy_plain_json_fallback():
    d = agent._SESSIONS_DIR
    d.mkdir(parents=True)
    (d / "20240101_000000.json").write_text(json.dumps({"model": "legacy"}), encoding="utf-8")
    assert agent._load_last_session()["model"] == "legacy"


def test_encrypted_preferred_over_legacy():
    d = agent._SESSIONS_DIR
    d.mkdir(parents=True)
    (d / "20990101_000000.json").write_text(json.dumps({"model": "legacy"}), encoding="utf-8")
    agent._save_session(MESSAGES, "p", "encrypted")
    assert agent._load_last_session()["model"] == "encrypted"


def test_wrong_key_cannot_decrypt(monkeypatch):
    agent._save_session(MESSAGES, "p", "m")
    monkeypatch.setattr(agent, "_get_or_create_key", lambda: Fernet.generate_key())
    with pytest.raises(InvalidToken):
        agent._load_last_session()
