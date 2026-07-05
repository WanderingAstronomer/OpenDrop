import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db
from .config import settings
from .routers import corrections, images, locations, meta, moderation, stats, votes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("opendrop")

_PROD = settings.app_env.lower() == "prod"


async def _assert_schema_at_head() -> None:
    """Refuse (prod) / warn (dev) if the DB isn't migrated to the version this image expects.
    Closes the 'new code against an old schema' drift class — the API won't serve traffic against a
    DB that predates the migrations its code relies on."""
    want = settings.expected_schema_version
    present = False
    try:
        async with db.pool.connection() as conn:
            cur = await conn.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (want,))
            present = await cur.fetchone() is not None
    except Exception as e:  # noqa: BLE001
        log.warning("could not verify schema version (%s): %s", want, e)
    if present:
        log.info("schema at expected head: %s", want)
        return
    msg = f"database is not migrated to expected head {want!r} — run scripts/migrate.sh first"
    if _PROD:
        raise RuntimeError(msg)
    log.warning("%s (continuing because APP_ENV != prod)", msg)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for warning in settings.insecure_default_warnings():  # loud in ANY env, even before the prod guard
        log.warning("INSECURE DEFAULT: %s", warning)
    settings.assert_production_secrets()  # refuse to boot in prod with default secrets
    await db.open_pool()
    await _assert_schema_at_head()
    yield
    await db.close_pool()


app = FastAPI(
    title="OpenDrop API", version="0.1.0", lifespan=lifespan,
    # Interactive docs + raw schema are disabled in production (smaller attack surface).
    docs_url=None if _PROD else "/docs",
    redoc_url=None if _PROD else "/redoc",
    openapi_url=None if _PROD else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    # Explicit method/header allowlists rather than "*": the API only uses these verbs, and the
    # only non-simple request headers are JSON content-type and the operator token.
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Operator-Token"],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Per-request id + structured access log, and a catch-all that turns any UNHANDLED exception
    into the uniform error envelope so a stack trace never reaches a client. HTTPExceptions are
    handled inside (by http_exception_handler) and arrive here as ordinary responses."""
    rid = uuid.uuid4().hex[:12]
    request.state.request_id = rid
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001
        dur = (time.perf_counter() - start) * 1000
        log.exception("rid=%s %s %s -> 500 (%.0fms)", rid, request.method, request.url.path, dur)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "internal server error",
                               "request_id": rid}},
            headers={"X-Request-ID": rid},
        )
    dur = (time.perf_counter() - start) * 1000
    log.info("rid=%s %s %s -> %s (%.0fms)", rid, request.method, request.url.path,
             response.status_code, dur)
    response.headers["X-Request-ID"] = rid
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Uniform error envelope: {"error": {"code","message",...}}."""
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        err = dict(detail)
    else:
        err = {"code": "error", "message": str(detail)}
    rid = getattr(request.state, "request_id", None)
    if rid:
        err.setdefault("request_id", rid)
    headers = {"X-Request-ID": rid} if rid else None
    return JSONResponse(status_code=exc.status_code, content={"error": err}, headers=headers)


app.include_router(meta.router, prefix="/api")
app.include_router(locations.router, prefix="/api")
app.include_router(votes.router, prefix="/api")
app.include_router(images.router, prefix="/api")
app.include_router(corrections.router, prefix="/api")
app.include_router(moderation.router, prefix="/api")
app.include_router(stats.router, prefix="/api")
