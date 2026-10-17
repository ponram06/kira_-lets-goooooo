# Kira — AI Desktop Assistant (Steps 1–7 + M1 App Controls)

Autonomous desktop assistant prototype with brain core, safety policy layer, tool suite, agent loop, real-time voice pipeline (Ears & Mouth), and WebSocket server for desktop avatar streaming.

---

## Project Layout

```
kira/
├── backend/
│   ├── __init__.py
│   ├── main.py           # Entry point: python -m backend.main [--headless]
│   ├── providers.py      # LLM provider manager (ordered fallback + cooldowns)
│   ├── brain.py          # Chat loop (typed + voice PTT input + WebSocket server)
│   ├── app_state.py      # M1: live persona / mic mute / sleep / shutdown
│   ├── config_loader.py  # M1: config.yaml + user settings (whitelisted overrides)
│   ├── user_config.py    # M1: per-user settings.json (outside the repo)
│   ├── envfile.py        # M1: safe .env read/update + key masking
│   ├── secrets_guard.py  # M1: redacts keys and the PIN from logs, events, stdout, WebSocket
│   ├── settings_cli.py   # M1: JSON helper used by the Settings window
│   ├── doctor.py         # M1: python -m backend.doctor
│   ├── server.py         # Step 6: WebSocket server for live avatar feed & control
│   ├── policy.py         # Permission layer (check / ModeManager / workspace guard)
│   ├── audit.py          # Audit logging (logs/actions.jsonl) & live event feed
│   ├── events.py         # In-process pub/sub event bus & JSONL action logger
│   ├── agent.py          # Agent loop: Plan -> Act -> Observe -> Verify -> Report
│   ├── voice.py          # Ears (Groq Whisper / faster-whisper) & Mouth (Edge TTS / pyttsx3)
│   └── tools/            # Concrete tool implementations & schemas
│       ├── __init__.py   # Tool registry and OpenAI function calling schemas
│       ├── filesystem.py # create_folder, create_file (workspace/ only)
│       ├── execution.py  # run_command (allow-listed commands, visible CMD)
│       ├── system.py     # open_app, open_website (allow-lists)
│       ├── vscode_visible.py # Visible VS Code typing with active window guard
│       └── voice_stub.py # speak (TTS narrator routed to VoiceManager)
├── scripts/
│   └── ws_client.py      # Interactive test client to watch feed in terminal 2
├── logs/                 # events.jsonl & actions.jsonl (git-ignored)
├── workspace/            # The ONLY directory tools are permitted to modify
├── tests/
│   ├── conftest.py
│   ├── test_providers.py
│   ├── test_policy.py
│   ├── test_tools.py     # Unit tests for tools & allow-lists
│   ├── test_agent.py     # End-to-end tests for agent loop & demo scenario
│   ├── test_voice.py     # Step 4 unit tests (STT/TTS/safety/cooldowns/hotkeys)
│   ├── test_vscode_visible.py # Visible VS Code typing tests
│   ├── test_events.py    # Event bus pub/sub tests
│   └── test_server.py    # Step 6 WebSocket server tests (17 passed)
├── .env.example
├── .gitignore            # Audio, temp files, .kira/ tokens ignored
├── config.yaml           # Non-secret settings, models, hotkeys, server & allow-lists
├── pytest.ini
├── requirements.txt
└── README.md
```

---

## Quick Setup

Run all commands from `d:\kira\`.

### 1. Create and activate the virtual environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

> If PowerShell blocks scripts, run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Pre-download local fallback Whisper model (Optional but Recommended)

Pre-caches the `faster-whisper` "small" model for offline STT fallback:

```powershell
python -m backend.voice --prepare
```

### 4. Configure API keys

```powershell
Copy-Item .env.example .env
notepad .env
```

Add your keys (`GROQ_API_KEY` for hosted Whisper STT and LLM, `OPENROUTER_API_KEY`, etc.).  
Ollama and faster-whisper run locally without an API key.

Set `KIRA_FREE_MODE_PIN` (4–12 digits, your own choice) if you want Free mode.
Without it, Free mode stays disabled. You can also set keys and the PIN later in
the avatar's **Settings** window.

### 5. Check your setup

```powershell
.venv\Scripts\python.exe -m backend.doctor
```

---

## Running Kira

### Unified Chat & Voice REPL

```powershell
.venv\Scripts\python.exe -m backend.main
```

(`run.bat` does the same.) The avatar app (`cd avatar; npm start`) starts the
backend by itself in headless mode if it is not already running.

Kira always starts in **Restricted mode**.

#### Voice Controls
* **Push-to-Talk (PTT)**: Hold **`` ` ``** (backtick, configurable in `config.yaml`) to speak. Release to transcribe and send.
* **Global Emergency Kill Hotkey**: Press **`Ctrl+Alt+K`** at any time to instantly halt all audio playback, tool execution, and active loops. Operates globally even if speech recognition is down.
* **Stop Word**: Saying **`"stop"`** or **`"Kira stop"`** immediately halts speech and activates the kill switch locally before sending to the LLM.
* **Mode Commands by Voice**:
  * Saying `"restricted mode"` immediately switches to Restricted mode (Apple turns **green**).
  * Saying `"free mode"` does **not** switch modes. Kira tells you to use the avatar menu (**Mode → Free…**), which asks for your PIN in a masked dialog. The PIN is never spoken, typed in the terminal, sent to speech recognition or logged.

#### Built-in Terminal Commands
* `restricted mode` — Switch immediately to Restricted mode (Apple glow is green).
* `free mode` — Explains how to enter Free mode (avatar menu → Mode → Free…, PIN dialog).
* `stop` — Activate the global **Kill Switch** (blocks all tool actions and audio instantly).
* `resume` / `reset` — Reset the Kill Switch.
* `exit` or `quit` — Close the REPL (terminal mode only; in the avatar app use **Quit**).

---

## Voice Pipeline Architecture (Step 4)

### Ears (STT):
1. **Groq Hosted Whisper**: Uses `whisper-large-v3-turbo` with `response_format="verbose_json"` for sub-second cloud transcription.
2. **Local Fallback**: Automatically falls back to `faster-whisper` ("small" model) if Groq is unavailable, on cooldown, or offline.
3. **Independent STT Cooldowns**: Separate cooldown tracker from chat LLM providers (rate-limit: 15m, quota: 1h, timeout: 1m). Never logs API keys.
4. **Quality & Hallucination Guard**: Uses `avg_logprob` (threshold: -1.0) and `no_speech_prob` (threshold: 0.6). Filters known silence hallucinations (`"Thank you."`, `"Thanks for watching"`).
5. **Tap Gating**: Recordings under 0.3s are ignored. Recordings are capped at 30.0s. Zero audio files saved to disk (100% in-memory WAV processing).

### Mouth (TTS):
1. **Edge TTS Primary**: Streams cloud MP3 audio and decodes directly in memory to raw PCM using **`miniaudio`** (pure C extension, no ffmpeg required).
2. **pyttsx3 Fallback**: Runs in a dedicated worker thread with a fresh engine per call, utilizing word-level callbacks for sub-second interruption.
3. **Sentence Pipelining & Overlap**: Splits LLM reply into sentences and overlaps synthesis of sentence $N+1$ while sentence $N$ is actively playing through `sounddevice`.
4. **Natural Speech Filtering**: Automatically replaces fenced code blocks with `"The code is displayed on your screen."`, strips raw URLs, markdown asterisks, blockquotes, and hashes. Preserves decimals (`3.14`) and abbreviations (`e.g.`, `i.e.`, `vs.`, `etc.`).
5. **Latency Reporting**: Measures and logs the latency from end of speech to first spoken word with a target under 3 seconds:
   `[Voice latency]: End of speech to first spoken word: 0.85s (target <3s)`

### Policy & Confirmation Gate:
* When an action requires confirmation (`check()` returns `"ask"`):
  1. Kira reads back the action and arguments out loud via TTS.
  2. A **10-second timer begins strictly after Kira finishes speaking**.
  3. A shared race allows either a typed `"yes"` or a spoken `"yes"` via PTT.
  4. Only a clear `"yes"` counts; anything else or a timeout defaults to `"no"` (action declined).

---

## Manual Test Script (Step 4 Verification)

To manually verify the complete voice pipeline on Windows:

### Test 1: Push-to-Talk Speech & Narration
1. Run `.venv\Scripts\python.exe -m backend.main`.
2. Hold **`F8`** (or configured hotkey) and say: `"What is the capital of France?"`.
3. Release **`F8`**.
4. **Verify**:
   - Terminal prints `\nYou [voice]: What is the capital of France?`
   - Terminal displays `[Voice latency]: End of speech to first spoken word: ... (<3s)`.
   - Kira answers out loud via Edge TTS.

### Test 2: Instant Interruption via PTT or Stop Word
1. Ask Kira a long question (e.g. `"Explain how gravity works in 5 sentences"`).
2. While Kira is speaking, press **`F8`** or say **`"stop"`**.
3. **Verify**: Audio playback stops immediately within a fraction of a second. Saying `"stop"` activates the kill switch.

### Test 3: Emergency Kill Hotkey (Ctrl+Alt+K)
1. At any point, press **`Ctrl+Alt+K`**.
2. **Verify**: Audio halts immediately and terminal prints:
   `[EMERGENCY KILL SWITCH: Ctrl+Alt+K pressed. All actions and audio halted.]`

### Test 4: Free Mode & PIN Protection
1. Set `KIRA_FREE_MODE_PIN` in Settings (or `.env`).
2. Right-click the avatar → **Mode → Free… (asks for PIN)**.
3. **Verify**: a dialog explains Free mode and asks for the PIN in a masked field.
4. Enter a wrong PIN three times. **Verify**: "Too many wrong PINs. Try again in 60 s."; even the right PIN is refused during the lockout.
5. After the lockout, enter the right PIN. **Verify**: the apple turns red and Free mode auto-reverts after the configured timeout.
6. Say or type `"restricted mode"`. Kira switches back immediately.

### Test 5: Non-blocking Spoken Read-Back & Confirmation
1. Ask Kira to perform an action outside allowlist requiring confirmation (e.g., delete a file).
2. **Verify**: Kira reads back the confirmation request out loud. The 10-second timer begins strictly after speech finishes without freezing the terminal.
3. Hold **`` ` ``** and say: `"yes"` (or type `yes` in the terminal).
4. **Verify**: The action is approved and proceeds immediately without timing out.

---

## Canonical Demo Flow

Run the automated one-click demo for the canonical `AGENTS.md` scenario:

```powershell
python -m backend.demo
```

### Scenario Executed:
> *"Open VS Code, create a folder called demo, make hello.py with hello world, and run it."*

### What you will observe on screen:
1. **Workspace Setup**: Kira creates `workspace/demo/` and empty `workspace/demo/hello.py`.
2. **VS Code Launch & Focus**: VS Code opens on `workspace/demo/` and focuses the window.
3. **Visible Typing & Security Guard**:
   - Push-to-talk microphone is paused during typing.
   - Foreground window title is verified to match VS Code and `hello.py` before every keystroke batch.
   - Code `print('Hello, World!')` is typed visibly into the editor.
   - `Ctrl+S` saves the file and disk contents are verified.
4. **Visible CMD Execution**:
   - A standalone Windows CMD window opens in `workspace/demo/`.
   - `python hello.py` runs with output visible in the console.
   - Pauses 5 seconds for observation with formatted Kira status banners.
5. **Narration & Result**:
   - Kira speaks out loud via Edge TTS:
     `"Here is the output in the terminal: Hello, World!"`

---

## Event Bus (`backend/events.py`)

Kira features an in-process, thread-safe pub/sub event bus:
- **Event Types**: `agent.step_start`, `agent.step_done`, `tool.start`, `tool.done`, `policy.decision`, `mode.change`, `voice.listening`, `voice.processing`, `voice.speaking`, `voice.done`.
- **Zero Secrets**: Sensitive keys (`key`, `token`, `secret`, `password`) are automatically stripped.
- **Replay Logging**: All events are appended to `logs/events.jsonl` (covered in `.gitignore`).
- **Coexistence**: Runs alongside `backend/audit.py` without breaking existing action logs.

---

## WebSocket Server & Live Avatar Action Feed (`backend/server.py`)

Step 6 provides a lightweight, pure-asyncio WebSocket server for live action feed streaming and desktop avatar integration:

### Security & Architecture
- **Local Loopback Only**: Binds strictly to `127.0.0.1` on port `8765` (configurable in `config.yaml`).
- **Ephemeral Authentication Token**:
  - Generated at server startup via `secrets.token_urlsafe(32)`.
  - Stored outside `workspace/` at `.kira/token` (never committed, git-ignored).
  - Automatically deleted on server exit (`atexit`).
  - Auth token is **never in the URL**. Sent as the client's first JSON message (`{"type": "auth", "token": "..."}`) within 5 seconds. Tokens compared with `secrets.compare_digest`.
- **Host & Origin Validation**:
  - `Host` header must match `127.0.0.1:8765` or `localhost:8765`.
  - Web browser HTTP/HTTPS origins are **unconditionally rejected**.
  - Local origins (`"null"`, `file://`, `vscode-webview://`, `electron://`) and non-browser clients require valid token authentication.
- **Outbound Whitelisted Schema**:
  - Designates exact fields per event type (`state`, `step_start`, `step_done`, `policy_ask`, `mode_change`, `speech`, `loudness`).
  - Summaries truncated (<=120 chars), phone numbers masked (`[PHONE]`), zero raw files or secrets.
  - Normalized audio loudness (`0.0` to `1.0` float) throttled to ~20/sec for lip-sync animation (never raw audio).
- **Inbound Commands**:
  - `stop`: Triggers `KILL_SWITCH.stop()` and halts speech immediately.
  - `confirm`: Submits `yes` or `no` using the pending `question_id`. First answer across voice, typing, and socket wins.
  - `restricted_mode`: Switches mode back to Restricted.
  - `free_mode`: Explicitly refused over WebSocket.
- **Resilience**:
  - Runs in a dedicated background thread with its own `asyncio` event loop so terminal `input()` and long tool executions never block the server.
  - Outbound bounded queues (50 msgs max): slow consumers are dropped without stalling Kira.
  - Inbound rate limits: max 20 msgs/sec per client, max 4 KB frame size.
  - Graceful degradation: If port is occupied or `server.enabled: false`, logs a warning and Kira continues.

### Manual Verification with Test Client (`scripts/ws_client.py`)

1. In **Terminal 1**, start Kira:
   ```powershell
   .venv\Scripts\python.exe -m backend.main
   ```
2. In **Terminal 2**, run the test client:
   ```powershell
   python scripts/ws_client.py
   ```
3. What you will see:
   - Initial authentication handshake and state snapshot:
     ```text
     [SNAPSHOT] State: IDLE | Mode: RESTRICTED
     ```
   - Hold **`F8`** (or configured hotkey) in Terminal 1 or speak:
     ```text
     [STATE] -> LISTENING
     [STATE] -> THINKING
     [STATE] -> SPEAKING
     [SPEECH] Hello! How can I assist with your code?
     [LOUDNESS] [########-------] 0.54
     ```
   - In Terminal 2, type `stop` to trigger the kill switch.
   - When a risky action prompts for confirmation, type `yes <question_id>` in Terminal 2 to approve.

---

## Desktop Avatar Window (`avatar/`, Electron)

Step 7 implements a lightweight, transparent 2D desktop avatar window featuring Ryuk (or custom sprite) with responsive animations driven by the Kira WebSocket event stream.

### Architecture & Security Highlights
- **Transparent & Frameless**: Electron frameless window with `transparent: true`, `alwaysOnTop: true`, and `skipTaskbar: true`.
- **Zero Focus Stealing**: Configured with `focusable: false` and displayed via `showInactive()`. Dragging the character or clicking the speech bubble **never interrupts active typing** in VS Code or terminals.
- **Dynamic Click-Through**: Transparent pixel regions use `setIgnoreMouseEvents(true, { forward: true })`. Hovering over interactive elements (hover menu, speech bubble, or the solid character body via precomputed 1-bit alpha hit-mask) immediately activates click capture.
- **Main-Process WebSocket Isolation**: The WebSocket connection runs exclusively in the Electron **Main Process**, re-reading `.kira/token` from disk on each connection attempt with exponential backoff and jitter. The renderer never sees credentials or auth tokens; events pass strictly over a hardened, context-isolated IPC bridge.
- **2D Canvas Renderer**: 30 FPS capped canvas loop rendering breathing, leaning, apple/eye radial glows, and mouth bobbing synchronized to backend speech loudness.
- **Speech Bubble**: Dark rounded speech bubble with thin red border and monospace typography. Enforces text-only output (`textContent`, strictly no `innerHTML`), 140-character maximum truncation with ellipsis, and a 3-second auto-fade timer.
- **Mode Indicator Ring**: Displays mode aura (Blue for `RESTRICTED`, Red for `FREE`). Character maintains its native amber/red glow and is never tinted blue.
- **System Tray, Avatar Menu & Kill Switch**: see **M1 App Controls** below.

### Custom Character Assets & Glow Configuration
- Put your character sprite at `avatar/assets/ryuk.png` (git-ignored). If absent, a neutral geometric placeholder is rendered.
- Optional speaking sprite: `avatar/assets/ryuk_mouth_open.png` (swapped automatically when loudness exceeds threshold).
- Tune eye and apple radial glow positions and colors in `avatar/avatar.config.json` without modifying code.

### Installation & Dependency Verification
```powershell
cd avatar
npm ci
npm audit
```

### Running Unit Tests
```powershell
cd avatar
npm test
```
Runs 22 pure unit tests covering the state machine, loudness smoothing, schema compliance, multi-monitor bounds clamping, speech bubble sanitization, and alpha hit-mask generation via Node.js native `node:test`.

### Quick Manual Testing with Mock Server
To test all avatar animations and interactions in isolation without running LLMs:

1. In **Terminal 1**, start the mock WebSocket server:
   ```powershell
   .venv\Scripts\python.exe scripts/mock_server.py
   ```
2. In **Terminal 2**, launch the Electron Avatar:
   ```powershell
   cd avatar
   npm start
   ```
3. Observe:
   - Initial greeting bubble appears from `avatar.config.json` and auto-fades after 3 seconds.
   - Character smoothly transitions through states: `idle` (breathing) -> `listening` (forward tilt, brighter eye glow) -> `thinking` (apple glow pulse) -> `working` (fast apple pulse and sway) -> `speaking` (speech bubble appears, character bobs with audio loudness).
   - Hover the character: a small ⋯ button appears and fades about 2 s after you leave. Click it (or right-click the character) and choose **Stop task**; observe immediate kill switch activation in Terminal 1.
   - Drag the character to any screen corner or secondary monitor. Release to verify bounds clamping.
   - Click outside the character in an open editor window to verify clicks pass through seamlessly.

### Running with Full Kira Voice Assistant
```powershell
cd avatar
npm start
```
The avatar starts the backend (`python -m backend.main --headless`, output in
`logs/backend.log`). To see the backend in a terminal instead, start `run.bat`
first; the avatar then connects to it and on Quit only asks it to shut down.

---

## M1 App Controls

### Tray icon
Show/hide avatar · Mute/unmute microphone · Persona (Kira / Ryuk) · Settings… ·
Stop current task · Quit Kira. Left-click the tray icon to show/hide.

### Avatar menu (hover ⋯ button or right-click the character)
Mute · Sleep/Wake · Persona · Mode (Restricted at once; **Free…** opens the PIN
dialog) · Size (small / medium / large) · Lock position · Show action feed ·
Settings… · Stop task · Quit Kira.

- **Click-through**: only the character and the ⋯ button take the mouse.
- **Sleep is not mute**: sleep dims the avatar (renders at 5 FPS); push-to-talk
  still works and wakes it. Mute is a separate switch.
- **Action feed**: last 4 events (tool started ▶ / done ✓ / asks ? / blocked ⛔ /
  countdown ⏱ / errors ✗) in the bottom-left corner.
- **Quit** asks the backend to shut down, waits up to 5 s
  (`backend.shutdown_timeout_ms` in `avatar/avatar.config.json`), then ends the
  whole process tree, so no stray `python.exe` stays behind.

### What is remembered
Per-user folder `%APPDATA%\Kira\` (override with the `KIRA_USER_DIR` variable),
never inside the repo:

| File | Written by | Contents |
|------|-----------|----------|
| `avatar.json` | avatar app | position, size, lock, feed visible, start with Windows (default off), show/hide hotkey (default `Ctrl+Alt+H`) |
| `settings.json` | backend | persona, mic mute, your overrides of config.yaml (models, microphone, voice, push-to-talk key, Free-mode timeout) |

The permission **mode is never saved**: every launch starts Restricted. If the
saved position is off every connected screen, the avatar resets to bottom-right.

### Settings window
Opened from the tray or avatar menu. Closing it only hides it.
- Key and model fields are built from `providers:` in `config.yaml` (add a
  provider there and it shows up with no UI change). Each has a **Test** button
  (OpenRouter uses `key_test_path: "/key"` because its `/models` is public).
- Masked Free-mode PIN field, Free-mode timeout, microphone, Edge voice,
  push-to-talk key, persona, start with Windows, show/hide hotkey, **Doctor**.
- Keys and the PIN are written only to the local `.env`, passed to the helper
  through stdin (never command-line arguments), and never logged. A changed PIN
  works immediately; key, model, microphone and push-to-talk changes need
  **Restart backend**.

### Doctor
```powershell
.venv\Scripts\python.exe -m backend.doctor          # report; exit code 1 on a required failure
.venv\Scripts\python.exe -m backend.doctor --json   # used by Settings
```
Checks Python 3.11+, packages from requirements.txt, the `code` command,
Ollama and `qwen2.5:7b`, the microphone, provider keys and `KIRA_FREE_MODE_PIN`
(presence only, never printed), that `.env` and `logs/` are git-ignored, that no
key or PIN appears in `logs/`, and internet access.

### Secret protection
`backend/secrets_guard.py` knows every value in `.env` (keys and the PIN) and
replaces them with `[REDACTED]` in log records (including tracebacks), printed
output, event text and data, `logs/events.jsonl` and every WebSocket payload.
`tests/test_no_secret_leaks.py` proves it.

### M1 manual checklist (what automated tests cannot see)
1. `cd avatar; npm start` → avatar appears bottom-right, tray icon appears, `logs/backend.log` fills.
2. Hover the character → ⋯ appears, fades ~2 s after leaving. Click empty window areas → clicks reach the app below.
3. Right-click the character → avatar menu. Try Size → Large, Lock position (dragging stops), hide/show action feed.
4. Tray → Persona → Ryuk; the next reply uses Ryuk's tone. Tray → Mute microphone; hold push-to-talk → "Microphone is muted".
5. Avatar menu → Sleep → avatar dims; hold push-to-talk → it wakes and listens.
6. Mode → Free… with no PIN set → item disabled ("set a PIN in Settings first"). Set a PIN in Settings → try wrong and right PINs.
7. Settings → Test each key, Run checks; close the window → avatar and backend keep running.
8. Press `Ctrl+Alt+H` → avatar hides/shows. Tick "Start Kira with Windows", sign out and in → Kira starts.
9. Drag the avatar, Quit, `npm start` again → same position and size, mode Restricted.
10. Quit → Task Manager shows no `python.exe` from `.venv`.

---

## M2. System and App Tools

Kira includes local system controls, persistent timers/reminders, and a fallback chat box in the avatar window.

### Tool Table & Risk Levels

| Tool | Risk Level | Restricted Mode | Free Mode | Description |
| :--- | :--- | :--- | :--- | :--- |
| `volume` | **LOW** | Allow | Allow | Speaker volume control (`up`, `down`, `set`, `mute`, `unmute`, `get`) via `pycaw`. |
| `brightness` | **LOW** | Allow | Allow | Main display brightness (`up`, `down`, `set`, `get`) with a safe 5% floor via `screen_brightness_control`. |
| `system_info` | **LOW** | Allow | Allow | Reports CPU %, RAM use/total, and battery state via `psutil`. Never returns or logs secrets. |
| `lock_pc` | **MEDIUM** | Ask first | Allow | Locks Windows session immediately via `LockWorkStation`. |
| `take_screenshot` | **MEDIUM** | Ask first | Allow | Captures all displays via GDI/ctypes, encodes PNG in-memory, and writes strictly inside `workspace/screenshots/`. |
| `set_timer` | **LOW** | Allow | Allow | Local countdown timer announced out loud and visually when complete. |
| `set_reminder` | **LOW** | Allow | Allow | Persistent reminder scheduled at a local time (`HH:MM` or `YYYY-MM-DD HH:MM`). Survives assistant restarts. |
| `list_reminders` | **LOW** | Allow | Allow | Lists active timers and reminders with their IDs. |
| `cancel_reminder` | **LOW** | Allow | Allow | Cancels a pending timer or reminder by ID. |
| `open_any_app` | **LOW** | Ask first (first launch) | Ask first (first launch) | Universal launcher for Start Menu and Store apps. Resolves locally via `AppIndex`. Requires first-launch approval in both modes. |
| `media_control` | **LOW** | Allow | Allow | Keyboard media control (`open`, `play_pause`, `next`, `previous`, `volume_up`, `volume_down`) for supported apps (Spotify). |
| `send_whatsapp` | **HIGH** | Ask first | Countdown (3s) | WhatsApp messaging tool. Resolves contact name against `%APPDATA%\Kira\contacts.yaml` (never accepts phone numbers from model). Spoken read-back of contact name, masked number, and full message. Verifies WhatsApp foreground window before pressing Enter keystroke. Rates limited persistently. First send to contact in Free mode requires confirmation. |
| `browser_open` | **LOW** | Allow | Allow | Opens an HTTPS website in the headed browser. Enforces port 443, checks private IP ranges, blocks non-HTTPS schemes, and intercepts redirects. Taint rule: opens to new domains after `browser_read` ask first. |
| `browser_search` | **LOW** | Allow | Allow | Web search using fixed search engine template (`duckduckgo.com`). Query length capped and scanned for secrets. |
| `browser_read` | **LOW** | Allow | Allow | Extracts visible page text, strips scripts, styles, hidden elements, and Unicode tags. Wraps output in random-delimited `<UNTRUSTED_WEB_DATA_{delim}>` block (max 4,000 chars). Sets task taint flag. |
| `browser_click` | **LOW / HIGH** | Ask first (form/buttons) | Ask first (high-risk) | Clicks an element by visible text or role. In Restricted mode, any button, submit input, or form element asks first with domain (punycode) and button text. High-risk keywords (submit, buy, post, delete, log in) ask first in both modes. |
| `browser_type` | **LOW** | Allow | Allow | Types text into input fields by label or placeholder using Playwright locator (never presses Enter, no OS typing). Strictly refuses password, OTP, and payment fields. Taint rule: all typing asks first after `browser_read`. |
| `browser_scroll` | **LOW** | Allow | Allow | Scrolls the active page viewport up or down by pixel amount. |
| `browser_back` | **LOW** | Allow | Allow | Navigates back in browser history. |
| `browser_close` | **LOW** | Allow | Allow | Closes active browser page and context cleanly. |

### M2B. Universal App Launcher, Adapters & Any-Task Planner

- **Universal App Indexer (`backend/app_index.py`)**:
  - Scans user and system Start Menu `.lnk` shortcuts and Store apps (`Get-StartApps`).
  - Read-only, 100% local resolution. Full app list is never sent to cloud models or dumped into logs.
  - Fuzzy matching with ambiguity detection (asks one candidate at a time with yes/no; never guesses).
- **Shortcut & Denylist Security (`backend/app_security.py`)**:
  - Extracts target path, arguments, and `SLDF_RUNAS_USER` run-as administrator flag.
  - Refuses scripts (`.bat`, `.cmd`, `.ps1`, `.vbs`, `.js`, etc.) and script interpreters (`mshta`, `wscript`, `cscript`, `rundll32`).
  - Blocks executables requiring administrator elevation to prevent UAC prompts.
  - Whole-word security denylist blocks command interpreters (`cmd`, `powershell`, `pwsh`, `wt`, `bash`, `wsl`), system consoles (`regedit`, `mmc`, `diskpart`), and installer/uninstaller binaries.
- **First-Launch Approval (`backend/app_approvals.py`)**:
  - Requires user approval on first launch in **both** Restricted and Free modes.
  - Remembers target path, arguments, and SHA-256 in `%APPDATA%\Kira\app_approvals.json` (outside git). If the binary changes, approval is re-requested.
  - Approval cannot be written via `user_text` or filesystem tools.
- **App Adapters (`backend/adapters/`)**:
  - Fixed action lists with parameter validation.
  - `spotify`: Desktop launch and virtual media keys (`play_pause`, `next`, `previous`, `volume_up`, `volume_down`). Confirms process is running first and reports `"play/pause sent"`.
  - `whatsapp`: Desktop opener and message adapter delegating to `send_whatsapp`.
- **Any-Task Planner Specification (`docs/ANY_TASK_PLANNER.md`)**:
  - 5-stage fallback hierarchy: Adapter $\to$ Built-in Tool $\to$ Browser (M4) $\to$ Script (M5) $\to$ Screen Control (M6).
  - 15-step task limit, zero policy lowering, screen control disabled on Ollama, zero password typing, read content is data.

### M3. Messaging & WhatsApp Integration

- **Contact Resolution & Phone Privacy (`backend/contacts.py`)**:
  - Contacts stored outside the repository in `%APPDATA%\Kira\contacts.yaml` (`contacts.example.yaml` in repo with fake numbers).
  - Model must provide only contact name. Any raw phone number string passed by the model is strictly rejected.
  - Phone numbers validated under E.164. All but the last 3 digits are masked (`+********567`). The full number is never displayed in prompts, spoken aloud, logged, or emitted in WebSocket events.
  - Case-insensitive exact and fuzzy matching. Ambiguous matches ask one candidate at a time with yes/no; returns $\le 3$ suggestions; never guesses.
- **Message Constraints & Provenance Guard (`backend/tools/whatsapp_tool.py`)**:
  - Capped at 280 characters with full text presented in confirmation prompts (no truncation).
  - Control characters and newlines are rejected.
  - Scanned for secret patterns using `secrets_guard`.
  - **Provenance guard**: Refuses messages containing 20+ characters copied from prior tool execution outputs or file reads not present in user request.
- **Persistent Rate Limiting & Approvals (`backend/whatsapp_rate_limit.py`)**:
  - Persisted in `%APPDATA%\Kira\whatsapp_rate_limit.json` with atomic writes. Corrupt or unreadable files fail closed.
  - Minimum gap (30s), hourly cap (5 messages), daily cap (20 messages).
  - First message to any contact in Free mode requires explicit confirmation; subsequent sends use 3-second countdown.
- **Execution & Honest Verification**:
  - Mandatory `request_id` (fail-closed if missing). Strictly one send allowed per request; zero retries by agent loop.
  - Opens `whatsapp://send` URI scheme (blocked in all other tools).
  - Verifies foreground window via `ctypes` (`GetForegroundWindow`, `GetWindowThreadProcessId`) matches WhatsApp process names (`whatsapp.exe`, `whatsappdesktop.exe`). Aborts without pressing Enter if not focused or if error dialog is shown.
  - Sends `VK_RETURN` via `keybd_event`. Default honest report: `"I pressed Enter. I can't verify delivery, check WhatsApp."`.

### M4. Browser Tasks with Playwright (Headed Mode)

Kira provides safe, headed browser automation via Playwright Chromium:
- **Clean Profile Isolation**: Uses a dedicated browser profile in `%APPDATA%\Kira\browser_profile` (never touches personal Chrome/Edge profiles, cookies, saved passwords, or extensions). Runs in visible headed mode for real-time observation.
- **Strict URL & IP Safety**: Strictly `https://` only; port 443 only; blocks `file://`, `chrome://`, `about:`, `data:`, `javascript:`, and all app protocols (`whatsapp://`, etc.). Blocks loopback, RFC 1918 private IPs, link-local IPs (`169.254.169.254`), decimal/hex/octal IP literals, and trailing dots. All IDN hostnames are displayed in punycode (`xn--...`).
- **DNS Rebinding Defence**: Response server address (`response.server_addr()`) is verified after each response; if connected IP is private, loopback, or link-local, the page is closed immediately and the task halted.
- **Prompt Injection & Untrusted Data Wrapper**: In-page text is cleaned of scripts, styles, hidden elements, zero-width spaces, BiDi overrides, and Unicode tags. Wrapped in a random-delimited `<UNTRUSTED_WEB_DATA_{delim}>` block with neutralized closing tags (max 4,000 chars). Injected prompt instructions are treated strictly as untrusted data.
- **Taint Tracking**: After `browser_read` returns web content in a task, navigating to a new domain or typing into any field requires explicit confirmation reading back the domain or field.
- **Typing & Click Hardening**: Password, OTP, and payment fields (credit card, CVV, expiry) are detected and refused, instructing the user to complete them manually. Normal typing never presses Enter or submits. In Restricted mode, any click on a button, submit input, or form element asks confirmation first reading back the domain and exact button text.
- **Context Hardening**: All permissions denied, service workers blocked, downloads cancelled, file choosers cancelled, dialogs auto-dismissed, extra tabs/popups closed immediately (1-tab cap).
- **Kill Switch & Ollama Protection**: Checked prior to every browser action. Browser tools are disabled when running on the local Ollama model.

### M5. Self-written Scripts (`run_script`)

The model can write a short Python script; Kira saves it as `workspace/scripts/<name>_<id>.py` and runs it.

- **Restricted mode** shows you the script and asks. **Free mode** runs it after the 3-second countdown (say "stop" to cancel).
- **Static checks** (`backend/tools/script_runner.py`) reject the script before you are even asked if it imports anything outside a small standard-library allow-list (no `os`, `sys`, `subprocess`, `socket`, `ctypes`, `shutil`, `importlib`, `pathlib`, network modules), calls `eval`/`exec`/`compile`/`__import__`/`getattr`, touches dunder attributes, or calls `open()` with anything but a literal relative path.
- **Runtime limits**: isolated `python -I` subprocess, working directory `workspace/scripts/`, an environment with no API keys or PIN, timeout, output cap and a memory cap with child processes blocked (Windows Job Object). The kill switch stops a running script. Limits live in `config.yaml` under `scripts:`.
- **⚠ The static checks are NOT a full sandbox.** Python is dynamic and a determined script can get around AST rules. For code you do not trust set `scripts.sandbox` to `docker` (no network, memory cap, read-only mount; needs Docker Desktop) or `windows_sandbox` (needs the Windows Sandbox feature, Pro/Enterprise editions). If the chosen sandbox is missing, Kira refuses to run the script instead of running it unsandboxed.

Test: `.venvScriptspython.exe -m pytest -q tests	est_script_runner.py`

### Avatar Chat Box (Fallback Input)

When a microphone is unavailable or muted, or when you prefer typing:
- **Opening**: Click the avatar hover button (⋯) or right-click the avatar → **Chat…**.
- **Refused while Working**: The "Chat…" menu option is disabled while Kira is busy executing a task.
- **Focus Safety**: The avatar window is `focusable: false` by default so it never steals focus from VS Code or terminals. The window temporarily becomes focusable only while the chat box is open, and returns to `focusable: false` on Send, Escape, or clicking away (blur).
- **Safety**: `user_text` is rate-limited, capped at 2000 characters, requires an authenticated session, and **never acts as an answer to an ask-first confirmation prompt** (only explicit `confirm` messages with the matching `question_id` can confirm actions). Text is rendered with `textContent` only.

---

## Known Limits & Safety Notes

1. **VS Code PATH**: VS Code must be installed with the `code` CLI tool available on system `PATH`.
2. **PyAutoGUI Fail-Safe**: Moving the mouse to any screen corner immediately triggers `pyautogui.FailSafeException` and aborts all typing.
3. **Window Focus Verification**: If VS Code loses foreground focus or another window covers it, typing is aborted immediately to ensure keystrokes never leak into other applications.
4. **Ollama Screen Control Restriction**: When running on local Ollama models, Restricted mode is forced and screen control automation is disabled.
5. **Clipboard Paste**: Multi-line code uses `pyperclip` paste (`Ctrl+V`) to avoid VS Code auto-indent and auto-bracket mangling; previous clipboard contents are restored immediately afterward.

---

## Automated Test Suite

Run the full automated test suite:

### Python Backend Suite (all offline/mocked)
```powershell
.venv\Scripts\python.exe -m pytest -q
```
Includes M1: `tests/test_app_controls.py` (persona, mute vs sleep, PTT wake,
shutdown, WebSocket control commands, PIN lockout), `tests/test_settings.py`
(.env editing, user settings, override whitelist, settings CLI),
`tests/test_doctor.py`, `tests/test_no_secret_leaks.py`.

### Electron Avatar Suite (node:test, no Electron or GPU needed)
```powershell
cd avatar
npm test
```
`avatar/tests/avatar.test.js` (state machine, loudness, schema, bounds, bubble,
hit-mask) and `avatar/tests/controls.test.js` (menus, user config, off-screen
reset, sizes, action feed, backend start/stop and kill-tree, settings CLI
runner, control command schema).

Current totals per milestone: see `docs/PROGRESS.md`.
