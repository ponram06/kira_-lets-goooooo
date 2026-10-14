"""
filesystem.py
=============
Filesystem tools for Kira. All operations strictly confined to workspace/.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.policy import resolve_path


def create_folder(path: str) -> str:
    """
    Create a new directory inside workspace/.

    Parameters
    ----------
    path:
        Relative path inside workspace/.
    """
    resolved: Path = resolve_path(path)
    os.makedirs(resolved, exist_ok=True)
    return f"Folder created successfully at '{path}'"


def create_file(path: str, content: str = "") -> str:
    """
    Create or overwrite a file inside workspace/ with the given content.

    Parameters
    ----------
    path:
        Relative path inside workspace/.
    content:
        String content to write into the file.
    """
    resolved: Path = resolve_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with open(resolved, "w", encoding="utf-8") as f:
        f.write(content)
    return f"File created successfully at '{path}' ({len(content)} chars)"

