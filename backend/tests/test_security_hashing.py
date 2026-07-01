"""Security primitives (backend/app/security.py): IP/token hashing + Turnstile verification.

These pin the privacy/anti-abuse guarantees the rest of the app leans on:

  * ip_hash    — salted SHA-256 over the client IP. Must be DETERMINISTIC (same IP -> same
                 64-hex digest so per-IP rate limits actually accumulate), SALTED (the configured
                 ip_hash_salt feeds the digest, so two deployments with different salts produce
                 different hashes for the same IP), and tolerant of a missing IP (None -> "").
  * token_hash — SHA-256 over a CF token, or None for a falsy token (so "no token" can't be
                 confused with "hash of empty string").
  * verify_turnstile — async. A missing/empty token ALWAYS fails (even in dev-mock), the CF
                 TEST_PASS_SECRET short-circuits to True and TEST_FAIL_SECRET to False, both
                 with NO network. We only ever exercise the short-circuit paths here, so the
                 httpx branch is never reached -> no network, no DB.

No DB, no network. `settings` (app.config) attributes are monkeypatched per-test.
Run: PYTHONPATH=.:backend pytest backend/tests/test_security_hashing.py
"""
import asyncio
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))               # make `pipeline` importable
sys.path.insert(0, str(ROOT / "backend"))   # make `app` importable

from hypothesis import given, settings as hyp_settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from app import security  # noqa: E402
from app.config import settings  # noqa: E402

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


def _is_hex64(s) -> bool:
    return isinstance(s, str) and bool(_HEX64.match(s))


# --------------------------------------------------------------------------- ip_hash

def test_ip_hash_is_64_lowercase_hex(monkeypatch):
    monkeypatch.setattr(settings, "ip_hash_salt", "pepper", raising=False)
    digest = security.ip_hash("203.0.113.7")
    assert _is_hex64(digest)


def test_ip_hash_is_deterministic(monkeypatch):
    """Same salt + same IP -> identical digest (rate-limit buckets depend on this)."""
    monkeypatch.setattr(settings, "ip_hash_salt", "pepper", raising=False)
    assert security.ip_hash("198.51.100.9") == security.ip_hash("198.51.100.9")


def test_ip_hash_matches_explicit_sha256(monkeypatch):
    """The digest is exactly sha256(salt + ip) — derived directly from the source."""
    monkeypatch.setattr(settings, "ip_hash_salt", "s4lt", raising=False)
    expected = hashlib.sha256(("s4lt" + "10.0.0.1").encode()).hexdigest()
    assert security.ip_hash("10.0.0.1") == expected


def test_ip_hash_distinguishes_different_ips(monkeypatch):
    monkeypatch.setattr(settings, "ip_hash_salt", "pepper", raising=False)
    assert security.ip_hash("192.0.2.1") != security.ip_hash("192.0.2.2")


def test_ip_hash_is_salted(monkeypatch):
    """Changing only the salt changes the digest for the same IP."""
    monkeypatch.setattr(settings, "ip_hash_salt", "salt-A", raising=False)
    a = security.ip_hash("192.0.2.50")
    monkeypatch.setattr(settings, "ip_hash_salt", "salt-B", raising=False)
    b = security.ip_hash("192.0.2.50")
    assert a != b


def test_ip_hash_none_is_treated_as_empty(monkeypatch):
    """A missing IP hashes as the empty string (salt + "") — no crash, valid digest."""
    monkeypatch.setattr(settings, "ip_hash_salt", "pepper", raising=False)
    none_digest = security.ip_hash(None)
    assert _is_hex64(none_digest)
    assert none_digest == security.ip_hash("")
    assert none_digest == hashlib.sha256(("pepper" + "").encode()).hexdigest()


# --------------------------------------------------------------------------- token_hash

def test_token_hash_none_returns_none():
    assert security.token_hash(None) is None


def test_token_hash_empty_returns_none():
    """Empty string is falsy -> None (distinct from a real hash)."""
    assert security.token_hash("") is None


def test_token_hash_nonempty_is_64_hex():
    assert _is_hex64(security.token_hash("cf-token-abc"))


def test_token_hash_is_deterministic_and_unsalted():
    """token_hash is a plain sha256(token) — no salt — so it's reproducible across processes."""
    assert security.token_hash("cf-token-abc") == security.token_hash("cf-token-abc")
    assert security.token_hash("cf-token-abc") == hashlib.sha256(b"cf-token-abc").hexdigest()


def test_token_hash_distinguishes_tokens():
    assert security.token_hash("token-1") != security.token_hash("token-2")


# --------------------------------------------------------------- verify_turnstile (async)
# Driven via asyncio.run so the suite needs no pytest-asyncio plugin. Only the CF TEST-secret
# short-circuits are exercised, so the httpx branch is never reached -> guaranteed no network.

def _verify(token, remote_ip=None):
    return asyncio.run(security.verify_turnstile(token, remote_ip))


def test_verify_pass_secret_accepts_nonempty_token(monkeypatch):
    monkeypatch.setattr(settings, "turnstile_secret", security.TEST_PASS_SECRET, raising=False)
    assert _verify("any-non-empty-token") is True


def test_verify_pass_secret_still_rejects_empty_token(monkeypatch):
    """Empty/None token ALWAYS fails, even with the always-pass dev-mock secret."""
    monkeypatch.setattr(settings, "turnstile_secret", security.TEST_PASS_SECRET, raising=False)
    assert _verify("") is False
    assert _verify(None) is False


def test_verify_fail_secret_rejects_nonempty_token(monkeypatch):
    monkeypatch.setattr(settings, "turnstile_secret", security.TEST_FAIL_SECRET, raising=False)
    assert _verify("any-non-empty-token") is False


def test_verify_fail_secret_rejects_empty_token(monkeypatch):
    monkeypatch.setattr(settings, "turnstile_secret", security.TEST_FAIL_SECRET, raising=False)
    assert _verify("") is False


def test_verify_empty_token_short_circuits_before_secret(monkeypatch):
    """The missing-token guard runs before settings.turnstile_secret is even read, so an
    empty token is rejected regardless of which secret is configured."""
    monkeypatch.setattr(settings, "turnstile_secret", security.TEST_PASS_SECRET, raising=False)
    assert _verify(None) is False


# --------------------------------------------------------------------------- property-based

@hyp_settings(max_examples=200)
@given(salt=st.text(), ip=st.text())
def test_ip_hash_property_deterministic_hex64(salt, ip):
    """For arbitrary salt + IP text, ip_hash is a deterministic 64-char lowercase-hex function
    that equals sha256((salt + ip).encode()).

    Salt is swapped per-example by direct save/restore (not the function-scoped monkeypatch
    fixture, which Hypothesis sets up only once across all examples)."""
    prev = settings.ip_hash_salt
    settings.ip_hash_salt = salt
    try:
        first = security.ip_hash(ip)
        second = security.ip_hash(ip)
        assert first == second                       # deterministic
        assert _is_hex64(first)                       # 64 lowercase-hex chars
        assert first == hashlib.sha256((salt + ip).encode()).hexdigest()  # exact formula
    finally:
        settings.ip_hash_salt = prev


@hyp_settings(max_examples=200)
@given(token=st.text(min_size=1))
def test_token_hash_property_nonempty_hex64(token):
    """Any non-empty token hashes to a 64-hex sha256(token) digest, deterministically."""
    digest = security.token_hash(token)
    assert _is_hex64(digest)
    assert digest == security.token_hash(token)
    assert digest == hashlib.sha256(token.encode()).hexdigest()


if __name__ == "__main__":
    print("Run with: PYTHONPATH=.:backend pytest backend/tests/test_security_hashing.py")
