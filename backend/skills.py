"""
skills.py
=========
M7 learned skills: a successful multi-step task can be saved (only after the
user says yes) as a JSON recipe of tool calls, and replayed later.

Safety
------
- The model cannot create skills: there is no "save skill" tool. Saving only
  happens through ``offer_save`` after a task, with the user's yes.
- A recipe holds tool names and their *schema* parameters only. On load, every
  step is re-validated: unknown tools, skill-management tools and any extra
  fields (mode, approved, allowlisted, config_path, ...) are dropped, so a
  skill can never raise its own permissions, even if the file is edited.
- Replay feeds the steps through the normal AgentRunner loop
  (``ScriptedProvider``), so every step passes exactly the same policy checks,
  asks, countdowns, kill switch and step cap as a live task.
- Steps whose arguments contain a stored secret are never saved.
- Stored in the user config folder (``skills.json``), never in the repo.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from backend import secrets_guard, user_config

logger = logging.getLogger(__name__)

MAX_STEPS = 15
MAX_SKILLS = 100
SKILL_TOOLS = frozenset({"list_skills", "run_skill", "rename_skill", "delete_skill"})
_NAME_RE = re.compile(r"^[\w .'-]{1,60}$", re.UNICODE)
_lock = threading.Lock()


def skills_path() -> Path:
    return user_config.user_dir(create=True) / "skills.json"


def _schema_params() -> dict[str, frozenset[str]]:
    from backend.agent import _SCHEMA_PARAMS
    return _SCHEMA_PARAMS


def clean_steps(steps: Any) -> list[dict]:
    """Validate recipe steps: known non-skill tools, schema params only."""
    from backend.policy import TOOL_REGISTRY
    schema = _schema_params()
    out: list[dict] = []
    if not isinstance(steps, list):
        return out
    for st in steps[:MAX_STEPS]:
        if not isinstance(st, dict):
            continue
        tool = st.get("tool")
        if not isinstance(tool, str) or tool in SKILL_TOOLS or tool not in TOOL_REGISTRY or tool not in schema:
            continue
        args = st.get("args") if isinstance(st.get("args"), dict) else {}
        out.append({"tool": tool, "args": {k: v for k, v in args.items() if k in schema[tool]}})
    return out


def valid_name(name: Any) -> bool:
    return isinstance(name, str) and bool(_NAME_RE.match(name.strip())) and bool(name.strip())


def name_from_request(text: str) -> str:
    words = re.findall(r"[\w'-]+", text or "", re.UNICODE)[:6]
    name = " ".join(words).strip() or "skill"
    return name[:60]


# ── storage ───────────────────────────────────────────────────────────────

def load_all() -> list[dict]:
    p = skills_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("skills.json unreadable (%s); treating as empty", type(exc).__name__)
        return []
    skills = []
    for s in data if isinstance(data, list) else []:
        if isinstance(s, dict) and valid_name(s.get("name")):
            steps = clean_steps(s.get("steps"))
            if steps:
                skills.append({"id": str(s.get("id") or uuid.uuid4().hex[:8]), "name": s["name"].strip(),
                               "created": s.get("created", ""), "steps": steps})
    return skills


def _save_all(skills: list[dict]) -> None:
    p = skills_path()
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".skills_", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(skills, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def find(name: str) -> Optional[dict]:
    key = (name or "").strip().lower()
    for s in load_all():
        if s["name"].lower() == key or s["id"] == key:
            return s
    return None


def save_skill(name: str, steps: list[dict]) -> tuple[bool, str]:
    if not valid_name(name):
        return False, "Skill names may use letters, digits, spaces and . ' - (up to 60 characters)."
    clean = clean_steps(steps)
    if len(clean) < 2:
        return False, "A skill needs at least two valid steps."
    if any(secrets_guard.contains_secret(json.dumps(s["args"], ensure_ascii=False)) for s in clean):
        return False, "Not saved: a step contains a stored secret."
    with _lock:
        skills = load_all()
        if len(skills) >= MAX_SKILLS:
            return False, f"You already have {MAX_SKILLS} skills. Delete one first."
        base, n = name.strip(), 2
        final = base
        while any(s["name"].lower() == final.lower() for s in skills):
            final = f"{base} {n}"[:60]
            n += 1
        skills.append({"id": uuid.uuid4().hex[:8], "name": final,
                       "created": time.strftime("%Y-%m-%d %H:%M"), "steps": clean})
        _save_all(skills)
    return True, final


# ── tools (registered in policy.TOOL_REGISTRY) ────────────────────────────

def list_skills() -> str:
    skills = load_all()
    if not skills:
        return "No saved skills yet."
    return "Saved skills:\n" + "\n".join(
        f"- {s['name']} ({len(s['steps'])} steps: {', '.join(st['tool'] for st in s['steps'])})" for s in skills)


def rename_skill(name: str, new_name: str) -> str:
    if not valid_name(new_name):
        return "Error: invalid new name."
    with _lock:
        skills = load_all()
        key = (name or "").strip().lower()
        hit = next((s for s in skills if s["name"].lower() == key or s["id"] == key), None)
        if not hit:
            return f"Error: no skill named '{name}'."
        if any(s["name"].lower() == new_name.strip().lower() and s is not hit for s in skills):
            return f"Error: a skill named '{new_name}' already exists."
        hit["name"] = new_name.strip()
        _save_all(skills)
    return f"Renamed skill to '{new_name.strip()}'."


def delete_skill(name: str) -> str:
    with _lock:
        skills = load_all()
        key = (name or "").strip().lower()
        keep = [s for s in skills if not (s["name"].lower() == key or s["id"] == key)]
        if len(keep) == len(skills):
            return f"Error: no skill named '{name}'."
        _save_all(keep)
    return f"Deleted skill '{name}'."


def run_skill(name: str = "") -> str:
    """Direct calls fail closed: replay must go through the agent loop."""
    return "Refused: run_skill can only run through the agent loop."


# ── recording and replay ──────────────────────────────────────────────────

def recipe_offerable(recipe: list[dict]) -> bool:
    return len(clean_steps(recipe)) >= 2


async def offer_save(recipe: list[dict], user_request: str,
                     confirm: Callable[[str], Awaitable[bool]],
                     speak: Optional[Callable[[str], Awaitable[None]]] = None) -> Optional[str]:
    """Ask once whether to save a successful multi-step task. Returns the saved name."""
    if not recipe_offerable(recipe):
        return None
    name = name_from_request(user_request)
    steps = clean_steps(recipe)
    prompt = (f"That took {len(steps)} steps ({', '.join(s['tool'] for s in steps)}). "
              f"Save it as a skill named '{name}'? (yes/no):")
    if speak:
        try:
            await speak(f"Shall I save this as a skill named {name}?")
        except Exception:
            pass
    if not await confirm(prompt):
        return None
    ok, result = save_skill(name, steps)
    return result if ok else None


class _Call:
    def __init__(self, i: int, tool: str, args: dict) -> None:
        self.id = f"skill_{i}"
        self.type = "function"
        self.function = type("F", (), {"name": tool, "arguments": json.dumps(args)})()


class _Msg:
    def __init__(self, content: str = "", tool_calls: Optional[list] = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


_STOP_PREFIXES = ("Error", "ERROR", "USER DECLINED", "CANCELLED", "Refused", "Failed", "Execution halted",
                  "Task halted", "Action blocked", "Action cancelled", "Screen control declined",
                  "Screen control failed", "Screen control halted", "Screen control refused")


class ScriptedProvider:
    """Stands in for the LLM during replay: returns one recorded step per turn
    and stops at the first step that did not succeed."""

    def __init__(self, skill: dict, provider_name: Optional[str]) -> None:
        self.steps = clean_steps(skill.get("steps"))
        self.name = skill.get("name", "skill")
        self.provider_name = provider_name or "skill"
        self.i = 0
        self.results: list[str] = []

    async def ask(self, messages: list[dict], tools: Any = None, return_raw: bool = False):
        if self.i > 0:
            last = next((m for m in reversed(messages) if m.get("role") == "tool"), None)
            body = str(last.get("content", "")) if last else ""
            body = body.split("\n", 1)[1] if body.startswith("DATA") and "\n" in body else body
            self.results.append(body[:120])
            if body.startswith(_STOP_PREFIXES):
                return _Msg(f"Skill '{self.name}' stopped at step {self.i} of {len(self.steps)} "
                            f"({self.steps[self.i - 1]['tool']}): {body[:200]}"), self.provider_name
        if self.i >= len(self.steps):
            return _Msg(f"Skill '{self.name}' finished all {len(self.steps)} steps."), self.provider_name
        st = self.steps[self.i]
        self.i += 1
        return _Msg(tool_calls=[_Call(self.i, st["tool"], st["args"])]), self.provider_name
