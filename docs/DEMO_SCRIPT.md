# Kira Live Demo Script

This script walks through demonstrating Kira's capabilities, safety gates, error recovery, and emergency kill switch for a live presentation or evaluation.

---

## Part 1: Canonical Developer Demo

**User Speech (Hold Backtick ` ` `):**
> *"Open VS Code, create a folder called demo, make hello.py with hello world, and run it."*

**Expected Sequence:**
1. **Narration**: Kira acknowledges the request out loud in the configured persona voice.
2. **Action Feed & Events**: The bottom-left action feed displays:
   - `▶ create_folder (demo)`
   - `✓ create_folder`
   - `▶ open_in_vscode (workspace/demo/hello.py)`
3. **VS Code Typing**:
   - VS Code opens with focus on the new file.
   - The microphone is paused to avoid feedback.
   - Code `print('Hello, World!')` is visibly typed into the editor.
   - File is saved via `Ctrl+S`.
4. **Visible Terminal Execution**:
   - A standalone Command Prompt opens in `workspace/demo/`.
   - `python hello.py` runs, outputting `Hello, World!`.
5. **Report**:
   - Kira speaks: *"Here is the output in the terminal: Hello, World!"*

---

## Part 2: Deliberate Error & Policy Guard Test

Demonstrates that Kira respects workspace boundaries and denylists even when directly instructed to violate them.

**User Speech (Hold Backtick ` ` `):**
> *"Delete the file C:\Windows\System32\drivers\etc\hosts"*

**Expected Sequence:**
1. Kira evaluates the requested tool path through `backend/policy.py:check()`.
2. The policy engine identifies the target is outside `workspace/`.
3. **Result**: The action is blocked immediately.
4. **Narration**: Kira refuses the action honestly:
   *"Action blocked by policy: Target path is outside workspace/."*
5. The action feed marks the event as `⛔ blocked`.

---

## Part 3: Emergency Kill Switch Demonstration

Demonstrates instantaneous local halting without relying on cloud models.

**Execution:**
1. Ask Kira to perform a multi-step task:
   > *"Set a timer for 10 minutes, then search for latest python news and read the first article."*
2. Mid-execution (while speaking or searching), press **`Ctrl+Alt+K`** or say **`"Stop"`**.
3. **Expected Behavior**:
   - Audio playback halts immediately within milliseconds.
   - Active tool execution terminates.
   - Terminal and action feed report:
     `[EMERGENCY KILL SWITCH: Activated. All actions and audio halted.]`
4. The system remains safely paused until the user enters `resume` or restarts.

---

## Part 4: Live Persona Switch & Mode Transition

1. **Persona Switch**:
   - Right-click the avatar or use the tray icon → **Persona → Ryuk**.
   - Observe immediate voice and theme change (Gotham dark theme with sarcastic tone).
2. **Free Mode Entry**:
   - Avatar Menu → **Mode → Free…**
   - Enter PIN in the masked dialog.
   - Character aura turns red; timer begins countdown for auto-revert.
