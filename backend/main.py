"""
main.py
=======
Entry point for the Kira backend.

    python -m backend.main               terminal + voice (run.bat)
    python -m backend.main --headless    started by the avatar app (no terminal input)

Sets up logging with secret redaction before anything else runs, and wraps
stdout/stderr so nothing printed can leak a secret into logs/backend.log
(which the avatar app writes from this process's output in headless mode).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from backend import secrets_guard


def setup_logging(headless: bool) -> None:
    """WARNING+ to stderr, with secrets redacted everywhere."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s  %(name)s: %(message)s" if headless
        else "%(levelname)s  %(name)s: %(message)s",
    )
    secrets_guard.install_log_redaction()


def wrap_std_streams() -> None:
    """Redact secrets from anything printed (idempotent)."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except Exception:
                pass
        if not isinstance(stream, secrets_guard.RedactingStream):
            setattr(sys, name, secrets_guard.RedactingStream(stream))


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m backend.main", description="Kira backend")
    parser.add_argument("--headless", action="store_true", help="no terminal input (used by the avatar app)")
    parser.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    args = parser.parse_args(argv)

    wrap_std_streams()
    setup_logging(args.headless)

    from backend.brain import chat_loop, _DEFAULT_CONFIG
    try:
        asyncio.run(chat_loop(args.config or _DEFAULT_CONFIG, headless=args.headless))
    except KeyboardInterrupt:
        print("\nInterrupted. Goodbye.")


if __name__ == "__main__":
    main()
