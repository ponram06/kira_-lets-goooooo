"""
safety.py
=========
Checks for M6 screen control that run before EVERY proposed action:

- refuse when the foreground window looks sensitive (password managers,
  banking/payment, Windows security and credential prompts, UAC);
- never type credentials, one-time codes or payment card numbers;
- only allow-listed keys (no Win-key combos, no Ctrl+Alt+Del);
- validate the action shape and that the target is on a real monitor.

Proposals come from a vision model that reads untrusted screen content, so
everything here assumes the proposal may be hostile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from backend import secrets_guard
from backend.screen.geometry import Rect, ScreenMap, on_any_monitor

ACTIONS = frozenset({"click", "double_click", "type", "key", "scroll", "done", "fail"})

SENSITIVE_TITLE_WORDS = (
    "password", "1password", "bitwarden", "keepass", "lastpass", "dashlane", "keeper",
    "bank", "banking", "paypal", "payment", "checkout", "credit card", "wallet",
    "windows security", "user account control", "credential", "sign in to windows",
    "authenticator", "one-time", "verification code", "bitlocker", "defender",
    "registry editor", "group policy", "local security policy",
)
SENSITIVE_PROCESSES = frozenset({
    "consent.exe", "credentialuibroker.exe", "lockapp.exe", "logonui.exe",
    "1password.exe", "bitwarden.exe", "keepass.exe", "keepassxc.exe", "lastpass.exe",
    "securityhealthui.exe", "securityhealthhost.exe", "regedit.exe", "mmc.exe",
})

_SIMPLE_KEYS = frozenset({
    "enter", "tab", "esc", "escape", "backspace", "delete", "space",
    "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
    "f2", "f3", "f5",
})
_CTRL_COMBOS = frozenset({"a", "c", "v", "x", "z", "y", "s", "f", "n", "t", "w", "l", "home", "end", "tab"})
MAX_TYPE_CHARS = 500


@dataclass
class Verdict:
    ok: bool
    reason: str = ""


def is_sensitive_window(title: str, process: str) -> Verdict:
    t = (title or "").lower()
    p = (process or "").lower()
    if p in SENSITIVE_PROCESSES:
        return Verdict(False, f"The foreground window ({p}) is a security or credential window.")
    for word in SENSITIVE_TITLE_WORDS:
        if word in t:
            return Verdict(False, f"The foreground window looks sensitive ('{word}').")
    return Verdict(True)


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def text_is_safe(text: str, field_hint: str = "") -> Verdict:
    """Refuse credentials, OTPs and card numbers, and anything in a password-like field."""
    if not isinstance(text, str) or not text:
        return Verdict(False, "Nothing to type.")
    if len(text) > MAX_TYPE_CHARS:
        return Verdict(False, f"Text is longer than {MAX_TYPE_CHARS} characters.")
    if any(ord(c) < 32 and c not in "\t" for c in text):
        return Verdict(False, "Text contains control characters or newlines.")
    hint = (field_hint or "").lower()
    if any(w in hint for w in ("password", "passcode", "pin", "otp", "one-time", "verification",
                               "card", "cvv", "cvc", "security code", "iban")):
        return Verdict(False, "Kira never types into password, code or payment fields. Please type it yourself.")
    if secrets_guard.contains_secret(text):
        return Verdict(False, "Text contains a stored secret.")
    compact = re.sub(r"[\s-]", "", text)
    if re.fullmatch(r"\d{4,8}", compact):
        return Verdict(False, "Text looks like a one-time code or PIN. Please type it yourself.")
    if re.fullmatch(r"\d{13,19}", compact) and _luhn(compact):
        return Verdict(False, "Text looks like a payment card number. Please type it yourself.")
    return Verdict(True)


def key_is_allowed(key: str) -> bool:
    k = (key or "").lower().replace(" ", "")
    if k in _SIMPLE_KEYS:
        return True
    parts = k.split("+")
    if len(parts) == 2 and parts[0] == "ctrl" and parts[1] in _CTRL_COMBOS:
        return True
    if len(parts) == 2 and parts[0] == "shift" and parts[1] in ("tab", "enter"):
        return True
    return False


def validate_action(action: Any, smap: ScreenMap, mons: list[Rect]) -> tuple[Verdict, Optional[tuple[int, int]]]:
    """Shape + bounds check. Returns (verdict, screen point or None)."""
    if not isinstance(action, dict):
        return Verdict(False, "The proposal is not an action object."), None
    kind = action.get("action")
    if kind not in ACTIONS:
        return Verdict(False, f"Unknown action '{kind}'."), None
    if kind in ("done", "fail"):
        return Verdict(True), None
    point = None
    if kind in ("click", "double_click", "scroll") or ("x" in action and "y" in action):
        try:
            ix, iy = int(action["x"]), int(action["y"])
        except (KeyError, TypeError, ValueError):
            return Verdict(False, "The action has no valid x/y coordinates."), None
        w, h = smap.image_size
        if not (0 <= ix < w and 0 <= iy < h):
            return Verdict(False, "The target is outside the screenshot."), None
        point = smap.to_screen(ix, iy)
        if not on_any_monitor(point[0], point[1], mons):
            return Verdict(False, "The target is not on a visible monitor."), None
    if kind == "type":
        v = text_is_safe(action.get("text", ""), str(action.get("field", "")))
        if not v.ok:
            return v, point
    if kind == "key" and not key_is_allowed(str(action.get("key", ""))):
        return Verdict(False, f"Key '{action.get('key')}' is not allowed."), point
    if kind == "scroll":
        try:
            amt = int(action.get("amount", 0))
        except (TypeError, ValueError):
            return Verdict(False, "Scroll amount is not a number."), point
        if not -20 <= amt <= 20 or amt == 0:
            return Verdict(False, "Scroll amount must be between -20 and 20."), point
    return Verdict(True), point
