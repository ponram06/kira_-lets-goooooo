"""
M1 settings: .env editing, user settings file, config overrides whitelist,
and the settings_cli used by the Settings window. All offline.
"""

import json
import os

import pytest

from backend import envfile, settings_cli, user_config
from backend.config_loader import apply_overrides, load_config
from tests.conftest import TEST_PIN

FAKE_KEY = "gsk_FAKE_test_key_1234567890abcd"


# ── envfile ─────────────────────────────────────────────────────────────────

def test_set_values_keeps_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nGROQ_API_KEY=old\nOTHER=keep\n", encoding="utf-8")
    envfile.set_values({"GROQ_API_KEY": "new", "ADDED": "x"}, env)
    text = env.read_text(encoding="utf-8")
    assert "# comment" in text and "OTHER=keep" in text
    assert "GROQ_API_KEY=new" in text and "ADDED=x" in text and "old" not in text


def test_empty_value_removes_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A_KEY=1\nB=2\n", encoding="utf-8")
    envfile.set_values({"A_KEY": ""}, env)
    assert envfile.read_env(env) == {"B": "2"}


@pytest.mark.parametrize("bad", ["two\nlines", "cr\rhere", " padded", "x" * 600])
def test_rejects_unsafe_values(tmp_path, bad):
    with pytest.raises(ValueError):
        envfile.set_values({"GROQ_API_KEY": bad}, tmp_path / ".env")


def test_rejects_bad_names(tmp_path):
    with pytest.raises(ValueError):
        envfile.set_values({"lower=case": "v"}, tmp_path / ".env")


def test_mask_key_shows_only_last_four():
    assert envfile.mask_key(FAKE_KEY) == "••••abcd"
    assert envfile.mask_key("short") == "••••"
    assert envfile.mask_key("") == ""


def test_get_value_prefers_file(monkeypatch):
    envfile.set_values({"GROQ_API_KEY": "fromfile"})
    monkeypatch.setenv("GROQ_API_KEY", "fromenv")
    assert envfile.get_value("GROQ_API_KEY") == "fromfile"


# ── user settings ───────────────────────────────────────────────────────────

def test_user_settings_round_trip_and_sanitize():
    user_config.save_settings({"persona": "Ryuk", "mic_muted": True, "mode": "free", "junk": 1})
    assert user_config.load_settings() == {"persona": "ryuk", "mic_muted": True}


def test_user_dir_outside_repo():
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    assert not str(user_config.user_dir()).startswith(repo)


def test_corrupt_settings_file_gives_defaults():
    user_config.user_dir(create=True)
    user_config.settings_path().write_text("{not json", encoding="utf-8")
    assert user_config.load_settings() == {}


# ── config overrides ────────────────────────────────────────────────────────

def test_only_whitelisted_overrides_apply():
    cfg = load_config(use_user_settings=False)
    merged = apply_overrides(cfg, {"overrides": {
        "providers.groq.model": "new-model",
        "voice.hotkey": "f8",
        "mode.free_revert_timeout": 120,
        "server.host": "0.0.0.0",                  # not overridable
        "tools.allowed_apps": "regedit",           # not overridable
        "mode.free_revert_timeout_typo": 5,
    }})
    groq = next(p for p in merged["providers"] if p["name"] == "groq")
    assert groq["model"] == "new-model"
    assert merged["voice"]["hotkey"] == "f8"
    assert merged["mode"]["free_revert_timeout"] == 120
    assert merged["server"]["host"] == "127.0.0.1"
    assert "regedit" not in merged["tools"]["allowed_apps"]


def test_bad_override_values_ignored():
    cfg = load_config(use_user_settings=False)
    merged = apply_overrides(cfg, {"overrides": {"mode.free_revert_timeout": 5, "voice.hotkey": "a\nb"}})
    assert merged["mode"]["free_revert_timeout"] == cfg["mode"]["free_revert_timeout"]
    assert merged["voice"]["hotkey"] == cfg["voice"]["hotkey"]


def test_pin_not_in_config_yaml():
    cfg = load_config(use_user_settings=False)
    assert "free_mode_pin" not in cfg.get("mode", {})


# ── settings_cli ────────────────────────────────────────────────────────────

def test_get_builds_fields_from_provider_list():
    envfile.set_values({"GROQ_API_KEY": FAKE_KEY})
    out = settings_cli.action_get({})
    names = [p["name"] for p in out["providers"]]
    cfg_names = [p["name"] for p in load_config()["providers"]]
    assert names == cfg_names                    # nothing hardcoded (no Gemini unless configured)
    groq = next(p for p in out["providers"] if p["name"] == "groq")
    assert groq["key_set"] and groq["key_masked"] == "••••abcd"
    assert FAKE_KEY not in json.dumps(out)
    assert out["pin_set"] is False


def test_set_saves_keys_pin_and_overrides():
    out = settings_cli.action_set({
        "keys": {"GROQ_API_KEY": FAKE_KEY},
        "pin": TEST_PIN,
        "models": {"groq": "m-1"},
        "voice": {"hotkey": "f9"},
        "persona": "ryuk",
        "free_revert_timeout": 300,
    })
    assert out == {"ok": True, "needs_restart": True}
    env = envfile.read_env()
    assert env["GROQ_API_KEY"] == FAKE_KEY and env["KIRA_FREE_MODE_PIN"] == TEST_PIN
    saved = user_config.load_settings()
    assert saved["persona"] == "ryuk"
    assert saved["overrides"]["providers.groq.model"] == "m-1"
    assert "KIRA_FREE_MODE_PIN" not in json.dumps(saved) and TEST_PIN not in json.dumps(saved)


def test_pin_change_alone_needs_no_restart():
    assert settings_cli.action_set({"pin": "4321"}) == {"ok": True, "needs_restart": False}


@pytest.mark.parametrize("pin", ["98", "abcd", "9876543210987", "98 76"])
def test_invalid_pin_rejected_without_echo(pin):
    out = settings_cli.action_set({"pin": pin})
    assert out["ok"] is False and pin not in json.dumps(out)
    assert "KIRA_FREE_MODE_PIN" not in envfile.read_env()


def test_unknown_env_name_refused():
    out = settings_cli.action_set({"keys": {"PATH": "C:\\evil"}})
    assert out["ok"] is False
    assert "PATH" not in envfile.read_env()


def test_pin_cannot_be_set_through_keys_field():
    out = settings_cli.action_set({"keys": {"KIRA_FREE_MODE_PIN": "9"}})
    assert out["ok"] is False


def test_test_key_ok_and_rejected_never_returns_key():
    calls = []

    def fake_get(url, headers, timeout):
        calls.append(url)
        return 200 if headers["Authorization"].endswith(FAKE_KEY) else 401

    ok = settings_cli.action_test_key({"provider": "groq", "key": FAKE_KEY}, http_get=fake_get)
    bad = settings_cli.action_test_key({"provider": "groq", "key": "wrong-key-000000"}, http_get=fake_get)
    assert ok["ok"] is True and bad["ok"] is False
    assert FAKE_KEY not in json.dumps(ok) + json.dumps(bad)
    assert calls[0].endswith("/models")


def test_test_key_uses_configured_path_for_openrouter():
    seen = []
    settings_cli.action_test_key({"provider": "openrouter", "key": FAKE_KEY},
                                 http_get=lambda u, h, t: seen.append(u) or 200)
    assert seen[0].endswith("/key")


def test_test_key_network_error():
    def boom(url, headers, timeout):
        raise ConnectionError(f"failed {headers}")   # even if the error text had the key
    out = settings_cli.action_test_key({"provider": "groq", "key": FAKE_KEY}, http_get=boom)
    assert out["ok"] is False and FAKE_KEY not in out["message"]


def test_run_rejects_bad_input():
    assert settings_cli.run("get", "[1,2]")["ok"] is False
    assert settings_cli.run("rm -rf", "{}")["ok"] is False


def test_cli_main_output_redacted(capsys, monkeypatch):
    envfile.set_values({"GROQ_API_KEY": FAKE_KEY})
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    settings_cli.main(["get"])
    assert FAKE_KEY not in capsys.readouterr().out
