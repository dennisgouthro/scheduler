"""Secret and PII scrubbing for run logs and AI diagnostic snippets."""

from __future__ import annotations

import re

_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.MULTILINE,
)

_KV_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|passphrase)\s*([=:])\s*([^\s'\"`,;]+|'[^']*'|\"[^\"]*\")"
)

_BEARER_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9\-._~+/]+=*")
_URL_CREDS_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^:\s/@]+:)([^@\s/]+)(@)")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def scrub_text(text: str) -> str:
    """Mask private keys, credentials, tokens, and SSNs before UI display."""
    if not text:
        return text
    out = _PEM_BLOCK_RE.sub("[PRIVATE KEY REDACTED]", text)
    out = _URL_CREDS_RE.sub(r"\1[REDACTED]\3", out)
    out = _BEARER_RE.sub(r"\1 [REDACTED]", out)
    out = _KV_SECRET_RE.sub(r"\1\2[REDACTED]", out)
    out = _SSN_RE.sub("[SSN REDACTED]", out)
    return out
