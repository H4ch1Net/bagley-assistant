from __future__ import annotations

import os

import pytest

from bagley.config import ServerConfig, load_dotenv, normalize_base_url, resolve_preferences


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "http://localhost:11434"),
        ("0.0.0.0", "http://127.0.0.1:11434"),
        ("myhost:1234", "http://myhost:1234"),
        ("http://box", "http://box"),
        ("https://openrouter.ai/api/v1/", "https://openrouter.ai/api/v1"),
    ],
)
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


def test_env_locks_preferences_and_bad_stored_values_are_dropped():
    prefs, locked = resolve_preferences(
        {"model": "stored", "temperature": 9, "persona": "concise", "bogus": 1},
        {"BAGLEY_MODEL": "from-env", "BAGLEY_THINK": "yes"},
    )
    assert prefs.model == "from-env"
    assert prefs.think is True
    assert prefs.temperature == 0.6  # Invalid stored value ignored.
    assert prefs.persona == "concise"
    assert locked == {"model", "think"}


def test_ollama_host_is_a_default_not_a_lock():
    prefs, locked = resolve_preferences({}, {"OLLAMA_HOST": "0.0.0.0:11434"})
    assert prefs.base_url == "http://127.0.0.1:11434"
    assert not locked


def test_invalid_env_value_exits():
    with pytest.raises(SystemExit, match="BAGLEY_TEMPERATURE"):
        resolve_preferences({}, {"BAGLEY_TEMPERATURE": "hot"})


def test_load_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        '# comment\nBAGLEY_T1="quoted value"\nexport BAGLEY_T2=plain # trailing\nBAGLEY_T3=keep\n'
    )
    monkeypatch.setenv("BAGLEY_T3", "existing")
    load_dotenv(env)
    assert os.environ["BAGLEY_T1"] == "quoted value"
    assert os.environ["BAGLEY_T2"] == "plain"
    assert os.environ["BAGLEY_T3"] == "existing"
    for key in ("BAGLEY_T1", "BAGLEY_T2"):
        monkeypatch.delenv(key)


def test_server_config_from_env(tmp_path):
    cfg = ServerConfig.from_env(
        {"BAGLEY_DATA_DIR": str(tmp_path), "BAGLEY_PORT": "9000", "BAGLEY_ENABLE_SHELL": "1"}
    )
    assert cfg.port == 9000
    assert cfg.enable_shell
    assert cfg.workspace == tmp_path / "workspace"
    assert cfg.is_loopback
