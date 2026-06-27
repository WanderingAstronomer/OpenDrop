"""Lightweight content screen for crowd submissions. Objective checks only
(links, emails, control chars, length) plus an optional env-configured wordlist —
no hardcoded slur list. Returns a human-readable rejection reason, or None if OK."""
import re

from .config import settings

_URL = re.compile(r"https?://|www\.", re.IGNORECASE)
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _denylist() -> set[str]:
    return {w.strip().lower() for w in (settings.content_denylist or "").split(",") if w.strip()}


def screen_submission(name: str, *extra: str | None) -> str | None:
    """Reject obvious abuse in a crowd submission. `name` is required; `extra` are
    optional address fields. Returns a reason string to reject, or None to allow."""
    name = (name or "").strip()
    if len(name) < 2:
        return "Name is too short."
    if len(name) > 200:
        return "Name is too long."

    deny = _denylist()
    for field in (name, *extra):
        if not field:
            continue
        if _CTRL.search(field):
            return "Text contains invalid control characters."
        if _URL.search(field):
            return "Links are not allowed in submissions."
        if _EMAIL.search(field):
            return "Email addresses are not allowed in submissions."
        low = field.lower()
        if any(word in low for word in deny):
            return "Submission contains disallowed content."
    return None
