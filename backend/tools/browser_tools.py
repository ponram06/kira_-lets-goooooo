"""
browser_tools.py
================
Playwright browser automation tools for Kira.

Tools:
- browser_open(url)
- browser_search(query)
- browser_read()
- browser_click(target)
- browser_type(field_label, text)
- browser_scroll(direction, amount)
- browser_back()
- browser_close()
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from backend.browser.manager import BROWSER_MANAGER
from backend.policy import Mode

logger = logging.getLogger(__name__)


def browser_open(
    url: str,
    active_provider: Optional[str] = None,
    confirm_cb: Optional[Callable[[str], bool]] = None,
) -> str:
    """
    Open an HTTPS website in the headed browser.
    Validates URL safety, checks DNS resolution, and intercepts redirects.
    """
    return BROWSER_MANAGER.open_url(url, active_provider=active_provider, confirm_cb=confirm_cb)


def browser_search(
    query: str,
    active_provider: Optional[str] = None,
    confirm_cb: Optional[Callable[[str], bool]] = None,
) -> str:
    """
    Perform a search using a fixed safe search engine template in code.
    Query length is capped and checked against secrets.
    """
    return BROWSER_MANAGER.search(query, active_provider=active_provider, confirm_cb=confirm_cb)


def browser_read(active_provider: Optional[str] = None) -> str:
    """
    Read visible, sanitized text from the current browser page.
    Scripts and hidden elements are removed; output is wrapped in an untrusted data block.
    """
    return BROWSER_MANAGER.read_page(active_provider=active_provider)


def browser_click(
    target: str,
    active_provider: Optional[str] = None,
    mode: Mode = Mode.RESTRICTED,
    confirm_cb: Optional[Callable[[str], bool]] = None,
) -> str:
    """
    Click an element by its visible text or role.
    In Restricted mode, any button, submit input, or form element asks first.
    """
    return BROWSER_MANAGER.click(target, active_provider=active_provider, mode=mode, confirm_cb=confirm_cb)


def browser_type(
    field_label: str,
    text: str,
    active_provider: Optional[str] = None,
    confirm_cb: Optional[Callable[[str], bool]] = None,
) -> str:
    """
    Type text into an input field matching field_label without pressing Enter.
    Refuses passwords, OTPs, and payment fields.
    """
    return BROWSER_MANAGER.type_text(field_label, text, active_provider=active_provider, confirm_cb=confirm_cb)


def browser_scroll(
    direction: str = "down",
    amount: int = 500,
    active_provider: Optional[str] = None,
) -> str:
    """
    Scroll the active page viewport up or down.
    """
    return BROWSER_MANAGER.scroll(direction=direction, amount=amount, active_provider=active_provider)


def browser_back(active_provider: Optional[str] = None) -> str:
    """
    Navigate back in browser history.
    """
    return BROWSER_MANAGER.back(active_provider=active_provider)


def browser_close() -> str:
    """
    Close the current browser page and context.
    """
    return BROWSER_MANAGER.close()
