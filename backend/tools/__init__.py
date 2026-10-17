"""
tools package
=============
Exports tool implementations and OpenAI-compatible tool calling schemas.
"""

from __future__ import annotations

from typing import Any, Callable

from backend.tools.execution import run_command
from backend.tools.filesystem import create_file, create_folder
from backend.tools.system import open_app, open_website
from backend.tools.voice_stub import speak
from backend.tools.vscode_visible import open_in_vscode
from backend.tools.system_control import brightness, lock_pc, system_info, take_screenshot, volume
from backend.tools.reminders_tool import cancel_reminder, list_reminders, set_reminder, set_timer
from backend.tools.app_launcher import media_control, open_any_app
from backend.tools.whatsapp_tool import send_whatsapp
from backend.tools.script_runner import run_script
from backend.tools.screen_tool import screen_task
from backend.tools.browser_tools import (
    browser_back,
    browser_click,
    browser_close,
    browser_open,
    browser_read,
    browser_scroll,
    browser_search,
    browser_type,
)

# Mapping of tool name -> callable implementation
TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "create_folder": create_folder,
    "create_file": create_file,
    "run_command": run_command,
    "open_app": open_app,
    "open_website": open_website,
    "speak": speak,
    "open_in_vscode": open_in_vscode,
    # M2 system tools
    "volume": volume,
    "brightness": brightness,
    "lock_pc": lock_pc,
    "take_screenshot": take_screenshot,
    "system_info": system_info,
    "set_timer": set_timer,
    "set_reminder": set_reminder,
    "list_reminders": list_reminders,
    "cancel_reminder": cancel_reminder,
    # M2B universal launcher and adapters
    "open_any_app": open_any_app,
    "media_control": media_control,
    # M3 messaging
    "send_whatsapp": send_whatsapp,
    # M5 self-written scripts
    "run_script": run_script,
    # M6 screen control (the agent awaits screen_tool.run_screen_task instead)
    "screen_task": screen_task,
    # M4 browser tools
    "browser_open": browser_open,
    "browser_search": browser_search,
    "browser_read": browser_read,
    "browser_click": browser_click,
    "browser_type": browser_type,
    "browser_scroll": browser_scroll,
    "browser_back": browser_back,
    "browser_close": browser_close,
}


def _fn(name: str, description: str, properties: dict | None = None, required: list | None = None) -> dict:
    """Compact helper for an OpenAI function schema."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": required or []},
        },
    }


_LEVEL = {"type": "integer", "minimum": 0, "maximum": 100, "description": "Level 0-100 (for action 'set')"}
_STEP = {"type": "integer", "minimum": 1, "maximum": 100, "description": "Percent to change for up/down (optional)"}

M2_TOOLS_SCHEMA: list[dict] = [
    _fn("volume", "Change or read the system speaker volume.",
        {"action": {"type": "string", "enum": ["up", "down", "set", "mute", "unmute", "get"]},
         "level": _LEVEL, "step": _STEP}, ["action"]),
    _fn("brightness", "Change or read the screen brightness of the main display.",
        {"action": {"type": "string", "enum": ["up", "down", "set", "get"]},
         "level": _LEVEL, "step": _STEP}, ["action"]),
    _fn("lock_pc", "Lock the Windows session (like Win+L)."),
    _fn("take_screenshot", "Save a screenshot of all monitors into workspace/screenshots/.",
        {"name": {"type": "string", "description": "Optional short file name prefix"}}),
    _fn("system_info", "Report CPU load, RAM use and battery state."),
    _fn("set_timer", "Start a countdown timer that is announced when it ends.",
        {"minutes": {"type": "number"}, "seconds": {"type": "number"},
         "label": {"type": "string", "description": "What the timer is for"}}),
    _fn("set_reminder", "Set a reminder at a local time. It survives restarts.",
        {"when": {"type": "string", "description": "'HH:MM' (24h) or 'YYYY-MM-DD HH:MM'"},
         "text": {"type": "string", "description": "What to remind about"}}, ["when", "text"]),
    _fn("list_reminders", "List pending timers and reminders with their ids."),
    _fn("cancel_reminder", "Cancel a timer or reminder by id.",
        {"reminder_id": {"type": "string"}}, ["reminder_id"]),
    _fn("open_any_app", "Open an installed Windows desktop or Store application by name.",
        {"name": {"type": "string", "description": "Name of the application to open (e.g. 'Spotify', 'Google Chrome')"}},
        ["name"]),
    _fn("media_control", "Control media playback on supported applications (e.g. Spotify).",
        {"action": {"type": "string", "enum": ["open", "play_pause", "next", "previous", "volume_up", "volume_down"], "description": "Media action to perform"},
         "app": {"type": "string", "description": "Target application name (default: 'spotify')"}},
        ["action"]),
    _fn("send_whatsapp", "Send a WhatsApp message to an existing contact from contacts.yaml.",
        {"contact": {"type": "string", "description": "Name of the contact from contacts.yaml (e.g. 'Meldan'). Phone numbers are not accepted."},
         "text": {"type": "string", "description": "Exact message text to send (up to 280 characters). Must come from user request."}},
        ["contact", "text"]),
]

# OpenAI-compatible function schemas
KIRA_TOOLS_SCHEMA: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "create_folder",
            "description": "Create a new directory inside workspace/.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path of the directory to create, relative to workspace/ (e.g. 'demo')",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Create a file with content inside workspace/.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path of the file relative to workspace/ (e.g. 'demo/hello.py')",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write into the file",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute an allow-listed command in a visible CMD window inside workspace/ and read back the output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command to run (e.g. 'python demo/hello.py')",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Optional subdirectory within workspace/ to run the command in",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open an allow-listed desktop application (e.g. 'code', 'notepad', 'calc').",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Application name to launch",
                    }
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_website",
            "description": "Open an allow-listed website in the default browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to open (e.g. 'https://github.com')",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "speak",
            "description": "Narrate an action or speak a mentor message to the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Message to speak",
                    }
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_in_vscode",
            "description": "Open VS Code on a workspace folder, type source code visibly into the file, save, and verify.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Folder or file path relative to workspace/ (e.g. 'demo' or 'demo/hello.py')",
                    },
                    "code": {
                        "type": "string",
                        "description": "Source code to type into the editor",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional file name (e.g. 'hello.py') if path specifies only the directory",
                    },
                },
                "required": ["path", "code"],
            },
        },
    },
]

M4_TOOLS_SCHEMA: list[dict] = [
    _fn("browser_open", "Open an HTTPS website in the headed browser.",
        {"url": {"type": "string", "description": "Safe HTTPS URL to open (e.g. 'https://docs.python.org')"}},
        ["url"]),
    _fn("browser_search", "Search the web using a safe search engine.",
        {"query": {"type": "string", "description": "Search query text"}},
        ["query"]),
    _fn("browser_read", "Read visible, sanitized text from the current browser page wrapped in untrusted data markers."),
    _fn("browser_click", "Click an element by visible button text, link text, or role.",
        {"target": {"type": "string", "description": "Visible text or role of element to click"}},
        ["target"]),
    _fn("browser_type", "Type text into a field by label or placeholder without pressing Enter.",
        {"field_label": {"type": "string", "description": "Label, placeholder, or name of input field"},
         "text": {"type": "string", "description": "Text to type into the field"}},
        ["field_label", "text"]),
    _fn("browser_scroll", "Scroll the active page viewport up or down.",
        {"direction": {"type": "string", "enum": ["up", "down"], "description": "Scroll direction"},
         "amount": {"type": "integer", "description": "Pixels to scroll (default: 500)"}}),
    _fn("browser_back", "Navigate back in browser history."),
    _fn("browser_close", "Close the active browser page and context."),
]

KIRA_TOOLS_SCHEMA.extend(M2_TOOLS_SCHEMA)
KIRA_TOOLS_SCHEMA.append(_fn(
    "screen_task",
    "LAST RESORT: control the screen with mouse and keyboard to reach a goal when no adapter, "
    "built-in tool, browser tool or script can do it. Kira looks at screenshots and does one action at a time.",
    {"goal": {"type": "string", "description": "What should be visibly achieved on screen"}},
    ["goal"]))
KIRA_TOOLS_SCHEMA.append(_fn(
    "run_script",
    "Write and run a short Python script (computation, text or file work inside workspace/scripts/). "
    "Only safe standard-library modules are allowed; no os, subprocess, network or system access. "
    "Prefer an adapter or built-in tool when one exists.",
    {"code": {"type": "string", "description": "Complete Python source code"},
     "name": {"type": "string", "description": "Short file name, letters/digits/_/- only"}},
    ["code"]))
KIRA_TOOLS_SCHEMA.extend(M4_TOOLS_SCHEMA)

__all__ = [
    "create_folder",
    "create_file",
    "run_command",
    "open_app",
    "open_website",
    "speak",
    "open_in_vscode",
    "open_any_app",
    "media_control",
    "send_whatsapp",
    "browser_open",
    "browser_search",
    "browser_read",
    "browser_click",
    "browser_type",
    "browser_scroll",
    "browser_back",
    "browser_close",
    "TOOL_FUNCTIONS",
    "KIRA_TOOLS_SCHEMA",
]


