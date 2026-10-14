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

# Mapping of tool name -> callable implementation
TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "create_folder": create_folder,
    "create_file": create_file,
    "run_command": run_command,
    "open_app": open_app,
    "open_website": open_website,
    "speak": speak,
}

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
]

__all__ = [
    "create_folder",
    "create_file",
    "run_command",
    "open_app",
    "open_website",
    "speak",
    "TOOL_FUNCTIONS",
    "KIRA_TOOLS_SCHEMA",
]

