# Kira — AI Desktop Assistant (Steps 1, 2, 3 & 4: Voice Pipeline)

Autonomous desktop assistant prototype with brain core, safety policy layer, tool suite, agent loop, and real-time voice pipeline (Ears & Mouth).

---

## Project Layout

```
kira/
├── backend/
│   ├── __init__.py
│   ├── providers.py      # LLM provider manager (ordered fallback + cooldowns)
│   ├── brain.py          # Unified REPL (typed + voice PTT input)
│   ├── policy.py         # Permission layer (check / ModeManager / workspace guard)
│   ├── audit.py          # Audit logging (logs/actions.jsonl) & live event feed
│   ├── agent.py          # Agent loop: Plan -> Act -> Observe -> Verify -> Report
│   ├── voice.py          # Ears (Groq Whisper / faster-whisper) & Mouth (Edge TTS / miniaudio / pyttsx3)
│   └── tools/            # Concrete tool implementations & schemas
│       ├── __init__.py   # Tool registry and OpenAI function calling schemas
│       ├── filesystem.py # create_folder, create_file (workspace/ only)
│       ├── execution.py  # run_command (allow-listed commands, visible CMD)
│       ├── system.py     # open_app, open_website (allow-lists)
│       └── voice_stub.py # speak (TTS narrator routed to VoiceManager)
├── logs/                 # actions.jsonl (git-ignored)
├── workspace/            # The ONLY directory tools are permitted to modify
├── tests/
│   ├── conftest.py
│   ├── test_providers.py
│   ├── test_policy.py
│   ├── test_tools.py     # Unit tests for tools & allow-lists
│   ├── test_agent.py     # End-to-end tests for agent loop & demo scenario
│   └── test_voice.py     # Step 4 unit tests (STT/TTS/safety/cooldowns/hotkeys)
├── .env.example
├── .gitignore            # Audio (*.wav, *.mp3) & temp files ignored
├── config.yaml           # Non-secret settings, models, hotkeys & allow-lists
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
* **Push-to-Talk (PTT)**: Hold **`F9`** (configurable in `config.yaml`) to speak. Release to transcribe and send.
* **Global Emergency Kill Hotkey**: Press **`Ctrl+Alt+K`** at any time to instantly halt all audio playback, tool execution, and active loops. Operates globally even if speech recognition is down.
* **Stop Word**: Saying **`"stop"`** or **`"Kira stop"`** immediately halts speech and activates the kill switch locally before sending to the LLM.
* **Mode Commands by Voice**:
  * Saying `"restricted mode"` immediately switches to Restricted mode.
  * Saying `"free mode"` by voice is **blocked by policy**; Kira instructs you to type it explicitly in the terminal.

#### Built-in Terminal Commands
* `restricted mode` — Switch immediately to Restricted mode.
* `free mode` — Request switch to Free mode (prompts for confirmation; auto-reverts after timeout).
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

### Test 4: Voice Free Mode Refusal
1. Hold **`F8`** and say: `"free mode"`.
2. **Verify**: Kira refuses via speech and terminal:
   `"Free mode cannot be enabled by voice. Please type 'free mode' in the terminal."`
   Mode remains `RESTRICTED`.

### Test 5: Spoken Read-Back & Confirmation
1. Ask Kira to perform an action outside allowlist requiring confirmation.
2. **Verify**: Kira reads back the confirmation request out loud. The 10-second timer starts after speech finishes.
3. Hold **`F8`** and say: `"yes"`.
4. **Verify**: The action is approved and proceeds.

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

## Known Limits & Safety Notes

1. **VS Code PATH**: VS Code must be installed with the `code` CLI tool available on system `PATH`.
2. **PyAutoGUI Fail-Safe**: Moving the mouse to any screen corner immediately triggers `pyautogui.FailSafeException` and aborts all typing.
3. **Window Focus Verification**: If VS Code loses foreground focus or another window covers it, typing is aborted immediately to ensure keystrokes never leak into other applications.
4. **Ollama Screen Control Restriction**: When running on local Ollama models, Restricted mode is forced and screen control automation is disabled.
5. **Clipboard Paste**: Multi-line code uses `pyperclip` paste (`Ctrl+V`) to avoid VS Code auto-indent and auto-bracket mangling; previous clipboard contents are restored immediately afterward.

---

## Automated Test Suite

Run the full automated test suite (**144 unit tests**, all offline/mocked):

```powershell
.venv\Scripts\pytest -v
```

### Test Breakdown by Module:
* `tests/test_agent.py`: 14 passed (agent loop, max steps, retry limits)
* `tests/test_events.py`: 6 passed (event pub/sub, ordering, secret stripping, restricted demo)
* `tests/test_policy.py`: 47 passed, 1 skipped (permission modes, boundaries, kill switch)
* `tests/test_providers.py`: 16 passed (provider fallback chain, cooldowns)
* `tests/test_tools.py`: 18 passed (filesystem, run_command, open_app, open_website)
* `tests/test_voice.py`: 25 passed (STT, TTS, sentence pipelining, hotkeys)
* `tests/test_vscode_visible.py`: 8 passed (window wait, save verify, active window guard, fail-safe)

Total: **144 passed, 1 skipped (100% green)**.
