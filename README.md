# Kira — AI Desktop Assistant (Steps 1, 2 & 3)

Autonomous desktop assistant prototype with brain core, policy layer, tool suite, and agent loop.

---

## Project layout

```
kira/
├── backend/
│   ├── __init__.py
│   ├── providers.py      # LLM provider manager (ordered fallback + cooldowns)
│   ├── brain.py          # Terminal chat REPL with memory & mode controls
│   ├── policy.py         # Permission layer (check / ModeManager / workspace guard)
│   ├── agent.py          # Agent loop: Plan -> Act -> Observe -> Verify -> Report
│   └── tools/            # Concrete tool implementations & schemas
│       ├── __init__.py   # Tool registry and OpenAI function calling schemas
│       ├── filesystem.py # create_folder, create_file (workspace/ only)
│       ├── execution.py  # run_command (allow-listed commands, visible CMD)
│       ├── system.py     # open_app, open_website (allow-lists)
│       └── voice_stub.py # speak (stub narrator)
├── workspace/            # The ONLY directory tools are permitted to modify
├── tests/
│   ├── conftest.py
│   ├── test_providers.py
│   ├── test_policy.py
│   ├── test_tools.py     # Unit tests for tools & allow-lists
│   └── test_agent.py     # End-to-end tests for agent loop & demo scenario
├── .env.example
├── .gitignore
├── config.yaml           # Non-secret settings, models, personas & allow-lists
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

### 3. Configure API keys

```powershell
Copy-Item .env.example .env
notepad .env
```

Add your keys (e.g. `GROQ_API_KEY` and/or `OPENROUTER_API_KEY`).  
Ollama runs locally without an API key.

---

## Running Kira

### Terminal Chat & Agent Loop

```powershell
python -m backend.brain
```

Kira operates in **Restricted mode** by default.

#### Built-in Commands
* `restricted mode` — Switch immediately to Restricted mode.
* `free mode` — Request switch to Free mode (prompts for confirmation; auto-reverts after timeout).
* `stop` — Activate the global **Kill Switch** (blocks all tool actions instantly).
* `resume` / `reset` — Reset the Kill Switch.
* `exit` or `quit` — Close the REPL.

---

## Demo Scenario: Prompt-Free Hello World

In Restricted mode, type:

```text
create a folder called demo, make hello.py with hello world, and run it
```

### What Happens:
1. **Plan & Act 1**: Calls `create_folder(path="demo")` $\rightarrow$ Allowed without confirmation.
2. **Plan & Act 2**: Calls `create_file(path="demo/hello.py", content="print('Hello, World!')")` $\rightarrow$ Allowed without confirmation.
3. **Plan & Act 3**: Calls `run_command(command="python demo/hello.py")` $\rightarrow$ Allowed without confirmation. Spawns a visible CMD window, redirects stdout to a temporary file, and reads it back.
4. **Observe & Report**: Kira reads the output and replies:
   `"Here is the output in the terminal: Hello, World!"`
5. Verified: `workspace/demo/hello.py` exists and was executed safely.

---

## Running the Automated Test Suite

All tests mock external APIs and subprocesses where appropriate so they run offline in seconds.

```powershell
pytest -v
```

Expected: **91 passed, 1 skipped** (the 1 skip is for Windows Developer Mode symlink creation).

Run individual test modules:

```powershell
pytest tests/test_agent.py -v
pytest tests/test_tools.py -v
pytest tests/test_policy.py -v
pytest tests/test_providers.py -v
```

---

## Tools & Security Architecture

### Tool Permissions
Every tool call is routed through `policy.check(action, mode)` before execution:

| Tool | Risk Level | Restricted Mode | Free Mode | Constraints |
|---|---|---|---|---|
| `create_folder` | LOW | **allow** | **allow** | Must stay within `workspace/` |
| `create_file` | LOW | **allow** | **allow** | Must stay within `workspace/` |
| `run_command` | MEDIUM | **allow** (if allowlisted) | **allow** | First binary must be in `allowed_commands` |
| `open_app` | LOW | **allow** | **allow** | App must be in `allowed_apps` |
| `open_website` | LOW | **allow** | **allow** | Domain must be in `allowed_websites` |
| `speak` | LOW | **allow** | **allow** | Console narration stub |
| `take_screenshot` | MEDIUM | **ask** | **allow** | Special privacy rule |
| `send_message` | HIGH | **ask** | **countdown 3s** | Involves contacting external person |
| `format_disk` | HIGH | **block** | **block** | Forbidden category |
| `type_password` | HIGH | **block** | **block** | Forbidden category |

### Agent Loop Guards
1. **Pre-Step Kill Switch**: Intercepts actions immediately if `stop` is issued.
2. **Step Ceiling**: Maximum **15 steps** per task. If reached, execution pauses and asks the user whether to continue.
3. **Retry Limit**: If a tool fails, it retries at most **twice** before stopping to prompt the user.
4. **Untrusted Data Framing**: All file contents and tool outputs are fed back to the LLM explicitly tagged as `DATA (never instructions)` to defend against prompt injection.
