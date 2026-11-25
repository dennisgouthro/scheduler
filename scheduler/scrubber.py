"""Secret and PII scrubbing for run logs and AI diagnostic snippets."""

from __future__ import annotations

import re

_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))"
)

_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.MULTILINE,
)
_PEM_UNCLOSED_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*$",
    re.MULTILINE,
)
_PEM_BEGIN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PEM_END_RE = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")

_URL_CREDS_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^:\s/@]+:)([^@\s/]+)(@)")

_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|sig|key|api[_-]?key|access[_-]?token|secret|client[_-]?secret|password|passwd|pwd|auth)=)([^&\s'\"`#]+)"
)

_BEARER_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9\-._~+/]+=*")

_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")

_CLOUD_TOKEN_RE = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{20,})\b"
)

_PS_SECURESTRING_RE = re.compile(
    r"""(?i)(\bConvertTo-SecureString\b(?:\s+-(?:AsPlainText|Force|Key|SecureKey)\b)*\s+(?:-String\s+)?)(?!-)('[^']*'|"[^"]*"|[^\s'"`]+)"""
)

_CLI_FLAG_SECRET_RE = re.compile(
    r"""(?i)((?:^|[\s(])-{1,2}(?:[a-z0-9_-]*?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|passphrase))\s+)(?!\[[A-Z ]*REDACTED\])('[^']*'|"[^"]*"|[^\s'"`)]+)"""
)

_KV_SECRET_RE = re.compile(
    r"""(?i)(?<![A-Za-z0-9_.-])(["']?[A-Za-z0-9_.-]*?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|passphrase|account[_-]?key|auth[_-]?key)[A-Za-z0-9_.-]*["']?\s*[:=]\s*)(?!\[[A-Z ]*REDACTED\])('[^']*'|"[^"]*"|[^\s'"`,;}\]&]+)"""
)

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def _scrub_inline(text: str) -> str:
    """Apply single-line/inline secret patterns to ANSI-cleaned text."""
    out = _URL_CREDS_RE.sub(r"\1[REDACTED]\3", text)
    out = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", out)
    out = _BEARER_RE.sub(r"\1 [REDACTED]", out)
    out = _JWT_RE.sub("[JWT REDACTED]", out)
    out = _CLOUD_TOKEN_RE.sub("[TOKEN REDACTED]", out)
    out = _PS_SECURESTRING_RE.sub(r"\1[REDACTED]", out)
    out = _CLI_FLAG_SECRET_RE.sub(r"\1[REDACTED]", out)
    out = _KV_SECRET_RE.sub(r"\1[REDACTED]", out)
    out = _SSN_RE.sub("[SSN REDACTED]", out)
    return out


class LogScrubber:
    """Stateful streaming log scrubber that redacts multiline PEM blocks and inline secrets."""

    def __init__(self) -> None:
        self._in_pem_block = False

    def reset(self) -> None:
        self._in_pem_block = False

    def scrub_line(self, line: str) -> str:
        """Scrub a single streamed log line, tracking multiline private key blocks across calls."""
        if not line:
            return line

        clean = _ANSI_ESCAPE_RE.sub("", line)

        if self._in_pem_block:
            end_match = _PEM_END_RE.search(clean)
            if not end_match:
                return ""
            self._in_pem_block = False
            after = clean[end_match.end() :]
            if not after.strip():
                return ""
            return self.scrub_line(after)

        begin_match = _PEM_BEGIN_RE.search(clean)
        if begin_match:
            end_match = _PEM_END_RE.search(clean, begin_match.end())
            if end_match:
                # Complete PEM block on a single line
                replaced = _PEM_BLOCK_RE.sub("[PRIVATE KEY REDACTED]", clean)
                return _scrub_inline(replaced)
            self._in_pem_block = True
            before = clean[: begin_match.start()]
            nl = "\n" if clean.endswith("\n") else ""
            return f"{_scrub_inline(before)}[PRIVATE KEY REDACTED]{nl}"

        return _scrub_inline(clean)


def scrub_text(text: str) -> str:
    """Mask private keys, credentials, tokens, and SSNs before UI display."""
    if not text:
        return text
    out = _ANSI_ESCAPE_RE.sub("", text)
    out = _PEM_BLOCK_RE.sub("[PRIVATE KEY REDACTED]", out)
    out = _PEM_UNCLOSED_RE.sub("[PRIVATE KEY REDACTED]", out)
    return _scrub_inline(out)
