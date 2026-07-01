import hmac

from fastapi import HTTPException, Request

from .config import settings


def client_ip(request: Request) -> str:
    """Trusted client IP. nginx overwrites X-Real-IP with the real peer ($remote_addr),
    so it can't be spoofed by a client header. We deliberately ignore inbound
    X-Forwarded-For (ARCHITECTURE §6). Falls back to the socket peer for direct/dev access."""
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    return request.client.host if request.client else ""


def require_operator(request: Request) -> None:
    """Gate operator/moderation endpoints behind OPERATOR_TOKEN (sent as `X-Operator-Token`).

    Returns 404 — not 401/403 — when the token is unset or wrong, so the operator surface is
    invisible to probes and indistinguishable from a non-existent route. The token is compared
    in constant time. When OPERATOR_TOKEN is empty (the default), the entire operator surface is
    disabled and every call 404s."""
    configured = settings.operator_token or ""
    presented = (request.headers.get("x-operator-token") or "").strip()
    if not configured or not presented or not hmac.compare_digest(presented, configured):
        raise HTTPException(404, {"code": "not_found", "message": "Not found"})
