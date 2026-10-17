# Kira — AI Desktop Assistant (Steps 1 through 6: Voice Pipeline & WebSocket Server)

Autonomous desktop assistant prototype with brain core, safety policy layer, tool suite, agent loop, real-time voice pipeline (Ears & Mouth), and WebSocket server for desktop avatar streaming.

---

## Project Layout

```
kira/
├── backend/
│   ├── __init__.py
│   ├── providers.py      # LLM provider manager (ordered fallback + cooldowns)
│   ├── brain.py          # Unified REPL (typed + voice PTT input + WebSocket server)
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

---

## Running Kira

### Unified Chat & Voice REPL

```powershell
python -m backend.brain
```

Kira operates in **Restricted mode** by default.

#### Voice Controls
* **Push-to-Talk (PTT)**: Hold **`` ` ``** (backtick, configurable in `config.yaml`) to speak. Release to transcribe and send.
* **Global Emergency Kill Hotkey**: Press **`Ctrl+Alt+K`** at any time to instantly halt all audio playback, tool execution, and active loops. Operates globally even if speech recognition is down.
* **Stop Word**: Saying **`"stop"`** or **`"Kira stop"`** immediately halts speech and activates the kill switch locally before sending to the LLM.
* **Mode Commands by Voice**:
  * Saying `"restricted mode"` immediately switches to Restricted mode (Apple turns **green**).
  * Saying `"go to free mode"` or `"free mode"` prompts for the security PIN (`1684`). Speak or type `1684` to enter Free mode (Apple turns **red**).

#### Built-in Terminal Commands
* `restricted mode` — Switch immediately to Restricted mode (Apple glow is green).
* `free mode` — Prompt for the security PIN (`1684`) to switch to Free mode (Apple glow is red; auto-reverts after timeout).
* `stop` — Activate the global **Kill Switch** (blocks all tool actions and audio instantly).
* `resume` / `reset` — Reset the Kill Switch.
* `exit` or `quit` — Close the REPL.

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
1. Run `python -m backend.brain`.
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

### Test 4: Voice Free Mode & PIN Protection
1. Hold **`` ` ``** and say: `"go to free mode"`.
2. **Verify**: Kira prompts for the PIN out loud and on screen: `"Please enter the PIN to enable Free mode."`.
3. Hold **`` ` ``** and say `"one six eight four"` (or type `1684` in the terminal).
4. **Verify**: Kira announces `"PIN verified. Switched to Free mode."`, the Ryuk avatar's apple turns **crimson red**, and Free mode activates (with a 600s auto-revert timer).
5. Say or type `"restricted mode"`. Kira switches back immediately and the avatar's apple turns **emerald green**.

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
   python -m backend.brain
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
- **System Tray & Kill Switch**: System tray icon with Show/Hide, Lock Position, and Quit controls. A dedicated **Stop** button in the hover menu and tray triggers the global emergency kill switch.

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
   - Click the gear icon to open the hover menu; click **Stop** and observe immediate kill switch activation in Terminal 1.
   - Drag the character to any screen corner or secondary monitor. Release to verify bounds clamping.
   - Click outside the character in an open editor window to verify clicks pass through seamlessly.

### Running with Full Kira Voice Assistant
1. In **Terminal 1**, start Kira:
   ```powershell
   .venv\Scripts\python.exe -m backend.brain
   ```
2. In **Terminal 2**, launch the avatar:
   ```powershell
   cd avatar
   npm start
   ```

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

### Python Backend Suite (**163 unit tests**, all offline/mocked):
```powershell
.venv\Scripts\python.exe -m pytest -v
```

* `tests/test_agent.py`: 14 passed (agent loop, max steps, retry limits)
* `tests/test_events.py`: 6 passed (event pub/sub, ordering, secret stripping, restricted demo)
* `tests/test_policy.py`: 48 passed, 1 skipped (permission modes, boundaries, kill switch, persona independence)
* `tests/test_providers.py`: 16 passed (provider fallback chain, cooldowns)
* `tests/test_tools.py`: 19 passed (filesystem, run_command, open_app, open_website, cmd pause)
* `tests/test_voice.py`: 25 passed (STT, TTS, sentence pipelining, hotkeys, shared confirm)
* `tests/test_vscode_visible.py`: 8 passed (window wait, save verify, active window guard, fail-safe)
* `tests/test_server.py`: 17 passed (token auth, origin/host checks, confirm question_id, slow client drop, throttling)

### Electron Avatar Suite (**22 unit tests**, node:test runner):
```powershell
cd avatar
npm test
```

* `avatar/tests/avatar.test.js`: 22 passed (state machine, loudness EMA, backend schema, bounds clamping, bubble sanitization, alpha hit-mask)

Total: **185 tests passed (100% green)**.

