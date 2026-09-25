"""Artifact typing and text sanitization for Playwright-collected diagnostics.

No Playwright calls happen here — this only defines what kind of artifact
exists (matching the ``artifacts`` table's ``artifact_type`` column from
Stage 1) and a best-effort text redaction pass to run before any DOM
snippet, error message, or log line derived from a real CRM page is
persisted or logged.

``sanitize_text_for_artifact`` is intentionally conservative and documented
as such: it is a regex-based pass over a few common PII shapes (email,
phone-like digit runs), not a general PII detector. Treat it as a safety net
that catches obvious cases, not a guarantee — the real control is capturing
less in the first place (redact at the point of DOM extraction, not just
before storage) once the Playwright adapter exists.
"""

from __future__ import annotations

import re
from enum import Enum

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d\-\s()]{7,}\d)(?!\d)")


class ArtifactType(str, Enum):
    SCREENSHOT = "SCREENSHOT"
    TRACE = "TRACE"
    DOM_SNIPPET = "DOM_SNIPPET"


def sanitize_text_for_artifact(text: str) -> str:
    redacted = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    redacted = _PHONE_RE.sub("[REDACTED_PHONE]", redacted)
    return redacted
