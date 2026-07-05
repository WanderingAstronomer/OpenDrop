"""Lightweight content screen for crowd submissions. Objective checks only
(links, emails, control chars, length) plus a small baked-in denylist of unambiguous
abuse/spam tokens, MERGED with an optional env-configured wordlist (CONTENT_DENYLIST).
Returns a human-readable rejection reason, or None if OK.

Design notes:
  * The default list is intentionally conservative — it targets obvious profanity, slurs, and
    spam/scam markers that have no legitimate place in a charity-location name or note. It is NOT
    a moderation policy on its own; the consensus + operator-takedown layers do the heavy lifting.
  * Operators EXTEND the list via CONTENT_DENYLIST (comma-separated); they never replace the
    baked-in floor, so a misconfigured env can't silently disable spam/abuse screening.
  * Matching is substring-on-lowercased-text. Keep entries specific enough to avoid the
    Scunthorpe problem on ordinary place names; when in doubt, leave a token out and rely on
    community flagging + operator takedown."""
import re

from .config import settings

_URL = re.compile(r"https?://|www\.", re.IGNORECASE)
# Bounded quantifiers so the match cost is linear in the input — the old `[^@\s]+@[^@\s]+\.[^@\s]+`
# backtracked quadratically on a long no-match string, a ReDoS lever on any screened field. The
# local-part/domain length caps here comfortably cover every real email while removing the runaway.
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,24}")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Hard ceiling applied inside _scan before any regex runs. Every caller already bounds its fields
# (name <=200, notes/reasons <=500, address parts <=200 via the Pydantic models), so this is a
# defense-in-depth backstop that keeps regex cost bounded even if some future caller forgets to cap.
_MAX_SCAN_LEN = 600

# Baked-in floor. Unambiguous slurs / profanity / spam-scam markers only. Lowercase, substring match.
_DEFAULT_DENYLIST: frozenset[str] = frozenset({
    # spam / scam markers (no legitimate donation-location use)
    "viagra", "cialis", "casino", "porn", "xxx", "sexcam", "camgirl", "escort service",
    "bitcoin generator", "free money", "click here", "telegram @", "whatsapp +",
    "loan offer", "crypto giveaway", "nft drop", "onlyfans",
    # unambiguous slurs / hate terms
    "nigger", "faggot", "retard", "kike", "spic", "chink", "tranny",
    # gross profanity unlikely in a real org name
    "fuckface", "motherfucker", "cunt",
})


def _denylist() -> set[str]:
    extra = {w.strip().lower() for w in (settings.content_denylist or "").split(",") if w.strip()}
    return set(_DEFAULT_DENYLIST) | extra


def _scan(field: str, deny: set[str]) -> str | None:
    """Objective abuse checks on one text field. None => clean."""
    if len(field) > _MAX_SCAN_LEN:
        return "Text is too long."
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
        reason = _scan(field, deny)
        if reason is not None:
            return reason
    return None


def screen_text(text: str | None) -> str | None:
    """Screen an OPTIONAL free-text field (e.g. a correction note). Empty/None is allowed.
    Applies the same objective checks as submissions, minus the name length rules."""
    text = (text or "").strip()
    if not text:
        return None
    if len(text) > 500:
        return "Note is too long."
    return _scan(text, _denylist())
