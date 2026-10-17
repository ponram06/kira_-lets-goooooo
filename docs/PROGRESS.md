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

---

---

## M2. System and app tools — done (2026-10-17)

> **Note on commit `9b2bb3e`**: Commit `9b2bb3e` was labeled `wip: M1 in progress (Claude limit reached)` when Claude Code reached its token usage limit. In reality, Milestone 1 was already completed in commit `19f2d92`; commit `9b2bb3e` contained the initial implementation of Milestone 2 (system controls, persistent reminders, and countdown).

### What was done
- **System controls** (`backend/tools/system_control.py`):
  - `volume`: up, down, set, mute, unmute, get via pycaw; clamps percentages, handles errors gracefully.
  - `brightness`: up, down, set, get via screen_brightness_control; primary display, minimum 5% safety floor.
  - `lock_pc`: locks Windows session via `LockWorkStation` (MEDIUM risk, asks in Restricted, auto-allows in Free).
  - `take_screenshot`: captures virtual desktop via GDI/ctypes, encodes PNG via zlib, saves strictly into `workspace/<screenshots_dir>/` (MEDIUM risk, asks in Restricted, auto-allows in Free).
  - `system_info`: CPU %, RAM use/total, and battery status with charging state via psutil. Verified to return zero secrets.
- **Timers and reminders** (`backend/reminders.py`, `backend/tools/reminders_tool.py`):
  - Local scheduler storing `reminders.json` in user directory outside git.
  - Event loop sleep calculation without polling loops; wakes on changes.
  - Fires `reminder.due` events, triggers spoken read-back via `VoiceManager`, and records missed reminders across restarts.
  - Tools `set_timer`, `set_reminder`, `list_reminders`, `cancel_reminder` registered in `TOOL_REGISTRY` as LOW risk.
- **Agent countdown & free mode safety** (`backend/countdown.py`):
  - 3-second spoken and on-screen countdown with cancel for medium/high risk actions in Free mode.
- **Avatar Chat Box (Fallback Input)**:
  - Added chat input dock overlay to `avatar/src/renderer/index.html` and `avatar/src/renderer/style.css`.
  - Added "Chat…" item to avatar menu (`avatar/src/main/menu.js`). Opens **only** on explicit menu click (no auto-open, no Enter/Space trigger). Refused while Kira's state is "working".
  - Window temporarily becomes focusable only while the chat box is open, and reverts to `focusable: false` on send, Escape, or blur, ensuring active typing in VS Code is never interrupted.
  - Plain text only (`textContent` exclusively, no HTML rendering).
- **Backend & Avatar Protocol for `user_text`**:
  - `user_text` requires authenticated WebSocket session, is capped at 2000 characters, and is rate-limited (max 5/sec).
  - `async_shared_confirm` in `backend/voice.py` explicitly buffers `chat_box`/`user_text` so that chat text **never acts as an answer to an ask-first confirmation prompt** (only explicit `confirm` with the matching `question_id` or terminal/voice can answer).
- **Tests**:
  - Python: **270 passed, 1 skipped** (100% offline pass rate). 38 tests in `tests/test_system_tools.py`, covering mock hardware, policy enforcement, reminder lifecycle, countdown, screenshot workspace containment, system_info secret protection, and user_text confirmation isolation.
  - Electron: **58 passed** in `avatar/` (`tests/avatar.test.js` and `tests/controls.test.js`), covering chat menu state, working-state refusal, and user_text schema validation.

### Next milestone
M2B. Universal app launcher, app adapters and the any-task planner.

---

## M2B. Universal app launcher, app adapters and the any-task planner — done (2026-10-17)

### What was done
- **Any-Task Planner Specification** (`docs/ANY_TASK_PLANNER.md`):
  - Strict 5-tier fallback hierarchy: (1) App Adapter -> (2) Built-in Tool -> (3) Browser Automation (M4) -> (4) Sandboxed Script (M5) -> (5) Screen Control (M6).
  - Explicit architectural constraints: planner never lowers a policy decision; 15-step task limit then pause and ask; screen control strictly disabled on Ollama provider; zero typing of passwords, PINs, OTPs, or payment details; external content treated as untrusted data; spoken narration before each step; per-step verification and honest stuck state reporting. Screen control is documented only and not wired in M2B.
- **Read-Only Universal App Indexer** (`backend/app_index.py`):
  - Recursively scans Start Menu shortcuts (`.lnk`) from user AppData and system ProgramData.
  - Queries Windows Store / UWP apps off the event loop via `Get-StartApps` with a 15-second timeout.
  - Deduplicates user and system shortcuts by normalized target path and Store AppIDs.
  - Keeps previous cache on refresh failures. Refreshes once and retries before declaring an app not found.
  - Strict privacy: resolution happens 100% locally; the index is never sent to cloud models and is never logged in full (only counts).
- **Fuzzy Resolution & Ambiguity Safety**:
  - Exact match, alias match, and token-similarity matching via `difflib.SequenceMatcher`.
  - Confident matches read back name and launch.
  - Ambiguous matches (two close candidates) ask the user one candidate at a time with yes/no (never guessing).
  - Weak matches (< 0.60) offer at most 3 closest candidate suggestions and never guess an executable path.
- **Shortcut Security & Denylist Engine** (`backend/app_security.py`):
  - Resolves `.lnk` files via COM `IShellLink` / `IShellLinkDataList` extracting target path, arguments, and `SLDF_RUNAS_USER` flag.
  - Refuses scripts (`.bat`, `.cmd`, `.ps1`, `.vbs`, `.js`, etc.) and script interpreters (`mshta`, `wscript`, `cscript`, `rundll32`, `regsvr32`).
  - Refuses administrator elevation: if the Run As Administrator flag is set, launch is blocked to prevent UAC prompts.
  - Whole-word security denylist blocks command interpreters (`cmd`, `powershell`, `pwsh`, `wt`, `bash`, `wsl`), system editors (`regedit`, `mmc`, `diskpart`, `diskmgmt`), installer/uninstaller binaries (`setup`, `unins000`, `installer`), and display-name phrases ("Windows PowerShell", "Command Prompt", "Windows Terminal", "Registry Editor", "Disk Management") in both Restricted and Free modes.
- **First-Launch Approval & Tamper Detection** (`backend/app_approvals.py`):
  - Stored outside the repository in `%APPDATA%\Kira\app_approvals.json` (atomic write via tempfile/replace).
  - Keyed by resolved target path and arguments, recording size, modified time, and SHA-256 checksum at approval time.
  - Store apps keyed by validated AppID (strictly checked against invalid shell/traversal characters).
  - Re-checks path, size, mtime, and SHA-256 on every launch; if the target binary is changed or planted, approval is revoked and re-asked.
  - Never writable via `user_text` or LLM filesystem tools; writable only through confirmed authorization flow.
- **Policy Integration** (`backend/policy.py` & `backend/agent.py`):
  - `policy.check()` accepts an additive `approved` flag. Unapproved applications return `"ask"` in BOTH Restricted and Free modes.
  - Registered `open_any_app` (LOW risk, `requires_approval=True`) and `media_control` (LOW risk) in `TOOL_REGISTRY`.
- **App Adapters** (`backend/adapters/`):
  - Base interface `AppAdapter` with fixed action lists, parameter validation, and detection.
  - `generic`: General desktop/Store app launcher via `AppIndex`.
  - `spotify`: Desktop app launch and keyboard media keys (`play_pause`, `next`, `previous`, `volume_up`, `volume_down`) using virtual key codes (`keybd_event`). Verifies `Spotify.exe` is running first and reports `"play/pause sent"` (not guessing player state). No `spotify:search:` links used.
  - `whatsapp`: Desktop/Store app opener only; messaging actions explicitly rejected/deferred to Milestone 3. Does not register `whatsapp://` URL protocols.
- **Window & Process Verification**:
  - Polls for active window or running process with timeout. Confirms with `"Opened {name}."` or reports `"Command was sent to open {name}, but the window could not be confirmed."`.

### Tests
- Python: **286 passed, 1 skipped** (`.venv\Scripts\python.exe -m pytest -q`). 16 new tests in `tests/test_app_launcher.py`.
- Electron: **58 passed** (`cd avatar; npm test`).

### Next milestone
M3. Messaging (WhatsApp adapter with contact fuzzy resolution, ask-first confirmation, rate limiting, and foreground window verification).

---

## M3. Messaging (WhatsApp adapter) — done (2026-10-17)

### What was done
- **Contact Resolution & Phone Privacy Engine (`backend/contacts.py`)**:
  - Contacts file stored outside repository in `%APPDATA%\Kira\contacts.yaml` (overridable via `KIRA_USER_DIR`). Provided `contacts.example.yaml` in repo with fake numbers.
  - Model must provide only contact name. Raw phone number strings passed by the model are strictly refused with actionable error messages.
  - Validates international phone numbers under E.164.
  - Phone masking: all but the last 3 digits are masked (`+********567`). The full number is never displayed in prompts, spoken aloud, logged, or emitted in WebSocket events.
  - Case-insensitive exact matching and fuzzy token similarity matching (`difflib.SequenceMatcher`).
  - Ambiguous matches prompt the user one candidate at a time with yes/no; returns $\le 3$ suggestions; never guesses a number or leaks the full contact list to cloud LLMs.
- **Message Constraints & Provenance Guard (`backend/tools/whatsapp_tool.py`)**:
  - Message text is capped at 280 characters; rejected in full without silent truncation if exceeded.
  - Control characters and newlines are rejected.
  - Scanned for secret patterns using `secrets_guard.known_secrets()`.
  - **Provenance guard**: Refuses messages containing 20+ characters copied from prior tool outputs or file reads in the current task that were not in the user's prompt.
- **Persistent Rate Limiting & Approvals (`backend/whatsapp_rate_limit.py`)**:
  - Persisted in `%APPDATA%\Kira\whatsapp_rate_limit.json` with atomic writes via tempfile/replace. Corrupt or unreadable files fail closed (never reset counters).
  - Minimum gap (30s), hourly cap (5 messages), daily cap (20 messages).
  - First message to any contact in Free mode requires explicit user confirmation; subsequent sends to that contact use the 3-second countdown.
- **Foreground Verification & Honest Delivery Reporting**:
  - Enforces mandatory `request_id` (fail closed if missing or empty). Strictly one send allowed per request; zero retries by the agent loop on error or after Enter.
  - Launches deep link via `whatsapp://send` URI scheme (isolated; rejected in all other tools).
  - Verifies foreground window via `ctypes.windll.user32` (`GetForegroundWindow`, `GetWindowThreadProcessId`) matches WhatsApp process names (`whatsapp.exe`, `whatsappdesktop.exe`). Aborts without pressing Enter if not focused or if an error dialog is active.
  - Sends `VK_RETURN` via `keybd_event`. Default honest report: `"I pressed Enter. I can't verify delivery, check WhatsApp."`.
- **Policy & Adapter Integration**:
  - Registered `send_whatsapp` in `TOOL_REGISTRY` with `HIGH` risk, `affects_people=True`, `outside_workspace=False`.
  - Updated `WhatsAppAdapter` in `backend/adapters/whatsapp.py` to delegate `send_message` directly to `send_whatsapp`.
- **No new external packages** (uses standard library `ctypes`, `urllib.parse`, `difflib`, `json`, `yaml`).

### Tests
- Python: **309 passed, 1 skipped** (`.venv\Scripts\python.exe -m pytest -q`). 23 comprehensive offline tests in `tests/test_whatsapp.py`.
- Electron: **58 passed** (`cd avatar; npm test`).

### Next milestone
M4. Browser tasks (Playwright).

---

## M4. Browser tasks (Playwright) — done (2026-10-17)

### What was done
- **Pinned Dependency & Profile Isolation**:
  - Pinned `playwright==1.63.0` in `requirements.txt` (released 2026-09-15, >4 weeks old).
  - Clean browser profile stored outside git in `%APPDATA%\Kira\browser_profile` (or `%KIRA_USER_DIR%\browser_profile` for test isolation). Completely isolated from user's personal Chrome/Edge profiles, cookies, saved passwords, or extensions.
  - Headed visible Chromium window for real-time observation. Installed only Chromium to minimize disk and memory footprint.
- **Strict URL & IP Safety Engine (`backend/browser/url_guard.py`)**:
  - Enforces `https://` only (blocks `file://`, `chrome://`, `about:`, `data:`, `javascript:`, `blob:`, `whatsapp://`, and all other app protocols).
  - Port strictly 443 only (rejects non-443 ports like 8080, 8443, 80).
  - Blocks `user@host` authentication tricks, trailing-dot hosts, and internal domain suffixes (`.local`, `.internal`, `.localhost`, `.lan`, `.home`, `.corp`).
  - Blocks IP literals in all forms (decimal, hex, octal, IPv4-mapped IPv6, standard notation).
  - All IDN domains are converted to ASCII punycode (`xn--...`) in confirmation prompts and logs.
  - Pre-DNS resolution blocks private RFC 1918 ranges, loopback, link-local (`169.254.169.254`), multicast, and reserved addresses.
  - Route handler intercepts every request (main frame, subframes, resources).
- **DNS Rebinding Defence (`check_server_address`)**:
  - Inspects connected server IP from `response.server_addr()` after each response. If the IP is private, loopback, or link-local, closes the page immediately and halts the task.
- **Content Sanitization & Prompt Injection Containment (`backend/browser/dom_reader.py`)**:
  - Evaluates in-page DOM script extracting visible plain text. Removes `<script>`, `<style>`, `<noscript>`, `<svg>`, `<canvas>`, `<template>`, `<iframe`, and CSS-hidden elements (`display: none`, `visibility: hidden`, `opacity: 0`, `aria-hidden="true"`).
  - Strips zero-width characters, BiDi overrides, Unicode tag characters, and invisible controls.
  - Neutralizes closing tag lookalikes (`</UNTRUSTED_WEB_DATA>` $\to$ `&lt;/UNTRUSTED_WEB_DATA>`).
  - Wraps extracted text in unique per-call random-delimited `<UNTRUSTED_WEB_DATA_{delim}>` tags (capped at 4,000 characters).
  - System prompt and agent loop treat wrapped web content strictly as untrusted data, never as instructions.
- **Interaction Guard & Hardened Typing/Clicks (`backend/browser/interaction_guard.py`)**:
  - Detects and refuses typing into password, OTP/2FA, and payment/credit-card fields, instructing the user to type them manually.
  - Scans typed text against `secrets_guard.known_secrets()` (blocks secrets from `.env` or contacts).
  - Typing strictly uses Playwright's locator API (`locator.fill`), never presses Enter, and never uses OS-level typing.
  - In Restricted mode, any click on a `<button>`, `<input type="submit">`, or element inside a `<form>` asks confirmation first reading back the domain (punycode) and button text. High-risk keywords (submit, buy, post, delete, log in) ask first as backstop in both modes.
- **Taint Tracking & Provenance Guard**:
  - Once `browser_read` executes, the task is marked tainted by web content.
  - Subsequent `browser_open` calls to a new domain and all `browser_type` calls require explicit confirmation with fixed wording stating the action follows web content.
  - M3 provenance guard treats `browser_read` outputs as prior tool outputs, catching attempts to forward read text to WhatsApp.
- **Context Hardening & Restrictions**:
  - All permissions denied, service workers blocked, `accept_downloads=False`, file choosers cancelled, dialogs auto-dismissed, extra tabs/popups closed immediately (1-tab limit).
  - Browser tools fail closed when running on the local Ollama provider.
  - Kill switch halts all browser actions immediately.
  - Single browser instance closed cleanly on Kira shutdown via `atexit`.
- **Policy & Adapter Integration**:
  - Registered 8 browser tools in `TOOL_REGISTRY` (`RiskLevel.LOW`, zero other policy rules changed).
  - Implemented `BrowserAdapter` in `backend/adapters/browser.py` and registered in `backend/adapters/__init__.py`.

### Tests
- Python: **328 passed, 1 skipped** (`.venv\Scripts\python.exe -m pytest -q`). 19 new tests in `tests/test_browser.py`.
- Electron: **58 passed** (`cd avatar; npm test`).

### Next milestone
M5. Self-written scripts (run_script tool, workspace/scripts/ directory, static AST checks, sandbox options).

---

## Phase 0. Independent verification of M1–M4 (Claude Code, 2026-10-17)

M1–M4 were built by another assistant. Re-verified from scratch; earlier notes above were not trusted.

### Real results (before Phase 0 fixes)
- `git status`: clean except an uncommitted `.env.example` that contained a real PIN value (restored to the committed blank version; never committed).
- pytest: **328 passed, 1 skipped** (66.8 s). Electron: **58 passed**.
- `python -m backend.doctor`: **FAIL** on "Logs: a key or PIN appears in actions.jsonl, events.jsonl". Cause: the 4‑digit PIN matched digits inside timestamps (no standalone PIN in logs). Fixed (see below). Ollama: warn (not running).

### Honest status per milestone
| Milestone | Status | Notes |
|---|---|---|
| M1 App controls | tested with mocks + needs hand test | Tray, menus, dialogs, hotkeys, multi-monitor need a screen. |
| M2 System tools | tested with mocks + needs hand test | pycaw, brightness, lock, screenshot, reminders on real hardware untested. Chat box focus was broken on Windows (fixed, needs hand test). |
| M2B Launcher/adapters | **partial**, tested with mocks | AppIndex/denylist/approvals exist. Missing from spec: notepad, explorer, calculator, vscode adapters; Spotify search/play (`spotify:search:`) and Spotify→browser fallback; saving a contact by voice; planner "which path" sentence. |
| M3 WhatsApp | **partial**, tested with mocks | Missing: dry-run mode, local send log without text, `config.yaml` whatsapp limits not wired into the tool. Real send needs hand test. Was broken end‑to‑end through the agent (crash on logging `Mode`), fixed. |
| M4 Browser | **partial**, tested with mocks | Real Playwright never run in tests. Missing: configurable domain deny-list, pause-and-tell on login/CAPTCHA/payment pages. Also crashed through the agent (same logging bug), fixed. |

### Security review findings (scope: policy bypass, secret leaks, unconfirmed messages, wrong-window keystrokes)
| # | Severity | Finding | Status |
|---|---|---|---|
| S1 | HIGH | Agent forwarded **all** model JSON args as kwargs. A hostile model could pass `config_path` (own allow-list → `open_app("powershell")`, any site via `open_website`, LOW risk, no ask), `hourly_cap/daily_cap/min_gap_seconds` (WhatsApp rate-limit bypass), `visible=False`, `mode`, `timeout_seconds`. | **Fixed**: `agent.filter_tool_args` keeps only schema params; internals injected by the loop. Tests: `tests/test_phase0_security.py`. |
| S2 | HIGH | Free mode, first message to a contact: the confirmation did not read back the message text. | **Fixed**: prompt reads back contact, masked number and text. Test: `test_free_mode_first_contact_prompt_reads_back_message`. |
| S3 | MEDIUM | Full WhatsApp/browser text and internal args were written to `logs/actions.jsonl` and `tool.start` events (also crashed on `Mode`). | **Fixed**: logs/events get public args with private text replaced by its length. |
| S4 | MEDIUM | VS Code focus check accepted any window titled "<file> … code" (e.g. a browser tab); single-line typing checked focus once before a long `typewrite`. | **Fixed**: foreground process must be VS Code; focus re-checked before every 8-char chunk. |
| S5 | MEDIUM | Digit-only PIN redacted inside any digit run (timestamps) → mangled logs, PIN-digit oracle, doctor false alarm. | **Fixed**: whole-number matching for numeric secrets (`secrets_guard.secret_in_text`). |
| S6 | HIGH (user action) | The current Free-mode PIN appears in git history (old commit message `e297479` and the README of that era), and that history is already on `origin`. | **Open — you must change the PIN in Settings.** History not rewritten (destructive; your call). |
| S7 | LOW | Wrong-PIN lockout resets after 60 s: a 4-digit PIN can be brute-forced over the local authenticated WebSocket in about 55 h. | Open (suggest escalating lockout). |
| S8 | LOW | Browser confirmation prompts include page-derived button text (untrusted) verbatim. | Open. |
| S9 | LOW | `run_script` is registered MEDIUM + allowlisted → would auto-allow in Restricted. No implementation existed yet. | To fix in M5 (Restricted must ask). |
| S10 | INFO | Chat box: `setFocusable(true)`+`focus()` on a window shown inactive did not reliably activate it on Windows; the menu-close blur closed it. | **Fixed** (`avatar/src/main/chat-focus.js`, 8 node tests); needs hand test. A task starting now closes the chat. |

### After fixes
- pytest: **337 passed, 1 skipped**. Electron: **66 passed**. Doctor: ready (Ollama warning only).

### NEXT STEP
M5: implement `backend/tools/script_runner.py` (`run_script`), make Restricted ask / Free countdown in `policy.py`, tests in `tests/test_script_runner.py`.

---

## Overall Milestones Status Summary
- **M1. App controls**: Done (tray, menus, PIN-gated Free mode, settings window, doctor, secret redaction, lifecycle).
- **M2. System and app tools**: Done (system controls, reminders, countdown, policy, avatar chat box fallback, isolated confirmations).
- **M2B. Universal app launcher & adapters**: Done (AppIndex, safe lnk resolution, denylist, first-launch approvals, Spotify/WhatsApp adapters, planner architecture).
- **M3. Messaging (WhatsApp adapter)**: Done (contacts resolution, phone masking, provenance guard, rate limits, foreground check, single send).
- **M4. Browser tasks (Playwright)**: Done (headed mode, clean profile, IP safety, DNS rebinding defence, prompt injection guard, taint tracking, context hardening).
- **M5. Self-written scripts**: Not started (next).

