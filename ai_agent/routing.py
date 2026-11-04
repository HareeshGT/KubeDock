"""Deterministic routing for Ops Mind requests supported by legacy workflows."""
from __future__ import annotations

import re

_MUTATING_REQUEST_RE = re.compile(
    r"\b(delete|remove|restart|scale|undo|revert|restore|patch|apply|edit|update|create|deploy|rollback|roll back|kill|terminate|drain|cordon|uncordon|resize)\b",
    re.IGNORECASE,
)
_LEGACY_READ_REQUEST_RE = re.compile(
    r"\b(investigat\w*|describe)\b",
    re.IGNORECASE,
)
_GET_KEY_REQUEST_RE = re.compile(
    r"\bget\b(?=.*\bkeys?\b)",
    re.IGNORECASE,
)


def should_use_legacy_interpreter(request: str) -> bool:
    """Keep mutations and read workflows unsupported by the agent on the mature interpreter."""
    text = str(request or "")
    return bool(
        _MUTATING_REQUEST_RE.search(text)
        or _LEGACY_READ_REQUEST_RE.search(text)
        or _GET_KEY_REQUEST_RE.search(text)
    )
