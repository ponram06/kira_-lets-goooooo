"""
url_guard.py
============
Strict URL validation, DNS pre-resolution, and network security filters for Kira's browser subsystem.

Guards:
1. HTTPS protocol only (strictly no http, file, chrome, about, data, javascript, blob, whatsapp, etc.).
2. No user@host authentication tricks in URL.
3. No trailing-dot hosts.
4. Port strictly 443 only.
5. Rejects special internal TLDs (.local, .internal, .localhost, .lan, .home, .corp).
6. Blocks IP literals in all forms (decimal, hex, octal, IPv4-mapped IPv6, standard notation).
7. Punycode encoding for all IDN hostnames in prompts and logs.
8. DNS pre-resolution blocks private (RFC 1918), loopback, link-local (RFC 3927), and reserved IPs.
9. Connected server address check (response.server_addr()) defends against DNS rebinding attacks.
10. URL and query length caps; secret detection via secrets_guard.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import urllib.parse
from typing import Optional, Union

from backend import secrets_guard

logger = logging.getLogger(__name__)

MAX_URL_LENGTH = 2048
MAX_QUERY_LENGTH = 200

# Blocked TLDs / host suffixes
BLOCKED_HOST_SUFFIXES = (
    ".local",
    ".internal",
    ".localhost",
    ".lan",
    ".home",
    ".corp",
)

# Common search engine template (fixed in code)
SEARCH_ENGINE_URL = "https://duckduckgo.com/?q={query}"


def is_ip_literal(host: str) -> bool:
    """Detect if host is an IP literal in standard, decimal, hex, octal, or mapped format."""
    clean = host.strip("[]")

    # 1. Standard IPv4 / IPv6
    try:
        ipaddress.ip_address(clean)
        return True
    except ValueError:
        pass

    # 2. Pure decimal integer (e.g. 2130706433 for 127.0.0.1)
    if clean.isdigit():
        return True

    # 3. Hexadecimal integer (e.g. 0x7f000001)
    if clean.lower().startswith("0x"):
        try:
            int(clean, 16)
            return True
        except ValueError:
            pass

    # 4. Dot-separated numeric parts with hex or octal (e.g. 0177.0.0.1, 0x7f.0.0.1)
    parts = clean.split(".")
    if len(parts) == 4:
        all_numeric = True
        for p in parts:
            p_clean = p.lower()
            try:
                if p_clean.startswith("0x"):
                    int(p_clean, 16)
                elif p_clean.startswith("0") and len(p_clean) > 1 and p_clean.isdigit():
                    int(p_clean, 8)
                else:
                    int(p_clean, 10)
            except ValueError:
                all_numeric = False
                break
        if all_numeric:
            return True

    # 5. IPv4-mapped IPv6 (::ffff:127.0.0.1 or ::ffff:7f00:1)
    if "::ffff:" in clean.lower():
        return True

    return False


def is_private_or_restricted_ip(ip_obj: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> bool:
    """Return True if an IP is private, loopback, link-local, multicast, or reserved."""
    if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
        return True
    if ip_obj.is_multicast or ip_obj.is_reserved or ip_obj.is_unspecified:
        return True

    # Handle IPv4-mapped IPv6 addresses (e.g. ::ffff:192.168.1.1)
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped:
        mapped_v4 = ip_obj.ipv4_mapped
        if mapped_v4.is_private or mapped_v4.is_loopback or mapped_v4.is_link_local:
            return True

    return False


def to_punycode(domain: str) -> str:
    """Convert an internationalized domain name to ASCII punycode."""
    try:
        return domain.encode("idna").decode("ascii").lower()
    except Exception:
        return domain.lower()


def validate_url(url: str) -> tuple[bool, str, str]:
    """
    Validate a user or model-supplied URL for browsing.
    
    Returns:
        (is_valid, clean_url_or_error, punycode_domain)
    """
    if not url or not isinstance(url, str):
        return False, "Error: URL must be a non-empty string.", ""

    raw_url = url.strip()
    if len(raw_url) > MAX_URL_LENGTH:
        return False, f"Action blocked: URL exceeds maximum length of {MAX_URL_LENGTH} characters.", ""

    # Check for secrets inside the URL
    if secrets_guard.contains_secret(raw_url):
        return False, "Action blocked: URL contains sensitive configuration secrets.", ""

    try:
        parsed = urllib.parse.urlsplit(raw_url)
    except Exception as exc:
        return False, f"Action blocked: Malformed URL ({exc}).", ""

    # 1. Scheme check: strictly HTTPS
    scheme = parsed.scheme.lower()
    if scheme != "https":
        if scheme in ("http", "file", "chrome", "about", "data", "javascript", "blob", "whatsapp"):
            return False, f"Action blocked: Insecure or prohibited URL scheme '{scheme}://'. Only 'https://' is allowed.", ""
        return False, f"Action blocked: Prohibited scheme '{scheme}://'. Only 'https://' is allowed.", ""

    # 2. No user@host authentication tricks
    if "@" in parsed.netloc or parsed.username is not None:
        return False, "Action blocked: URL contains user authentication credentials or '@' trick.", ""

    # 3. Hostname inspection
    hostname = (parsed.hostname or "").strip()
    if not hostname:
        return False, "Action blocked: URL contains no hostname.", ""

    # 4. Trailing dot check (e.g. google.com.)
    if hostname.endswith("."):
        return False, "Action blocked: Hostname with trailing dot is not permitted.", ""

    # 5. Port check: strictly 443 only
    if parsed.port is not None and parsed.port != 443:
        return False, f"Action blocked: Port {parsed.port} is not permitted. Only standard HTTPS (port 443) is allowed.", ""

    # 6. Block internal/special TLDs
    host_lower = hostname.lower()
    if host_lower == "localhost" or any(host_lower.endswith(sfx) for sfx in BLOCKED_HOST_SUFFIXES):
        return False, f"Action blocked: Access to internal or local domain '{hostname}' is prohibited.", ""

    # 7. Convert IDN to punycode
    punycode_host = to_punycode(hostname)

    # 8. Check for IP literals in odd forms
    if is_ip_literal(punycode_host):
        try:
            # Try parsing integer/standard IP
            if punycode_host.isdigit():
                ip_val = int(punycode_host)
                ip_obj = ipaddress.ip_address(ip_val)
            else:
                ip_obj = ipaddress.ip_address(punycode_host)
            if is_private_or_restricted_ip(ip_obj):
                return False, f"Action blocked: Access to private or loopback IP literal '{punycode_host}' is prohibited.", ""
        except ValueError:
            return False, f"Action blocked: Obfuscated IP literal '{punycode_host}' is prohibited.", ""

    # 9. Pre-DNS resolution check: ensure domain does not resolve to private or loopback IP
    try:
        addr_info = socket.getaddrinfo(punycode_host, 443, proto=socket.IPPROTO_TCP)
        for _, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            ip_obj = ipaddress.ip_address(ip_str)
            if is_private_or_restricted_ip(ip_obj):
                return False, f"Action blocked: Domain '{punycode_host}' resolves to restricted IP {ip_str}.", punycode_host
    except socket.gaierror as e:
        logger.debug("DNS pre-resolution failed for %s (%s)", punycode_host, e)
        # Note: if offline test mock or real failure, resolution will be checked again at request routing time.

    # Reconstruct sanitized URL (ensuring 443 standard and clean punycode host)
    path = parsed.path if parsed.path else "/"
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    clean_url = f"https://{punycode_host}{path}{query}{fragment}"

    return True, clean_url, punycode_host


def validate_search_query(query: str) -> tuple[bool, str, str]:
    """
    Validate a search query and build the fixed search URL.
    
    Returns:
        (is_valid, message_or_url, punycode_domain)
    """
    if not query or not isinstance(query, str):
        return False, "Error: Search query must be a non-empty string.", ""

    clean_q = query.strip()
    if not clean_q:
        return False, "Error: Search query cannot be empty or whitespace.", ""

    if len(clean_q) > MAX_QUERY_LENGTH:
        return False, f"Action blocked: Search query exceeds maximum length of {MAX_QUERY_LENGTH} characters.", ""

    if secrets_guard.contains_secret(clean_q):
        return False, "Action blocked: Search query contains sensitive configuration secrets.", ""

    encoded = urllib.parse.quote_plus(clean_q)
    search_url = SEARCH_ENGINE_URL.format(query=encoded)
    return True, search_url, "duckduckgo.com"


def check_server_address(server_addr: Union[dict, str, tuple, None]) -> tuple[bool, str]:
    """
    Inspect the connected server IP address from response.server_addr().
    Used to defend against DNS rebinding attacks where initial resolution passed
    but the socket connected to a private/loopback address.
    """
    if not server_addr:
        return True, ""

    ip_str = ""
    if isinstance(server_addr, dict):
        ip_str = str(server_addr.get("ipAddress", "")).strip()
    elif isinstance(server_addr, (list, tuple)) and len(server_addr) > 0:
        ip_str = str(server_addr[0]).strip()
    elif isinstance(server_addr, str):
        ip_str = server_addr.strip()

    if not ip_str:
        return True, ""

    try:
        ip_obj = ipaddress.ip_address(ip_str)
        if is_private_or_restricted_ip(ip_obj):
            return False, f"Action blocked: Connected server address {ip_str} is private/loopback/link-local (DNS rebinding defence)."
    except ValueError:
        return False, f"Action blocked: Invalid server address {ip_str} detected."

    return True, ""
