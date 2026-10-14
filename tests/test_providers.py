"""
test_providers.py
=================
Tests for backend/providers.py

All network I/O is mocked.  Real AsyncOpenAI clients are built during
ProviderManager.__init__ but their ``chat.completions.create`` coroutine
is replaced with AsyncMock before any call is made.

Tested behaviours
-----------------
- Happy path: first provider answers
- HTTP 429 → 15-min cooldown, fallback to next provider
- Quota / daily-limit 429 → quota cooldown (configurable)
- Timeout → 1-min cooldown, fallback
- Connection error → 1-min cooldown, fallback
- HTTP 5xx → 1-min cooldown, fallback
- HTTP 401/403 → provider disabled for session, fallback
- Cooled-down provider is skipped entirely
- Cooldown expiry: provider becomes available again
- All providers unavailable → human-readable error string, None provider
- Missing API key → provider silently skipped at startup

Note: openai 3.x uses httpx2 for its internal HTTP transport.
Exception constructors (APIStatusError subclasses) require httpx2.Request
and httpx2.Response objects.  We import httpx2 with a plain-httpx fallback
so the tests also work with older openai 1.x installs.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

# openai 3.x ships with httpx2 as its HTTP layer.
# Use httpx2 for exception helpers so type checks inside openai pass.
try:
    import httpx2 as _hx
except ImportError:         # older openai 1.x used plain httpx
    import httpx as _hx     # type: ignore[no-redef]

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)

from backend.providers import ProviderManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "providers": [
        {
            "name": "alpha",
            "base_url": "http://alpha/v1",
            "api_key_env": "KEY_ALPHA",
            "model": "model-alpha",
        },
        {
            "name": "beta",
            "base_url": "http://beta/v1",
            "api_key_env": "KEY_BETA",
            "model": "model-beta",
        },
    ],
    "cooldowns": {
        "rate_limit": 900,   # 15 min
        "timeout": 60,       # 1 min
        "server_error": 60,
        "quota": 3600,       # 1 hr
    },
    "memory_limit": 20,
}

_MESSAGES = [{"role": "user", "content": "hello"}]


def _make_response(content: str = "OK") -> MagicMock:
    """Build a minimal mock that looks like an openai ChatCompletion."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _make_exc(cls, status: int = 429, message: str = "error") -> Exception:
    """Build an openai APIStatusError subclass with the right httpx(2) types."""
    req = _hx.Request("POST", "http://test")
    resp = _hx.Response(status, request=req)
    return cls(message, response=resp, body=None)


def _make_timeout() -> APITimeoutError:
    return APITimeoutError(request=_hx.Request("POST", "http://test"))


def _make_connection_error() -> APIConnectionError:
    return APIConnectionError(
        message="Connection refused", request=_hx.Request("POST", "http://test")
    )


def _make_5xx(status: int = 500) -> APIStatusError:
    req = _hx.Request("POST", "http://test")
    resp = _hx.Response(status, request=req)
    return APIStatusError("Server error", response=resp, body=None)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProviderManager:
    """
    ProviderManager loaded from a temp config with two mock providers.
    Each provider's ``chat.completions.create`` is pre-wired as an AsyncMock
    returning a success response; individual tests override side_effect as needed.
    """
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(_BASE_CONFIG), encoding="utf-8")

    monkeypatch.setenv("KEY_ALPHA", "test-key-alpha")
    monkeypatch.setenv("KEY_BETA", "test-key-beta")

    mgr = ProviderManager(config_file)

    # Replace real AsyncOpenAI clients with lightweight mocks
    for p in mgr._providers:
        client = MagicMock()
        client.chat.completions.create = AsyncMock(
            return_value=_make_response(f"OK from {p['name']}")
        )
        p["client"] = client

    return mgr


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


async def test_first_provider_answers(manager: ProviderManager) -> None:
    reply, name = await manager.ask(_MESSAGES)
    assert name == "alpha"
    assert reply == "OK from alpha"


async def test_tools_passed_through(manager: ProviderManager) -> None:
    tools = [{"type": "function", "function": {"name": "dummy"}}]
    await manager.ask(_MESSAGES, tools=tools)
    call_kwargs = manager._providers[0]["client"].chat.completions.create.call_args
    assert call_kwargs.kwargs.get("tools") == tools


# ---------------------------------------------------------------------------
# Tests — HTTP 429 / rate limit
# ---------------------------------------------------------------------------


async def test_rate_limit_triggers_15min_cooldown(manager: ProviderManager) -> None:
    exc = _make_exc(RateLimitError, 429, "rate limited")
    manager._providers[0]["client"].chat.completions.create.side_effect = exc

    reply, name = await manager.ask(_MESSAGES)

    # Fell back to beta
    assert name == "beta"
    # alpha has a ~900 s cooldown
    remaining = manager._cooldown_until.get("alpha", 0) - time.monotonic()
    assert 800 < remaining <= 900, f"Expected ~900 s cooldown, got {remaining:.1f} s"


async def test_rate_limit_fallback_skips_alpha(manager: ProviderManager) -> None:
    exc = _make_exc(RateLimitError, 429)
    manager._providers[0]["client"].chat.completions.create.side_effect = exc

    _, name = await manager.ask(_MESSAGES)
    assert name == "beta"
    manager._providers[0]["client"].chat.completions.create.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — quota / daily-limit (special 429 sub-case)
# ---------------------------------------------------------------------------


async def test_quota_error_triggers_long_cooldown(manager: ProviderManager) -> None:
    exc = _make_exc(RateLimitError, 429, "quota exceeded for this billing period")
    manager._providers[0]["client"].chat.completions.create.side_effect = exc

    _, name = await manager.ask(_MESSAGES)
    assert name == "beta"
    remaining = manager._cooldown_until.get("alpha", 0) - time.monotonic()
    assert 3500 < remaining <= 3600, f"Expected ~3600 s quota cooldown, got {remaining:.1f} s"


async def test_daily_limit_keyword_triggers_quota_cooldown(manager: ProviderManager) -> None:
    exc = _make_exc(RateLimitError, 429, "daily limit reached")
    manager._providers[0]["client"].chat.completions.create.side_effect = exc

    _, name = await manager.ask(_MESSAGES)
    remaining = manager._cooldown_until.get("alpha", 0) - time.monotonic()
    assert remaining > 3000


# ---------------------------------------------------------------------------
# Tests — timeouts and connection errors (1-min cooldown)
# ---------------------------------------------------------------------------


async def test_timeout_triggers_1min_cooldown(manager: ProviderManager) -> None:
    manager._providers[0]["client"].chat.completions.create.side_effect = (
        _make_timeout()
    )
    _, name = await manager.ask(_MESSAGES)
    assert name == "beta"
    remaining = manager._cooldown_until.get("alpha", 0) - time.monotonic()
    assert 50 < remaining <= 60, f"Expected ~60 s cooldown, got {remaining:.1f} s"


async def test_connection_error_triggers_1min_cooldown(manager: ProviderManager) -> None:
    manager._providers[0]["client"].chat.completions.create.side_effect = (
        _make_connection_error()
    )
    _, name = await manager.ask(_MESSAGES)
    assert name == "beta"
    remaining = manager._cooldown_until.get("alpha", 0) - time.monotonic()
    assert 50 < remaining <= 60


# ---------------------------------------------------------------------------
# Tests — HTTP 5xx
# ---------------------------------------------------------------------------


async def test_5xx_triggers_1min_cooldown(manager: ProviderManager) -> None:
    manager._providers[0]["client"].chat.completions.create.side_effect = _make_5xx(503)
    _, name = await manager.ask(_MESSAGES)
    assert name == "beta"
    remaining = manager._cooldown_until.get("alpha", 0) - time.monotonic()
    assert 50 < remaining <= 60


# ---------------------------------------------------------------------------
# Tests — 401 / 403 bad key
# ---------------------------------------------------------------------------


async def test_401_disables_provider_for_session(manager: ProviderManager) -> None:
    exc = _make_exc(AuthenticationError, 401)
    manager._providers[0]["client"].chat.completions.create.side_effect = exc

    _, name = await manager.ask(_MESSAGES)
    assert name == "beta"
    assert "alpha" in manager._disabled


async def test_403_disables_provider_for_session(manager: ProviderManager) -> None:
    exc = _make_exc(PermissionDeniedError, 403)
    manager._providers[0]["client"].chat.completions.create.side_effect = exc

    _, name = await manager.ask(_MESSAGES)
    assert "alpha" in manager._disabled


async def test_disabled_provider_stays_disabled(manager: ProviderManager) -> None:
    """After a 401, alpha must not be contacted again even if called twice."""
    exc = _make_exc(AuthenticationError, 401)
    manager._providers[0]["client"].chat.completions.create.side_effect = exc

    await manager.ask(_MESSAGES)   # first call disables alpha
    await manager.ask(_MESSAGES)   # second call must still skip alpha

    # alpha's create was called exactly once (on the first ask)
    manager._providers[0]["client"].chat.completions.create.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — cooldown state
# ---------------------------------------------------------------------------


async def test_cooled_down_provider_is_skipped(manager: ProviderManager) -> None:
    manager._cooldown_until["alpha"] = time.monotonic() + 900

    _, name = await manager.ask(_MESSAGES)
    assert name == "beta"
    manager._providers[0]["client"].chat.completions.create.assert_not_called()


async def test_expired_cooldown_provider_is_used(manager: ProviderManager) -> None:
    # Cooldown already in the past
    manager._cooldown_until["alpha"] = time.monotonic() - 1

    _, name = await manager.ask(_MESSAGES)
    assert name == "alpha"


# ---------------------------------------------------------------------------
# Tests — all providers unavailable
# ---------------------------------------------------------------------------


async def test_all_fail_returns_error_and_none_provider(manager: ProviderManager) -> None:
    manager._providers[0]["client"].chat.completions.create.side_effect = (
        _make_timeout()
    )
    manager._providers[1]["client"].chat.completions.create.side_effect = (
        _make_connection_error()
    )

    reply, name = await manager.ask(_MESSAGES)
    assert name is None
    assert "unavailable" in reply.lower()


# ---------------------------------------------------------------------------
# Tests — missing API key at startup
# ---------------------------------------------------------------------------


async def test_missing_api_key_skips_provider_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider whose env var is absent must be skipped at init, not crash."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(_BASE_CONFIG), encoding="utf-8")

    # Only KEY_BETA is set — KEY_ALPHA is absent
    monkeypatch.delenv("KEY_ALPHA", raising=False)
    monkeypatch.setenv("KEY_BETA", "test-key-beta")

    mgr = ProviderManager(config_file)
    assert len(mgr._providers) == 1
    assert mgr._providers[0]["name"] == "beta"
