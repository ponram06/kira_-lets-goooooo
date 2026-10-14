"""
providers.py
============
LLM provider manager with ordered fallback and per-provider cooldowns.

Provider list, model names, and cooldown durations come from config.yaml.
API keys come from .env.  Neither is ever logged.

Usage (async)::

    from backend.providers import ProviderManager
    mgr = ProviderManager()
    reply, provider_name = await mgr.ask(messages)
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)

load_dotenv()  # Load .env into os.environ at import time

logger = logging.getLogger(__name__)

# Default config path: jarvis/config.yaml (one level above this file's package)
_DEFAULT_CONFIG = Path(__file__).parent.parent / "config.yaml"

# Keywords that distinguish quota / daily-limit errors from transient 429s
_QUOTA_KEYWORDS = ("quota", "daily", "exceeded", "resource_exhausted", "billing")


def _load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class ProviderManager:
    """
    Manages a priority-ordered list of OpenAI-compatible LLM providers.

    Cooldown rules
    --------------
    - HTTP 429 (transient rate limit)  → ``cooldowns.rate_limit`` seconds
    - HTTP 429 with quota keywords     → ``cooldowns.quota`` seconds
    - HTTP 5xx                         → ``cooldowns.server_error`` seconds
    - Timeout / connection error       → ``cooldowns.timeout`` seconds
    - HTTP 401 / 403 (bad key)         → disabled for the session
    - Missing env var                  → provider silently skipped at startup
    """

    def __init__(self, config_path: Path = _DEFAULT_CONFIG) -> None:
        cfg = _load_config(config_path)

        self._cooldown_cfg: dict = cfg.get("cooldowns", {})
        self._providers: list[dict] = []          # ordered list of live providers
        self._cooldown_until: dict[str, float] = {}  # name -> monotonic deadline
        self._disabled: set[str] = set()          # session-disabled (bad key)

        for p in cfg.get("providers", []):
            name: str = p["name"]
            api_key_env: Optional[str] = p.get("api_key_env")
            api_key: Optional[str] = None

            if api_key_env:
                api_key = os.getenv(api_key_env)
                if not api_key:
                    # Missing key — skip silently (do NOT log the var name at WARNING)
                    logger.debug("Provider '%s': %s not set — skipping.", name, api_key_env)
                    continue

            client = AsyncOpenAI(
                base_url=p["base_url"],
                api_key=api_key or "no-key",  # Ollama accepts any non-empty string
            )
            self._providers.append(
                {"name": name, "client": client, "model": p["model"]}
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_available(self, name: str) -> bool:
        """Return True if the provider is not disabled and not in cooldown."""
        if name in self._disabled:
            return False
        return time.monotonic() >= self._cooldown_until.get(name, 0.0)

    def _cooldown(self, name: str, key: str) -> None:
        """Put a provider in cooldown for the duration specified by ``key``."""
        seconds: int = self._cooldown_cfg.get(key, 60)
        self._cooldown_until[name] = time.monotonic() + seconds
        logger.warning(
            "Provider '%s' → cooldown %d s (%s).", name, seconds, key
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ask(
        self,
        messages: list[dict],
        tools: Optional[list] = None,
    ) -> tuple[str, Optional[str]]:
        """
        Send *messages* to the first available provider and return
        ``(reply_text, provider_name)``.

        If every provider is unavailable, returns a human-readable error
        string with ``None`` as the provider name.

        Parameters
        ----------
        messages:
            OpenAI-style message list (role / content dicts).
        tools:
            Optional list of tool schemas (passed through unchanged).
        """
        for provider in self._providers:
            name: str = provider["name"]

            if not self._is_available(name):
                logger.debug("Skipping '%s' (cooldown / disabled).", name)
                continue

            try:
                kwargs: dict = {"model": provider["model"], "messages": messages}
                if tools:
                    kwargs["tools"] = tools

                response = await provider["client"].chat.completions.create(**kwargs)
                content: str = response.choices[0].message.content or ""
                # Log only length — never the content itself
                logger.info("Provider '%s' replied (%d chars).", name, len(content))
                return content, name

            except RateLimitError as exc:
                err_lower = str(exc).lower()
                if any(kw in err_lower for kw in _QUOTA_KEYWORDS):
                    logger.warning(
                        "Provider '%s': quota / daily-limit reached.", name
                    )
                    self._cooldown(name, "quota")
                else:
                    logger.warning("Provider '%s': rate-limited (429).", name)
                    self._cooldown(name, "rate_limit")

            except (AuthenticationError, PermissionDeniedError):
                # 401 / 403 — the key is wrong; no point retrying this session
                logger.error(
                    "Provider '%s': authentication error (401/403). "
                    "Verify the API key in .env.  Disabling for this session.",
                    name,
                )
                self._disabled.add(name)

            except APITimeoutError:
                logger.warning("Provider '%s': request timed out.", name)
                self._cooldown(name, "timeout")

            except APIConnectionError:
                logger.warning("Provider '%s': connection error.", name)
                self._cooldown(name, "timeout")

            except APIStatusError as exc:
                if 500 <= exc.status_code < 600:
                    logger.warning(
                        "Provider '%s': server error %d.", name, exc.status_code
                    )
                    self._cooldown(name, "server_error")
                else:
                    # Unexpected 4xx we don't handle specifically — log and skip
                    logger.error(
                        "Provider '%s': unexpected API status %d.",
                        name,
                        exc.status_code,
                    )

        return (
            "⚠️  All LLM providers are currently unavailable.  "
            "Check your API keys in .env, your network connection, "
            "and try again in a few minutes.",
            None,
        )

