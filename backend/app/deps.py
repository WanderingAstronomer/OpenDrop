import hmac
import ipaddress

from fastapi import HTTPException, Request

from .config import settings


def _peer_is_trusted_proxy(request: Request) -> bool:
    """Only a request whose immediate socket peer is loopback/private (i.e. the local nginx/Caddy
    reverse proxy on the container network) is allowed to set X-Real-IP on our behalf. A direct
    public client — e.g. if the API port were ever exposed — has a public peer, so its forged
    X-Real-IP is ignored and its real socket address is used instead."""
    peer = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def client_ip(request: Request) -> str:
    """Trusted client IP for the per-IP rate limits / anonymized ip_hash.

    nginx overwrites X-Real-IP with the real peer ($remote_addr), so behind the proxy it can't be
    spoofed; we deliberately ignore inbound X-Forwarded-For (ARCHITECTURE §6). In PRODUCTION we only
    honor X-Real-IP when the socket peer is the trusted local proxy (loopback/private) — a client
    reaching the API directly cannot forge its identity. In dev/test (single host, and where the
    TestClient sets X-Real-IP to drive per-IP tests) the header is trusted as before."""
    xri = request.headers.get("x-real-ip")
    if xri and (settings.app_env.lower() != "prod" or _peer_is_trusted_proxy(request)):
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
