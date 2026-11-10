"""Scrub text from outside the runtime (planner output, upstream error text) before it is stored.

The event store is append-only: whatever reaches it stays. Text that did not come from our own
code is passed through `scrub` first, which redacts what looks like a credential, a token or an
e-mail address and replaces characters that could rewrite what a reader of the log or of
`cellrt replay` sees (control characters, line and paragraph separators, direction controls,
invisible characters) or that cannot be stored (lone surrogates). It is a best-effort filter
for an audit trail, not a guarantee: the safer rule stays "record error codes, not text".
"""

from __future__ import annotations

import re

REDACTED = "<redacted>"
# Only this much of the input is scrubbed and returned; callers cut it further.
MAX_SCRUB_CHARS = 4_000

# Characters that can change how stored text reads: C0 controls, DEL, C1 controls (\x9b alone
# is a terminal escape), the Arabic letter mark, zero-width characters and direction marks,
# line and paragraph separators, direction embeddings, overrides and isolates, the word joiner
# and invisible operators, the byte order mark, and tag characters. One class, written with
# escapes that Python's `re` and pydantic's (Rust) regex engine both read, shared by `scrub` and
# by every pattern that refuses such text outright.
UNSAFE_CHARS = (
    r"\x00-\x1f\x7f-\x9f\u061c\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff"
    r"\U000e0000-\U000e007f"
)
_UNSAFE = re.compile(f"[{UNSAFE_CHARS}]")

# A key name that says its value is secret, then ":" or "=", then the value. The name may be
# compound (client_secret, AWS_SECRET_ACCESS_KEY) or quoted (JSON); a quoted value goes whole.
# A bare space is not a separator, so prose ("token budget exceeded") is kept.
_KEYED = re.compile(
    r"(?i)(?<![\w-])"
    r"([\w-]*(?:secret|token|password|passwd|api[_ -]?key|access[_-]?key|private[_-]?key"
    r"|credential)[\w-]*|pass|pwd|auth|authorization)"
    r"([\"']?\s*[:=]\s*)"
    r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s'\",;}]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)[^\s'\",;]+")
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[^-]*"),  # PEM private keys
    re.compile(r"\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{8,}"),  # API keys with a known prefix
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key ids
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),  # Google API keys
    re.compile(r"\bya29\.[0-9A-Za-z_-]{10,}"),  # Google OAuth access tokens
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),  # JWTs
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),  # e-mail addresses
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),  # long hex digests and keys
]
# A long opaque run is redacted only if it mixes letters and digits, so a long identifier
# (test_a_skill_that_cancels_itself) is kept.
_OPAQUE = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")


def scrub(text: str, *, max_chars: int = MAX_SCRUB_CHARS) -> str:
    """The first `max_chars` of `text` with secrets redacted and unsafe characters replaced by
    "?". The default suits text from outside; a caller that stores more (a planner prompt) says
    how much, and still gets a bounded scan."""
    text = text[:max_chars]
    # Bearer first: "Authorization: Bearer x" would otherwise lose only the word "Bearer".
    text = _BEARER.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _KEYED.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _URL_CREDENTIALS.sub(lambda m: f"{m.group(1)}{REDACTED}@", text)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _OPAQUE.sub(_opaque, text)
    # A lone surrogate (a JSON "\ud800" escape, decoded) cannot be written as UTF-8, so the
    # store would refuse the event; it becomes "?" too. Not in UNSAFE_CHARS: pydantic's regex
    # engine does not accept the surrogate range.
    return _UNSAFE.sub("?", text).encode("utf-8", "replace").decode("utf-8")


def _opaque(match: re.Match[str]) -> str:
    run = match.group(0)
    mixed = any(c.isdigit() for c in run) and any(c.isalpha() for c in run)
    return REDACTED if mixed else run
