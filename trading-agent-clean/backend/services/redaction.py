from __future__ import annotations

import re
from typing import Any


REDACTION_LIMIT = 500
SECRET_QUERY_RE = re.compile(
    r"([?&](?:api[_-]?key|apikey|access[_-]?token|token|secret|password|pass|auth)=)[^&\s]+",
    re.IGNORECASE,
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(api[_-]?key|apikey|access[_-]?token|token|secret|password|pass|auth)=([^\s,;]+)",
    re.IGNORECASE,
)
URI_CREDENTIAL_RE = re.compile(r"([a-z][a-z0-9+.-]*://)([^:/@\s]+):([^@\s]+)@", re.IGNORECASE)
AUTH_HEADER_RE = re.compile(r"(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+", re.IGNORECASE)
WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s\"'<>]+")
POSIX_PATH_RE = re.compile(r"(?<!\w)/(?:Users|home|var|tmp|etc|Program Files|mnt)/[^\s\"'<>]+")


def redact_text(value: Any, *, limit: int = REDACTION_LIMIT) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = URI_CREDENTIAL_RE.sub(r"\1<redacted>:<redacted>@", text)
    text = SECRET_QUERY_RE.sub(r"\1<redacted>", text)
    text = SECRET_ASSIGNMENT_RE.sub(r"\1=<redacted>", text)
    text = AUTH_HEADER_RE.sub(r"\1<redacted>", text)
    text = WINDOWS_PATH_RE.sub("<redacted-path>", text)
    text = POSIX_PATH_RE.sub("<redacted-path>", text)
    if len(text) > limit:
        text = f"{text[:limit]}..."
    return text


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
