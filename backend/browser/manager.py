"""
manager.py
==========
Singleton BrowserManager for Kira's headed Chromium browser automation.

Hardening & Invariants:
1. Persistent clean profile in %APPDATA%\\Kira\\browser_profile (never git-tracked,
   never uses personal Chrome/Edge profiles or cookies).
2. Visible (headed) window for real-time user observation.
3. Denies all permissions; blocks service workers; accept_downloads=False.
4. Auto-dismisses dialogs; cancels file choosers; closes popups and extra tabs immediately (1 tab cap).
5. Intercepts all network requests (main frame, subframes, resources); blocks non-HTTPS and private IPs.
6. Server address check on response (response.server_addr()) defends against DNS rebinding.
7. Taint tracking: after browser_read, new-domain navigation and typing require confirmation.
8. Ollama provider check: blocks automation when Ollama is active.
9. Kill switch check before every action; clean shutdown on exit (atexit).
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from backend import events as _events
from backend import secrets_guard
from backend.browser.dom_reader import (
    EXTRACT_VISIBLE_TEXT_JS,
    wrap_untrusted_content,
)
from backend.browser.interaction_guard import (
    INSPECT_CLICK_ELEMENT_JS,
    INSPECT_INPUT_SENSITIVITY_JS,
    check_click_confirmation_needed,
    check_typing_allowed,
)
from backend.browser.url_guard import (
    check_server_address,
    is_private_or_restricted_ip,
    to_punycode,
    validate_search_query,
    validate_url,
)
from backend.policy import KILL_SWITCH, Mode
from backend.user_config import user_dir

logger = logging.getLogger(__name__)

BROWSER_PROFILE_DIRNAME = "browser_profile"


def browser_profile_path() -> Path:
    """Return path to Kira's clean browser profile outside git."""
    p = user_dir(create=True) / BROWSER_PROFILE_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return p


class BrowserManager:
    """
    Singleton manager controlling the headed Playwright browser instance and context.
    """

    _instance: Optional[BrowserManager] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._playwright = None
        self._context = None
        self._page = None
        self._current_domain: str = ""
        self._current_url: str = ""
        self._is_tainted_by_web: bool = False
        self._active_task_lock = threading.Lock()
        self._rebinding_detected: bool = False
        self._rebinding_message: str = ""

    @classmethod
    def get_instance(cls) -> BrowserManager:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @property
    def is_tainted_by_web(self) -> bool:
        return self._is_tainted_by_web

    @property
    def current_domain(self) -> str:
        return self._current_domain

    @property
    def current_url(self) -> str:
        return self._current_url

    def reset_task_taint(self) -> None:
        """Reset web content taint at the beginning of a fresh user request."""
        self._is_tainted_by_web = False
        self._rebinding_detected = False
        self._rebinding_message = ""

    def _ensure_browser(self) -> Any:
        """Ensure headed Playwright browser context is running with hardened settings."""
        if self._page and not self._page.is_closed():
            return self._page

        from playwright.sync_api import sync_playwright

        if self._playwright is None:
            self._playwright = sync_playwright().start()

        profile_dir = str(browser_profile_path())

        # Hardened launch options
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            "--disable-extensions",
            "--disable-component-update",
        ]

        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            accept_downloads=False,
            ignore_https_errors=False,
            permissions=[],
            service_workers="block",
            args=args,
            viewport={"width": 1280, "height": 800},
        )

        # 1. Close extra tabs and popups immediately (cap at 1 tab)
        def on_page_created(new_page):
            if self._page and new_page != self._page:
                try:
                    logger.info("Closing unrequested popup/tab to maintain 1-tab limit.")
                    new_page.close()
                except Exception:
                    pass

        self._context.on("page", on_page_created)

        # Get the primary page
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = self._context.new_page()

        # 2. Hardened page listeners
        self._page.on("download", lambda d: d.cancel())
        self._page.on("dialog", lambda dlg: dlg.dismiss())
        self._page.on("filechooser", lambda fc: fc.set_files([]))

        # 3. Request route interception: check EVERY request (main frame, subframes, resources)
        def handle_route(route):
            req = route.request
            req_url = req.url

            # Block downloads or non-https requests
            is_valid, _, _ = validate_url(req_url)
            if not is_valid:
                logger.debug("Aborting disallowed request to: %s", req_url)
                route.abort()
                return

            route.continue_()

        self._page.route("**/*", handle_route)

        # 4. Response listener: inspect connected server IP for DNS rebinding defence
        def handle_response(response):
            try:
                server_addr = response.server_addr()
                if server_addr:
                    allowed, reason = check_server_address(server_addr)
                    if not allowed:
                        self._rebinding_detected = True
                        self._rebinding_message = reason
                        logger.warning("DNS rebinding attempt detected! %s. Closing page.", reason)
                        try:
                            self._page.close()
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("Failed reading response server_addr: %s", e)

        self._page.on("response", handle_response)

        return self._page

    def check_preconditions(self, active_provider: Optional[str] = None) -> tuple[bool, str]:
        """Verify kill switch, provider restrictions, and rebinding state."""
        if KILL_SWITCH.is_stopped():
            return False, "Action blocked: kill switch is active."

        if active_provider and ("ollama" in active_provider.lower() or "qwen2.5" in active_provider.lower()):
            return False, "Action blocked: Browser automation is disabled when running on the local Ollama model."

        if self._rebinding_detected:
            return False, f"Action blocked: {self._rebinding_message}"

        return True, ""

    def open_url(
        self,
        url: str,
        active_provider: Optional[str] = None,
        confirm_cb: Optional[Callable[[str], bool]] = None,
    ) -> str:
        """Open an HTTPS URL in the browser with security checks and taint tracking."""
        ok, reason = self.check_preconditions(active_provider)
        if not ok:
            return reason

        is_valid, clean_url, punycode_domain = validate_url(url)
        if not is_valid:
            return clean_url

        # Taint Rule: if task is already tainted by web content and domain differs, ask confirmation
        if self._is_tainted_by_web and self._current_domain and punycode_domain != self._current_domain:
            prompt = (
                f"Confirmation required: Following web content, Kira would like to open "
                f"'{punycode_domain}'. Allow?"
            )
            if confirm_cb:
                if not confirm_cb(prompt):
                    return "Action cancelled by user."
            else:
                return prompt

        page = self._ensure_browser()

        try:
            _events.publish(
                "browser.open",
                f"Navigating to {punycode_domain}",
                {"domain": punycode_domain, "action": "open"},
            )
            page.goto(clean_url, wait_until="domcontentloaded", timeout=10000)
            self._current_domain = punycode_domain
            self._current_url = clean_url

            if self._rebinding_detected:
                return f"Action blocked: {self._rebinding_message}"

            return f"Opened {punycode_domain}."
        except Exception as exc:
            if self._rebinding_detected:
                return f"Action blocked: {self._rebinding_message}"
            logger.error("Failed opening URL: %s", exc)
            return f"Error opening page: {exc}"

    def search(
        self,
        query: str,
        active_provider: Optional[str] = None,
        confirm_cb: Optional[Callable[[str], bool]] = None,
    ) -> str:
        """Perform a web search using fixed search engine template in code."""
        ok, reason = self.check_preconditions(active_provider)
        if not ok:
            return reason

        is_valid, search_url, domain = validate_search_query(query)
        if not is_valid:
            return search_url

        return self.open_url(search_url, active_provider=active_provider, confirm_cb=confirm_cb)

    def read_page(self, active_provider: Optional[str] = None) -> str:
        """Extract sanitized visible page text wrapped in unique random-delimited block."""
        ok, reason = self.check_preconditions(active_provider)
        if not ok:
            return reason

        page = self._ensure_browser()

        try:
            raw_text = page.evaluate(EXTRACT_VISIBLE_TEXT_JS)
            self._is_tainted_by_web = True

            wrapped = wrap_untrusted_content(
                text=raw_text or "",
                domain=self._current_domain or "unknown",
                url=self._current_url or "",
            )

            _events.publish(
                "browser.read",
                f"Read page content from {self._current_domain}",
                {"domain": self._current_domain, "action": "read"},
            )
            return wrapped
        except Exception as exc:
            logger.error("Failed reading page: %s", exc)
            return f"Error reading page content: {exc}"

    def click(
        self,
        target: str,
        active_provider: Optional[str] = None,
        mode: Mode = Mode.RESTRICTED,
        confirm_cb: Optional[Callable[[str], bool]] = None,
    ) -> str:
        """Click an element by visible text or role, asking confirmation if sensitive or in form."""
        ok, reason = self.check_preconditions(active_provider)
        if not ok:
            return reason

        clean_target = (target or "").strip()
        if not clean_target:
            return "Error: Target to click cannot be empty."

        page = self._ensure_browser()

        try:
            # Locate element by text, role, or label (never coordinates)
            locator = (
                page.get_by_role("button", name=clean_target)
                .or_(page.get_by_role("link", name=clean_target))
                .or_(page.get_by_text(clean_target, exact=False))
                .or_(page.locator(f"input[type='submit'][value*='{clean_target}']"))
            ).first

            if not locator.count():
                return f"Error: Could not locate clickable element matching '{clean_target}'."

            # Inspect element for form / button / high-risk action
            element_handle = locator.element_handle()
            meta = page.evaluate(INSPECT_CLICK_ELEMENT_JS, element_handle)

            # Check if confirmation is required
            needs_confirm, prompt = check_click_confirmation_needed(meta, self._current_domain, mode)
            if needs_confirm:
                if confirm_cb:
                    if not confirm_cb(prompt):
                        return "Action cancelled by user."
                else:
                    return prompt

            # Execute click using Playwright locator API
            locator.click(timeout=5000)

            btn_text = meta.get("text", clean_target)[:30]
            _events.publish(
                "browser.click",
                f"Clicked '{btn_text}' on {self._current_domain}",
                {"domain": self._current_domain, "action": "click"},
            )
            return f"Clicked '{btn_text}'."

        except Exception as exc:
            logger.error("Failed clicking element '%s': %s", clean_target, exc)
            return f"Error clicking element '{clean_target}': {exc}"

    def type_text(
        self,
        field_label: str,
        text: str,
        active_provider: Optional[str] = None,
        confirm_cb: Optional[Callable[[str], bool]] = None,
    ) -> str:
        """Type text into a field located by label or placeholder without pressing Enter."""
        ok, reason = self.check_preconditions(active_provider)
        if not ok:
            return reason

        clean_label = (field_label or "").strip()
        if not clean_label:
            return "Error: Field label cannot be empty."

        # Taint Rule: if task is tainted by web content, every typing action must ask first
        if self._is_tainted_by_web:
            prompt = (
                f"Confirmation required: Following web content, Kira would like to type "
                f"into '{clean_label}'. Allow?"
            )
            if confirm_cb:
                if not confirm_cb(prompt):
                    return "Action cancelled by user."
            else:
                return prompt

        page = self._ensure_browser()

        try:
            # Locate input element by label or placeholder
            locator = (
                page.get_by_label(clean_label)
                .or_(page.get_by_placeholder(clean_label))
                .or_(page.locator(f"input[name*='{clean_label}']"))
                .or_(page.locator(f"textarea[name*='{clean_label}']"))
            ).first

            if not locator.count():
                return f"Error: Could not locate input field matching '{clean_label}'."

            element_handle = locator.element_handle()
            meta = page.evaluate(INSPECT_INPUT_SENSITIVITY_JS, element_handle)

            # Security verification: refuse passwords, OTP, payment fields, or config secrets
            allowed, refusal_reason = check_typing_allowed(meta, clean_label, text)
            if not allowed:
                return refusal_reason

            # Type strictly using locator.fill() (NEVER presses Enter or submits)
            locator.fill(text, timeout=5000)

            char_count = len(text)
            _events.publish(
                "browser.type",
                f"Typed {char_count} chars into '{clean_label}' on {self._current_domain}",
                {"domain": self._current_domain, "action": "type"},
            )
            return f"Typed {char_count} characters into '{clean_label}'."

        except Exception as exc:
            logger.error("Failed typing into '%s': %s", clean_label, exc)
            return f"Error typing into field '{clean_label}': {exc}"

    def scroll(
        self,
        direction: str = "down",
        amount: int = 500,
        active_provider: Optional[str] = None,
    ) -> str:
        """Scroll the page viewport up or down."""
        ok, reason = self.check_preconditions(active_provider)
        if not ok:
            return reason

        page = self._ensure_browser()

        try:
            delta = amount if direction.lower() == "down" else -abs(amount)
            page.evaluate(f"window.scrollBy(0, {delta})")
            return f"Scrolled {direction} by {abs(amount)} pixels."
        except Exception as exc:
            return f"Error scrolling page: {exc}"

    def back(self, active_provider: Optional[str] = None) -> str:
        """Navigate back in page history."""
        ok, reason = self.check_preconditions(active_provider)
        if not ok:
            return reason

        page = self._ensure_browser()

        try:
            page.go_back(timeout=5000)
            return "Navigated back."
        except Exception as exc:
            return f"Error navigating back: {exc}"

    def close(self) -> str:
        """Close current browser page/context cleanly."""
        try:
            if self._page and not self._page.is_closed():
                self._page.close()
            if self._context:
                self._context.close()
            self._page = None
            self._context = None
            self._current_domain = ""
            self._current_url = ""
            self._is_tainted_by_web = False
            return "Browser closed."
        except Exception as exc:
            return f"Error closing browser: {exc}"

    def shutdown(self) -> None:
        """Clean shutdown hook called on backend exit."""
        try:
            self.close()
            if self._playwright:
                self._playwright.stop()
                self._playwright = None
        except Exception:
            pass


# Global singleton manager and atexit hook
BROWSER_MANAGER = BrowserManager.get_instance()
atexit.register(BROWSER_MANAGER.shutdown)
