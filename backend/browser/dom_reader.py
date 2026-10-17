"""
dom_reader.py
=============
Sanitized DOM visible text extractor, character purifier, and untrusted data wrapper.

Security Guarantees:
1. Strips scripts, styles, noscript, svg, canvas, template, and iframe elements.
2. Removes hidden elements (display:none, visibility:hidden, opacity:0, aria-hidden=true).
3. Strips zero-width, bidirectional (bidi) override, Unicode tag characters, and invisible controls.
4. Neutralizes closing-tag lookalikes to prevent data-boundary breakout attacks.
5. Escapes attributes on wrapper tags.
6. Caps extracted text at 4,000 characters.
7. Wraps page text in unique per-call random delimited <UNTRUSTED_WEB_DATA_{delim}> blocks.
"""

from __future__ import annotations

import html
import logging
import re
import secrets
from typing import Any

logger = logging.getLogger(__name__)

MAX_PAGE_TEXT_CHARS = 4000

# Zero-width, BiDi, and Unicode Tag character regex
# \u200B-\u200F: Zero-width space, ZWNJ, ZWJ, LRM, RLM
# \uFEFF: Zero-width no-break space (BOM)
# \u202A-\u202E: BiDi embedding and override controls (LRE, RLE, PDF, LRO, RLO)
# \u2066-\u2069: BiDi isolates (LRI, RLI, FSI, PDI)
# \U000E0000-\U000E007F: Unicode Tag characters
_DANGEROUS_UNICODE_PATTERN = re.compile(
    r"[\u200B-\u200F\uFEFF\u202A-\u202E\u2066-\u2069\U000E0000-\U000E007F]"
)

# Control characters except newline (\n), carriage return (\r), tab (\t)
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# In-page DOM extraction script executed via page.evaluate()
EXTRACT_VISIBLE_TEXT_JS = """
() => {
    // 1. Remove non-content and media tags
    const removeTags = ['script', 'style', 'noscript', 'svg', 'canvas', 'template', 'iframe', 'object', 'embed'];
    removeTags.forEach(tag => {
        document.querySelectorAll(tag).forEach(el => el.remove());
    });

    // 2. Helper to check if an element is rendered and visible
    function isVisible(el) {
        if (!el || el.nodeType !== Node.ELEMENT_NODE) return true;
        if (el.getAttribute('aria-hidden') === 'true' || el.hasAttribute('hidden')) return false;
        try {
            const style = window.getComputedStyle(el);
            if (!style) return true;
            if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) {
                return false;
            }
        } catch (e) {}
        return true;
    }

    // 3. Remove all hidden elements from the DOM copy
    const allElements = document.querySelectorAll('*');
    for (let i = allElements.length - 1; i >= 0; i--) {
        const el = allElements[i];
        if (!isVisible(el)) {
            el.remove();
        }
    }

    // 4. Return innerText of body
    return document.body ? document.body.innerText : '';
}
"""


def sanitize_text(text: str) -> str:
    """
    Purge zero-width characters, BiDi overrides, Unicode tags, and invisible controls.
    Neutralize any closing tag lookalikes.
    """
    if not text:
        return ""

    # 1. Strip dangerous Unicode characters
    clean = _DANGEROUS_UNICODE_PATTERN.sub("", text)

    # 2. Strip non-printable ASCII/Latin control characters
    clean = _CONTROL_CHAR_PATTERN.sub("", clean)

    # 3. Neutralize closing-tag lookalikes to prevent wrapper breakout
    # e.g. '</UNTRUSTED_WEB_DATA' -> '&lt;/UNTRUSTED_WEB_DATA'
    clean = re.sub(
        r"<\s*/\s*UNTRUSTED",
        "&lt;/UNTRUSTED",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(
        r"<\s*/\s*DATA",
        "&lt;/DATA",
        clean,
        flags=re.IGNORECASE,
    )

    return clean.strip()


def wrap_untrusted_content(
    text: str,
    domain: str,
    url: str,
    max_chars: int = MAX_PAGE_TEXT_CHARS,
) -> str:
    """
    Sanitize, size-cap, and wrap extracted page text in an unambiguous data block
    with a random per-call delimiter.
    """
    sanitized = sanitize_text(text)

    # Enforce maximum text length
    if len(sanitized) > max_chars:
        sanitized = sanitized[:max_chars].rstrip() + f"\n\n[... content clipped at {max_chars} characters ...]"

    # Random 16-character hex delimiter for the wrapper tags
    delim = secrets.token_hex(8)

    # Escape attributes to prevent injection into tag attributes
    safe_domain = html.escape(domain, quote=True)
    safe_url = html.escape(url, quote=True)

    header = f'<UNTRUSTED_WEB_DATA_{delim} domain="{safe_domain}" url="{safe_url}">'
    footer = f"</UNTRUSTED_WEB_DATA_{delim}>"

    return f"{header}\n{sanitized}\n{footer}"
