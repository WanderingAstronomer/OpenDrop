import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db
from .config import settings
from .routers import locations, meta, votes

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await db.open_pool()
    yield
    await db.close_pool()


app = FastAPI(title="OpenDrop API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    """Uniform error envelope: {"error": {"code","message",...}}."""
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        err = detail
    else:
        err = {"code": "error", "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": err})


app.include_router(meta.router, prefix="/api")
app.include_router(locations.router, prefix="/api")
app.include_router(votes.router, prefix="/api")
