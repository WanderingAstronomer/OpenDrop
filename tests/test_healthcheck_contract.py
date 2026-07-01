"""Contract: every container healthcheck targets the port + path its service actually listens on
(P4 integration/contract). A healthcheck that probes the wrong port/path reports a healthy service
as unhealthy (compose `depends_on: service_healthy` then wedges the whole bring-up) or, worse,
reports a broken service as healthy. This already bit the `web` service once — busybox wget resolved
`localhost` to ::1 while nginx only listens on IPv4, so the check failed against a working server.

These pure-logic tests (no DB) parse the deploy artifacts as text and, for the API, introspect the
REAL FastAPI route table so the Dockerfile healthcheck URL can't drift away from a served route
(rename `/health` or change the `/api` mount prefix and this goes RED).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
NGINX = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")


def _served_paths() -> set[str]:
    # The OpenAPI schema is the canonical "what paths does this app serve" source — robust across
    # FastAPI versions (0.138 wraps include_router in opaque proxies, so walking app.routes misses
    # the mounted sub-routes). Building the schema touches no DB.
    from app.main import app  # import lazily: only when this test runs

    return set(app.openapi().get("paths", {}).keys())


def test_api_healthcheck_hits_a_real_route_on_the_listener_port():
    # The URL inside the Dockerfile HEALTHCHECK, e.g. http://localhost:8000/api/health
    m = re.search(r"HEALTHCHECK.*?(http://[^\s'\"\)]+)", DOCKERFILE, re.S)
    assert m, "no http:// URL found in the backend/Dockerfile HEALTHCHECK"
    url = m.group(1)
    parts = re.match(r"http://([^/:]+):(\d+)(/\S*?)/?$", url)
    assert parts, f"could not parse host:port/path from healthcheck URL {url!r}"
    host, port, path = parts.group(1), parts.group(2), parts.group(3)

    assert host == "localhost", f"api healthcheck host should be localhost, got {host!r}"
    assert port == "8000", f"api healthcheck port {port!r} != the listener (uvicorn --port 8000)"

    # The probed path must be a route the app actually serves (catches /health rename or /api drift).
    served = _served_paths()
    assert path in served, (
        f"api healthcheck probes {path!r}, which is NOT a served route. Served paths include "
        f"{sorted(p for p in served if 'health' in p or p.startswith('/api'))[:8]!r}"
    )

    # The 8000 the healthcheck probes must equal every other place the API port is declared.
    assert re.search(r"^EXPOSE\s+8000\b", DOCKERFILE, re.M), "Dockerfile must EXPOSE 8000"
    assert re.search(r'--port",\s*"8000"', DOCKERFILE), "uvicorn CMD must bind --port 8000"
    api_container_port = re.search(r'"\$\{API_PORT:-\d+\}:(\d+)"', COMPOSE)
    assert api_container_port and api_container_port.group(1) == "8000", (
        "compose api service must map to container port 8000"
    )


def test_web_healthcheck_uses_ipv4_and_the_nginx_listen_port():
    # The web service healthcheck wgets the nginx root; pull host + (optional) port out of it.
    m = re.search(r"wget[^\n]*http://([0-9.]+)(?::(\d+))?/", COMPOSE)
    assert m, "no wget http://<ipv4>/ healthcheck found for the web service in docker-compose.yml"
    host, port = m.group(1), m.group(2) or "80"

    # IPv4 literal, not 'localhost': the documented fix for the ::1-vs-IPv4 healthcheck flap.
    assert host == "127.0.0.1", f"web healthcheck must use the IPv4 literal 127.0.0.1, got {host!r}"

    listen_ports = set(re.findall(r"^\s*listen\s+(\d+)", NGINX, re.M))
    assert port in listen_ports, (
        f"web healthcheck probes port {port}, but nginx listens on {sorted(listen_ports)}"
    )

    # And that's the port compose publishes to (container side of the web mapping).
    web_container_port = re.search(r'"\$\{WEB_PORT:-\d+\}:(\d+)"', COMPOSE)
    assert web_container_port and web_container_port.group(1) == port, (
        f"compose web service container port != nginx listen port {port}"
    )


def test_db_healthcheck_uses_the_compose_postgres_credentials():
    # The db healthcheck must pg_isready against the same user/db compose provisions, or it can pass
    # against the wrong database (or fail against the right one).
    block = re.search(r"db:.*?(?=\n  \w)", COMPOSE, re.S)
    assert block, "could not locate the db service block in docker-compose.yml"
    db_block = block.group(0)
    assert "pg_isready" in db_block, "db healthcheck should use pg_isready"
    assert "POSTGRES_USER" in db_block and "POSTGRES_DB" in db_block, (
        "db healthcheck must reference the same POSTGRES_USER / POSTGRES_DB compose sets"
    )
