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

# The last word of a secret-bearing name. Mirrors the rule set of the Hermes
# integration (``cognee_integration_hermes.capture_policy._RULES``); the packages
# ship separately, so the rules are copied rather than imported.
_SECRET_WORD = (
    r"secret(?:[_-]?(?:access[_-]?)?key)?|token|password|passwd|pwd|api[_-]?key"
    r"|access[_-]?key(?:[_-]?id)?|session[_-]?token|auth[_-]?token"
)
_SECRET_TEXT_PATTERNS = [
    # An unterminated private key (clipped by an upstream cap) is still a key.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?)://[^\s\"'<>]+"),
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+"),
    # ``access_token=``, ``client_secret: "x"``, ``X-Api-Key=``: any prefixed secret word.
    re.compile(
        rf"(?i)[\"']?\b(?:(?:[\w-]*[_-])?(?:{_SECRET_WORD}|x-api-key)|_auth)"
        r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    ),
    re.compile(
        r"\b(?:sk-(?:proj-|ant-)?|gh[pousr]_|github_pat_|xox[baprs]-|whsec_)[A-Za-z0-9_-]{16,}"
        r"|\bnpm_[A-Za-z0-9]{36}\b|\bpypi-[A-Za-z0-9_-]{50,}"
    ),
    re.compile(
        r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|APKA|ABIA|ACCA)[A-Z0-9]{16}\b"
    ),
    re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}"),
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
    """Replace credential-looking spans with ``<redacted>``.

    Applied to everything that leaves the process as memory content *or* as a
    search query: cognee records queries server-side, so a secret in a recall
    query is stored just as a secret in a document would be.
    """
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
