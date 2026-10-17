"""
settings_cli.py
===============
Back end of the Settings window. The Electron app runs

    python -m backend.settings_cli <action>

with a JSON object on stdin and reads one JSON object from stdout.
Secrets travel only through stdin (never argv) and are never printed,
logged or echoed back; key fields come back masked.

Actions
-------
get        current settings (keys masked, PIN only as set / not set)
set        save keys + PIN to .env and other choices to user settings
test-key   one cheap request to a provider to check its key
doctor     run backend.doctor checks
list-mics  input devices for the microphone picker

Key and model fields are built from the provider list in config.yaml, so
adding a provider there needs no UI change.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import Any, Callable, Optional

from backend import envfile, secrets_guard, user_config
from backend.config_loader import is_overridable, load_config

PIN_ENV = secrets_guard.PIN_ENV


def _providers(cfg: dict) -> list[dict]:
    return [p for p in cfg.get("providers", []) if p.get("name")]


def _key_envs(cfg: dict) -> set[str]:
    """Env names the Settings window may write: provider keys + the PIN."""
    return {p["api_key_env"] for p in _providers(cfg) if p.get("api_key_env")} | {PIN_ENV}


def valid_pin(pin: str) -> bool:
    return pin.isdigit() and 4 <= len(pin) <= 12


# ── get ──────────────────────────────────────────────────────────────────────

def action_get(_payload: dict) -> dict:
    cfg = load_config()
    saved = user_config.load_settings()
    voice = cfg.get("voice", {})
    persona_cfg = cfg.get("persona", {})
    providers = []
    for p in _providers(cfg):
        env = p.get("api_key_env")
        value = envfile.get_value(env) if env else ""
        providers.append({
            "name": p["name"],
            "api_key_env": env,          # null → no key needed (e.g. Ollama)
            "key_set": bool(value),
            "key_masked": envfile.mask_key(value),
            "model": p.get("model", ""),
        })
    return {
        "ok": True,
        "providers": providers,
        "pin_set": bool(envfile.get_value(PIN_ENV)),
        "persona": persona_cfg.get("name", "kira"),
        "personas": list((persona_cfg.get("available_personas") or {}).keys()),
        "mic_muted": bool(saved.get("mic_muted", False)),
        "voice": {
            "hotkey": voice.get("hotkey", "`"),
            "edge_voice": voice.get("tts", {}).get("edge_voice", ""),
            "input_device": voice.get("input_device"),
        },
        "free_revert_timeout": int(cfg.get("mode", {}).get("free_revert_timeout", 600)),
    }


# ── set ──────────────────────────────────────────────────────────────────────

def action_set(payload: dict) -> dict:
    """
    payload = {keys: {ENV: value}, pin: str, models: {provider: model},
               voice: {hotkey, edge_voice, input_device}, persona: str,
               free_revert_timeout: int}
    Every field is optional; empty key / PIN strings mean "leave unchanged",
    ``clear_keys`` lists env names to remove. Errors name the field, never the value.
    """
    cfg = load_config()
    errors: list[str] = []
    env_updates: dict[str, str] = {}
    allowed_envs = _key_envs(cfg)

    for env, value in (payload.get("keys") or {}).items():
        if env not in allowed_envs or env == PIN_ENV:
            errors.append(f"{env}: not a configurable key")
            continue
        if not isinstance(value, str) or not value:
            continue
        try:
            envfile.validate_value(value)
            env_updates[env] = value
        except ValueError as exc:
            errors.append(f"{env}: {exc}")

    for env in payload.get("clear_keys") or []:
        if env in allowed_envs:
            env_updates[env] = ""

    pin = payload.get("pin")
    if isinstance(pin, str) and pin:
        if valid_pin(pin):
            env_updates[PIN_ENV] = pin
        else:
            errors.append("PIN: must be 4 to 12 digits")
    pin = None

    overrides: dict[str, Any] = {}
    known = {p["name"] for p in _providers(cfg)}
    for name, model in (payload.get("models") or {}).items():
        path = f"providers.{name}.model"
        if name in known and is_overridable(path, model):
            overrides[path] = model
        else:
            errors.append(f"model for {name}: invalid")

    voice = payload.get("voice") or {}
    for field, path in (("hotkey", "voice.hotkey"), ("edge_voice", "voice.tts.edge_voice"),
                        ("input_device", "voice.input_device")):
        if field in voice:
            if is_overridable(path, voice[field]):
                overrides[path] = voice[field]
            else:
                errors.append(f"{field}: invalid")

    if "free_revert_timeout" in payload:
        if is_overridable("mode.free_revert_timeout", payload["free_revert_timeout"]):
            overrides["mode.free_revert_timeout"] = payload["free_revert_timeout"]
        else:
            errors.append("free_revert_timeout: must be 60 to 86400 seconds")

    persona = payload.get("persona")
    personas = (cfg.get("persona", {}).get("available_personas") or {}).keys()
    if persona is not None and persona not in personas:
        errors.append("persona: unknown")
        persona = None

    if errors:
        return {"ok": False, "errors": errors}

    if env_updates:
        envfile.set_values(env_updates)
        secrets_guard.refresh()
    changes: dict[str, Any] = {}
    if overrides:
        changes["overrides"] = overrides
    if persona:
        changes["persona"] = persona
    if changes:
        user_config.update_settings(**changes)

    restart_paths = {p for p in overrides if p != "mode.free_revert_timeout"}
    needs_restart = bool(restart_paths) or any(e != PIN_ENV for e in env_updates)
    return {"ok": True, "needs_restart": needs_restart}


# ── test-key ─────────────────────────────────────────────────────────────────

def _http_get(url: str, headers: dict, timeout: float) -> int:
    import httpx
    return httpx.get(url, headers=headers, timeout=timeout).status_code


def action_test_key(payload: dict, http_get: Callable[[str, dict, float], int] = _http_get) -> dict:
    """payload = {provider: name, key?: unsaved value}. Never returns the key."""
    cfg = load_config()
    prov = next((p for p in _providers(cfg) if p["name"] == payload.get("provider")), None)
    if prov is None:
        return {"ok": False, "message": "Unknown provider."}
    timeout = float((cfg.get("doctor") or {}).get("timeout", 4)) + 4
    base = str(prov.get("base_url", "")).rstrip("/")
    env = prov.get("api_key_env")

    if not env:
        # Local provider (Ollama): check that it runs and the model is pulled
        from backend.doctor import Probe, check_ollama
        result = check_ollama(Probe(), {"providers": [prov]}, timeout)
        return {"ok": result.status == "ok", "message": result.detail}

    key = payload.get("key") or envfile.get_value(env)
    if not isinstance(key, str) or not key:
        return {"ok": False, "message": "No key entered or saved."}
    url = base + str(prov.get("key_test_path", "/models"))
    try:
        status = http_get(url, {"Authorization": f"Bearer {key}"}, timeout)
    except Exception as exc:
        return {"ok": False, "message": f"Could not reach {prov['name']} ({type(exc).__name__})."}
    finally:
        key = None
    if 200 <= status < 300:
        return {"ok": True, "message": "Key works."}
    if status in (401, 403):
        return {"ok": False, "message": "Key rejected (unauthorized)."}
    if status == 429:
        return {"ok": True, "message": "Key accepted but rate-limited right now."}
    return {"ok": False, "message": f"Unexpected response (HTTP {status})."}


# ── doctor / mics ────────────────────────────────────────────────────────────

def action_doctor(_payload: dict) -> dict:
    from backend.doctor import has_required_failure, run_checks
    checks = run_checks()
    return {"ok": not has_required_failure(checks), "checks": [asdict(c) for c in checks]}


def action_list_mics(_payload: dict) -> dict:
    from backend.voice import list_input_devices
    return {"ok": True, "mics": list_input_devices()}


ACTIONS: dict[str, Callable[[dict], dict]] = {
    "get": action_get,
    "set": action_set,
    "test-key": action_test_key,
    "doctor": action_doctor,
    "list-mics": action_list_mics,
}


def run(action: str, raw_input: str) -> dict:
    handler = ACTIONS.get(action)
    if handler is None:
        return {"ok": False, "errors": ["unknown action"]}
    try:
        payload = json.loads(raw_input) if raw_input.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        return {"ok": False, "errors": ["input must be a JSON object"]}
    try:
        return handler(payload)
    except Exception as exc:
        # Type only: exception text could contain a value
        return {"ok": False, "errors": [f"internal error ({type(exc).__name__})"]}


def main(argv: Optional[list[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    result = run(args[0] if args else "", sys.stdin.read() if args else "")
    sys.stdout.write(secrets_guard.redact(json.dumps(result)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
