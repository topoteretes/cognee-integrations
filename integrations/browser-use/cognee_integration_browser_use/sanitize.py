"""Sanitizers for what leaves the browser and enters memory.

Page text is untrusted input; URLs may carry credentials; nothing here ever
promotes stored text into instructions. The adapter never touches
``sensitive_data``, cookies, browser profiles, screenshots or model thoughts.
"""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SECRET_QUERY_KEYS = re.compile(
    r"(^|[_\-.])(token|key|secret|password|passwd|pwd|auth|authorization|session|sid|"
    r"sig|signature|api[_-]?key|access[_-]?token|refresh[_-]?token|code|credential|"
    r"bearer|jwt|otp)($|[_\-.])",
    re.IGNORECASE,
)

_SECRET_TEXT_PATTERNS = [
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]


def sanitize_url(url: str) -> str:
    """Drop userinfo and secret-looking query parameters; keep the rest intact."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    if parts.scheme.lower() not in {"http", "https"}:
        return ""
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _SECRET_QUERY_KEYS.search(key)
    ]
    query = urlencode(kept)
    return urlunsplit((parts.scheme.lower(), host, parts.path, query, ""))


def sanitize_urls(urls: Iterable[str | None], limit: int) -> list[str]:
    """Unique, sanitized, order-preserving URLs capped at ``limit``."""
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if not url:
            continue
        clean = sanitize_url(str(url))
        if not clean or clean in seen or clean.startswith("about:"):
            continue
        seen.add(clean)
        out.append(clean)
        if len(out) >= limit:
            break
    return out


def redact_text(text: str) -> str:
    out = text
    for pattern in _SECRET_TEXT_PATTERNS:
        out = pattern.sub("<redacted>", out)
    return out


def cap_text(text: str, max_chars: int, marker: str = " …[truncated]") -> str:
    text = text.replace("\r\n", "\n").strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - len(marker))].rstrip() + marker


def clean_for_memory(text: str, max_chars: int) -> str:
    """Redact, normalize and cap text destined for a memory record."""
    return cap_text(redact_text(text or ""), max_chars)
