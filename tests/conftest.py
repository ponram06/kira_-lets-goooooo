"""Pytest bootstrap: project root on sys.path, and every test isolated from
the real user settings folder and the real .env file."""
import sys
from pathlib import Path

import pytest

# Ensure the project root is first on the path
_PROJECT_ROOT = str(Path(__file__).parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Obvious fake PIN for tests — never a real PIN
TEST_PIN = "0000"


@pytest.fixture(autouse=True)
def _isolated_user_files(tmp_path, monkeypatch):
    """Point user settings and .env at a temp folder for every test."""
    monkeypatch.setenv("KIRA_USER_DIR", str(tmp_path / "user"))
    monkeypatch.setenv("KIRA_ENV_FILE", str(tmp_path / "test.env"))
    monkeypatch.delenv("KIRA_FREE_MODE_PIN", raising=False)
    from backend import secrets_guard
    secrets_guard.refresh()
    yield
    secrets_guard.refresh()
