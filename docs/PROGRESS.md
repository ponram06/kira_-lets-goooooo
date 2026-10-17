# Progress Log

One entry per milestone from docs/MASTER_PROMPT.md. Newest at the bottom.

---

## M1. App controls — done (2026-10-17)

### What was done
- **Tray icon**: show/hide avatar, mute/unmute mic, persona, settings, stop current task, quit.
- **Avatar menu**: opened from a hover ⋯ button (fades after 2 s) or by right-clicking the character. It has mute, sleep/wake, persona, mode, size (small/medium/large), lock position, action feed toggle, settings, stop task and quit. Click-through stays on everywhere except the character and the button.
- **Free mode from the menu**: a confirmation dialog with a masked PIN field. The PIN travels only renderer → main → `free_mode_request` over the token-authenticated WebSocket → `ModeManager.try_enter_free`. After 3 wrong PINs, every attempt is refused for 60 s (configurable). Auto-revert works as before. Free mode can't be entered by voice or terminal.
- **PIN moved out of config.yaml** into `.env` as `KIRA_FREE_MODE_PIN` (`.env.example` has only an empty placeholder). It is read fresh on each attempt, so a PIN changed in Settings works without a restart. If it is missing, Free mode is disabled and the doctor says so. Attempted PINs are no longer logged (`policy.py`) or printed (`brain.py`). Spoken/typed PIN entry and `extract_pin_digits` were removed, and the old PIN value is gone from code, tests and README (tests use the fake `TEST_PIN = "0000"`).
- **Secret redaction** (`backend/secrets_guard.py`): every `.env` value (keys, PIN) is replaced with `[REDACTED]` in log records (including tracebacks), stdout/stderr, event text/data, `logs/events.jsonl` and WebSocket payloads.
- **Backend lifecycle**: new `python -m backend.main [--headless]`. The avatar app starts the backend if it isn't already running, logging to `logs/backend.log`. Quit sends `shutdown` → waits up to 5 s → `taskkill /T /F`. A backend started with `run.bat` is only asked to shut down. Smoke-tested on this machine: clean exit in about 2 s, code 0, token file removed.
- **Live controls over WebSocket**: `set_persona`, `set_mic_muted`, `set_sleep`, `get_status`, `reload_settings`, `shutdown`, `free_mode_request`, with `request_id` acks. New events: `persona.change`, `mic.mute`, `sleep.change`, `free_mode.denied`, `app.shutdown`, plus forwarding of `policy.decision` (block/countdown), `tool.error` and `kill_switch.activated` for the action feed.
- **Sleep is separate from mute**: sleep dims the avatar and drops to 5 FPS; push-to-talk still works and wakes it.
- **User config outside the repo** (`%APPDATA%\Kira\`, or `KIRA_USER_DIR`):
  - `avatar.json`: position, size, lock, feed, start-with-Windows (default off), show/hide hotkey (default Ctrl+Alt+H)
  - `settings.json`: persona, mic mute, whitelisted overrides of config.yaml

  Mode is never saved. An off-screen position resets to bottom-right.
- **Settings window** (closing only hides it): provider key and model fields are built from `config.yaml` providers (Groq, OpenRouter, Ollama today) with a Test button each. It also has the masked PIN field, Free-mode timeout, microphone list, Edge voice, push-to-talk key, persona, start with Windows, show/hide hotkey, a Doctor panel and Restart backend. Secrets go to `backend.settings_cli` through stdin only.
- **Doctor** (`python -m backend.doctor [--json]`) checks:
  - Python version and packages
  - the `code` command
  - Ollama running and the model pulled
  - the microphone
  - which keys and the PIN are set (presence only)
  - that `.env` and `logs/` are git-ignored
  - that no key or PIN appears in `logs/`
  - internet access

  Every failure includes a fix.
- **No new packages.**

### Tests
- Python: **229 passed, 1 skipped** (`.venv\Scripts\python.exe -m pytest -q`). New files: `test_app_controls.py`, `test_settings.py`, `test_doctor.py`, `test_no_secret_leaks.py`. The policy and voice tests were updated for the PIN-only Free mode and lockout.
- Electron: **56 passed** (`cd avatar; npm test`). New file: `tests/controls.test.js`.
- The new leak test found one real gap during development (secrets inside exception tracebacks). It is fixed.

### Known gaps
- These can't be automated and need the manual checklist in the README "M1 manual checklist": tray, native menus, hover fade, dialogs, start with Windows, the global hotkey, and multi-monitor behaviour.
- Click-through uses the character's rectangle (as before), not exact pixels. Pixel-accurate hit testing is planned for M8.
- Changing keys, models, microphone or the push-to-talk key needs "Restart backend". Persona, mic mute, PIN and Free-mode timeout apply live.
- `logs/backend.log` isn't rotated yet (planned for M10).
- `docs/MASTER_PROMPT.md` mentions the first name "Meldan" as a test contact (a name only, no number).

### Next milestone
M2. System and app tools.
