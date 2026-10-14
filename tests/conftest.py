"""Pytest bootstrap: add project root to sys.path so 'from backend.x import y' works."""
import sys
from pathlib import Path

# Ensure the project root is first on the path
_PROJECT_ROOT = str(Path(__file__).parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

