import hashlib

import httpx

from .config import settings

# Cloudflare Turnstile documented TEST keys (dev mock).
TEST_PASS_SECRET = "1x0000000000000000000000000000000AA"  # always passes
TEST_FAIL_SECRET = "2x0000000000000000000000000000000AA"  # always fails
SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def ip_hash(ip: str | None) -> str:
    return hashlib.sha256((settings.ip_hash_salt + (ip or "")).encode()).hexdigest()


def token_hash(token: str | None) -> str | None:
    if not token:
        return None
    return hashlib.sha256(token.encode()).hexdigest()


async def verify_turnstile(token: str | None, remote_ip: str | None = None) -> bool:
    """Returns True if the Turnstile token is valid.

    A missing/empty token ALWAYS fails — including in dev-mock mode — which is what
    satisfies Phase 4 step 3 ("blocks submission without a valid token in dev mock mode").
    With the CF test secret we short-circuit (no network) so dev/tests need no internet.
    """
    if not token:
        return False
    secret = settings.turnstile_secret
    if secret == TEST_PASS_SECRET:
        return True
    if secret == TEST_FAIL_SECRET:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                SITEVERIFY_URL,
                data={"secret": secret, "response": token, "remoteip": remote_ip or ""},
            )
            return bool(resp.json().get("success"))
    except Exception:  # noqa: BLE001 — network/parse failure => reject conservatively
        return False
