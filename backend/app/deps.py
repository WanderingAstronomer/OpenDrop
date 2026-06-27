from fastapi import Request


def client_ip(request: Request) -> str:
    """Trusted client IP. nginx overwrites X-Real-IP with the real peer ($remote_addr),
    so it can't be spoofed by a client header. We deliberately ignore inbound
    X-Forwarded-For (ARCHITECTURE §6). Falls back to the socket peer for direct/dev access."""
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    return request.client.host if request.client else ""
