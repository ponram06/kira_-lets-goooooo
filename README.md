# Jarvis — AI Desktop Assistant (Steps 1 & 2)

Brain core + policy layer.  No voice, avatar, or real tools yet.

---

## Project layout

```
jarvis/
├── backend/
│   ├── __init__.py
│   ├── providers.py   # LLM provider manager (fallback + cooldowns)
│   ├── brain.py       # Terminal chat REPL with rolling memory
│   └── policy.py      # Permission layer (check / ModeManager / workspace guard)
├── workspace/         # The ONLY folder tools may modify
├── tests/
│   ├── conftest.py
│   ├── test_providers.py
│   └── test_policy.py
├── .env.example
├── .gitignore
├── config.yaml
├── pytest.ini
├── requirements.txt
└── README.md
```

---

## Step 1 — First-time setup

Run all commands from `d:\kira\jarvis\`.

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

Fill in `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`.  
Ollama needs no key — just leave that line absent.

### 4. Choose your model names

Open `config.yaml` and replace the placeholder model names for Gemini, Groq
and OpenRouter (each has a `# FILL IN:` comment).  Ollama is already set to
`qwen2.5:7b`.

---

## Step 2 — Run the chat REPL

```powershell
python -m backend.brain
```

Kira starts a conversation loop.  After every reply the provider name appears
in brackets, e.g. `Kira [gemini]:`.  Type `exit` or `quit` to stop.

**Provider fallback is automatic.**  If Gemini is rate-limited or offline,
Kira silently falls back to Groq → OpenRouter → Ollama.

---

## Step 3 — Run the tests

```powershell
pytest -v
```

Expected: all tests pass.  Tests that need symlink or junction creation are
automatically skipped if the environment lacks the required permissions.

Run only one file:

```powershell
pytest tests/test_providers.py -v
pytest tests/test_policy.py -v
```

---

## 5-line manual terminal test

After setup, activate the venv and run the REPL:

```powershell
.venv\Scripts\Activate.ps1
python -m backend.brain
```

Then type these lines one by one:

```
You: Hello, who are you?
You: What is the difference between a list and a tuple in Python?
You: What would you do before deleting a file on my system?
You: How many messages do you remember?
You: exit
```

You should see Kira reply to each question, with a provider tag like
`[gemini]` or `[ollama]` after her name.  The final line prints `Goodbye.`

---

## Configuration reference

| Key | Default | Description |
|---|---|---|
| `cooldowns.rate_limit` | 900 s | 429 transient rate-limit cooldown |
| `cooldowns.quota` | 3600 s | Quota / daily-limit cooldown |
| `cooldowns.timeout` | 60 s | Timeout / connection error cooldown |
| `cooldowns.server_error` | 60 s | HTTP 5xx cooldown |
| `memory_limit` | 20 | Rolling conversation history size |
| `persona.name` | kira | Switch to `ryuk` for sarcastic persona (future) |
| `mode.free_revert_timeout` | 600 s | Free mode auto-revert to Restricted |

---

## Policy quick reference

| Action | Restricted | Free |
|---|---|---|
| LOW risk (create file, speak…) | **allow** | **allow** |
| MEDIUM allowlisted (run_command, run_script) | **allow** | **allow** |
| MEDIUM screenshot | **ask** | **allow** |
| MEDIUM non-allowlisted | **ask** | **ask** |
| HIGH (delete, install…) | **ask** | **ask** |
| HIGH + affects people (send_message) | **ask** | **countdown 3 s** |
| outside_workspace | **block** | **ask** |
| Forbidden (disk format, passwords…) | **block** | **block** |
| Kill switch active | **block** | **block** |

> **Ollama auto-restriction:** when Ollama answers, the mode is forced to  
> Restricted and screen control is disabled until you deliberately re-enable  
> Free mode.

---

## Security notes

* API keys live only in `.env` — never in code or `config.yaml`.
* Keys are never logged (only provider name and reply length).
* `workspace/` contents are in `.gitignore` — nothing you create there is  
  committed accidentally.
* Path traversal, symlinks, junctions, and cross-drive escapes are all  
  blocked by `resolve_path()`.

