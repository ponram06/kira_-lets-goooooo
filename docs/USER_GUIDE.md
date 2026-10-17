# Kira User Guide & Voice Command Cheat Sheet

## 1. Quick Controls & Keyboard Shortcuts

| Shortcut | Scope | Description |
|---|---|---|
| **`` ` `` (Backtick)** | Global | **Push-to-Talk (PTT)**: Hold to speak, release to send. (Configurable in Settings). |
| **`Ctrl+Alt+K`** | Global | **Emergency Kill Switch**: Halts all audio, tool executions, and active tasks immediately. |
| **`Ctrl+Alt+H`** | Global | **Show / Hide Avatar**: Toggles desktop avatar visibility. (Configurable in Settings). |
| **Mouse Corner** | Desktop | **PyAutoGUI Fail-Safe**: Moving mouse to any screen corner immediately halts active automation. |

---

## 2. Voice Command Cheat Sheet

Speak while holding the Push-to-Talk key:

### Persona & Controls
- *"Switch to Ryuk"* / *"Switch to Kira"* — Switches assistant personality and Edge voice live.
- *"Mute microphone"* / *"Unmute microphone"* — Toggles listening without closing the app.
- *"Go to sleep"* / *"Wake up"* — Puts avatar in low-power idle (5 FPS).
- *"Restricted mode"* — Reverts instantly to safe Restricted mode.
- *"Free mode"* — Explains that Free mode requires entering your PIN via the Avatar Menu.
- *"Stop"* / *"Kira stop"* — Activates the local emergency kill switch.

### System & Desktop Tools
- *"Turn the volume up / down"* or *"Set volume to 50%"* — Controls master audio volume.
- *"Set brightness to 80%"* — Adjusts primary screen brightness (minimum 5% floor).
- *"Lock my PC"* — Locks Windows workstation (asks confirmation in Restricted mode).
- *"Take a screenshot"* — Captures desktop to `workspace/screenshots/`.
- *"How is my system doing?"* — Reports CPU %, RAM usage, and battery status.

### Timers & Reminders
- *"Set a timer for 10 minutes"* — Runs an in-memory countdown timer with spoken alerts.
- *"Remind me to push code at 5:00 PM"* — Schedules a persistent reminder surviving reboots.
- *"List my reminders"* — Shows active scheduled reminders.
- *"Cancel reminder <ID>"* — Removes a scheduled reminder.

### Universal App Launcher & Adapters
- *"Open Spotify and play Bohemian Rhapsody"* — Launches Spotify and triggers media playback.
- *"Open VS Code"* / *"Open Notepad"* / *"Open Calculator"* — Launches indexed desktop/Store apps.
- *"Send a WhatsApp message to Alex saying I will be there in 10 minutes"* — Resolves contact, reads back message and masked number, and requests confirmation before sending.

### Learned Skills
- *"Save this as a skill called deploy demo"* — Saves a multi-step task recipe after completion.
- *"List my skills"* — Lists your saved workflow recipes.
- *"Run skill deploy demo"* — Replays the saved sequence step-by-step through policy checks.
- *"Delete skill deploy demo"* — Removes a saved skill recipe.

---

## 3. Permission Modes & Security Model

Kira operates under two distinct security modes:

1. **Restricted Mode (Default)**:
   - Modifications are confined strictly within the `workspace/` folder.
   - Any sensitive or medium/high-risk action (locking the PC, taking screenshots, messaging, deleting files, running scripts) prompts you with an audible and on-screen read-back with a 10-second timer.
   - You can approve by saying **"yes"** or typing **"yes"**.
   - Permanent denylist blocks system settings, registry editor, disk formatting, and UAC elevations.

2. **Free Mode**:
   - Entered **only** by selecting **Mode → Free…** from the Avatar Menu and entering your masked PIN.
   - Low and medium risk tasks run with fewer prompts; high-risk tasks run an audible 3-second countdown before executing (saying "Stop" or pressing `Ctrl+Alt+K` cancels).
   - Automatically reverts to Restricted mode after inactivity (default 15 minutes) or backend restart.

---

## 4. Settings & Troubleshooting

- **Settings Window**: Access via right-click on the avatar → **Settings…** or the system tray icon.
  - Set and test API keys (`GROQ_API_KEY`, `OPENROUTER_API_KEY`).
  - Configure models, microphone input, Edge TTS voice, and Free-mode PIN.
- **Doctor Command**:
  ```powershell
  .venv\Scripts\python.exe -m backend.doctor
  ```
  Runs a comprehensive health check on dependencies, microphone, keys, and git hygiene.
