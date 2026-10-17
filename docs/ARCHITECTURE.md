# Kira Architecture Documentation

## 1. System Overview

Kira is a local-first desktop AI assistant tailored for developers on Windows 11. It pairs a voice-and-text agent core with an on-screen transparent Electron avatar, governed by strict policy boundaries.

```mermaid
flowchart TD
    subgraph Client ["Frontend: Electron Avatar"]
        Tray["System Tray & Menus"]
        AvatarWin["Transparent Avatar Window"]
        Renderer["2D Canvas / Three.js VRM"]
        Feed["Action Feed & Chat Box"]
        WSClient["WebSocket Client (Local Token Auth)"]
    end

    subgraph Server ["Backend: Python Asyncio Core"]
        WSServer["WebSocket Server (127.0.0.1:8765)"]
        EventBus["In-Process Event Bus (events.py)"]
        AuditLog["Audit Logger (audit.py, Rotated)"]
        StateManager["AppState & ModeManager"]
    end

    subgraph Pipeline ["Agent & Voice Pipeline"]
        Ears["Ears: Groq Whisper / faster-whisper"]
        Brain["Brain: Multi-Provider LLM Fallback"]
        PolicyGate["Policy Engine & Permission Modes"]
        Hands["Hands: Tool Registry & App Adapters"]
        Mouth["Mouth: Edge TTS / pyttsx3 / miniaudio"]
    end

    Tray --> WSClient
    AvatarWin --> Renderer
    Renderer --> Feed
    WSClient <-->|Token Authenticated JSON| WSServer

    WSServer <--> EventBus
    EventBus --> AuditLog
    WSServer <--> StateManager

    Ears --> Brain
    Brain --> PolicyGate
    PolicyGate --> Hands
    Hands --> Brain
    Brain --> Mouth
    Mouth -.->|Loudness Events| EventBus
```

---

## 2. Security Boundaries & Permission Modes

All tool invocations pass through `backend/policy.py:check()` before execution.

```mermaid
stateDiagram-v2
    [*] --> Restricted: Startup Default

    Restricted --> Free: Masked PIN Entry Dialog\n(3 attempts max -> 60s lockout)
    Free --> Restricted: User voice/typed 'restricted mode'\nOR Inactivity Timeout (default 15m)\nOR Backend Restart

    state Restricted {
        [*] --> CheckRestricted
        CheckRestricted: Safe actions in workspace/ -> Allow
        CheckRestricted: Medium/High risk or uncertain -> Ask Confirmation
        CheckRestricted: Actions outside workspace/ -> Block
        CheckRestricted: Modifying security/PIN/disk -> Block
    }

    state Free {
        [*] --> CheckFree
        CheckFree: Low risk -> Allow
        CheckFree: Medium/High risk -> 3-Second Audible Countdown
        CheckFree: Actions outside workspace/ -> Ask Confirmation
        CheckFree: Permanent denylist (formatting, UAC) -> Block
    }
```

### Safety Principles
1. **Workspace Boundary**: The filesystem tools can only create or edit files strictly inside `workspace/`. Token files, `.env`, and git roots are physically isolated.
2. **Untrusted Data vs Instructions**: Content retrieved from websites (`browser_read`), files, tool results, and screenshots is tagged as untrusted data (`<UNTRUSTED_WEB_DATA_{delim}>`) and cannot manipulate agent mode or permissions.
3. **Secret Redaction**: API keys and `KIRA_FREE_MODE_PIN` are redacted (`[REDACTED]`) from logs, tracebacks, events, stdout, and WebSocket frames via `backend/secrets_guard.py`.
4. **Emergency Kill Switch**: Pressing `Ctrl+Alt+K` or speaking "Stop" activates `KILL_SWITCH.stop()`, immediately terminating speech audio and aborting active loops.

---

## 3. Communication Protocol (WebSocket API)

The backend exposes a local WebSocket server at `127.0.0.1:8765`:
- **Authentication**: On connection, the client must present the ephemeral token read from `.kira/token` within 5 seconds.
- **Inbound Commands**:
  - `free_mode_request`: Carries PIN from the Electron modal.
  - `set_persona`: Switches persona between `kira` and `ryuk`.
  - `set_mic_muted`: Toggles microphone state.
  - `set_sleep`: Enters low-power avatar sleep mode (5 FPS).
  - `confirm`: Resolves a pending `question_id` (`yes` or `no`).
  - `user_text`: Text chat input fallback (rate-limited, isolated from confirmations).
  - `stop`: Halts task execution and audio playback immediately.
  - `shutdown`: Initiates clean process shutdown.
- **Outbound Event Feed**:
  - `agent.step_start` / `agent.step_done`
  - `tool.start` / `tool.done` / `tool.error`
  - `policy.decision` (allow, ask, countdown, block)
  - `voice.loudness` (smoothed RMS loudness 0.0–1.0 for lip-sync)
  - `screen.highlight` (DIP target ring coordinates)
