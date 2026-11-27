"""LoanTrack backend API.

Run with:  uvicorn app.main:app --host 0.0.0.0 --port 8000

`app` is the FastAPI application wrapped (outermost) by HeaderMiddleware,
so every HTTP response - including errors - carries X-Served-By and X-App-Version.
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import loans
from .config import Settings, load_settings
from .db import Database, DatabaseUnavailable
from .middleware import HeaderMiddleware
from .schemas import Loan, LoanCreate

logger = logging.getLogger("loantrack.api")


def configure_logging() -> None:
    """INFO-level application logs to stdout with timestamps.

    Uvicorn configures only its own loggers, so without this our retry
    messages would not appear in `docker logs`.
    """
    app_logger = logging.getLogger("loantrack")
    if not app_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S%z")
        )
        app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)
    # psycopg_pool logs connection failures at WARNING with the server's error text
    # (host, port, user, database). We log our own class-name-only messages instead.
    logging.getLogger("psycopg").setLevel(logging.ERROR)


class AsciiJSONResponse(JSONResponse):
    """Same JSON as JSONResponse, with non-ASCII written as \\uXXXX escapes.

    422 bodies echo the client's input; a lone surrogate in it cannot be
    UTF-8 encoded, which made the default 422 response fail with a 500.
    """

    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=True, allow_nan=False, indent=None, separators=(",", ":")).encode(
            "ascii"
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db: Database = app.state.db
    await db.start()  # returns immediately, even when PostgreSQL is down
    try:
        yield
    finally:
        await db.stop()


def _status_body(settings: Settings, **fields: Any) -> dict[str, Any]:
    return {**fields, "served_by": settings.served_by, "version": settings.app_version}


def create_app(
    settings: Settings | None = None,
    pool_factory: Callable[[Settings], Any] | None = None,
) -> HeaderMiddleware:
    configure_logging()
    settings = settings or load_settings()

    api = FastAPI(title="LoanTrack API", version=settings.app_version, lifespan=lifespan)
    api.state.settings = settings
    api.state.db = Database(settings, pool_factory=pool_factory)

    @api.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Same body as FastAPI's default handler; only the encoding is made safe.
        return AsciiJSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

    @api.exception_handler(DatabaseUnavailable)
    async def database_unavailable(request: Request, exc: DatabaseUnavailable) -> JSONResponse:
        logger.warning("database unavailable: path=%s error=%s", request.url.path, exc)
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})

    @api.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Generic body, no traceback to the client. Uvicorn still logs the traceback.
        logger.error("unhandled error: path=%s error=%s", request.url.path, type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    @api.get("/healthz")
    async def healthz() -> dict[str, Any]:
        # Liveness: the process is alive. Never touches the database.
        return _status_body(settings, status="ok")

    @api.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        # Readiness: SELECT 1 through the pool; 503 when PostgreSQL is unreachable.
        ok, _ = await request.app.state.db.check()
        if ok:
            return JSONResponse(status_code=200, content=_status_body(settings, status="ready", database="ok"))
        return JSONResponse(
            status_code=503, content=_status_body(settings, status="not_ready", database="unavailable")
        )

    @api.get("/loans", response_model=list[Loan])
    async def get_loans(request: Request) -> list[dict[str, Any]]:
        return await loans.list_loans(request.app.state.db)

    @api.post("/loans", response_model=Loan, status_code=201)
    async def post_loan(request: Request, body: LoanCreate) -> dict[str, Any]:
        return await loans.create_loan(request.app.state.db, body)

    return HeaderMiddleware(api, served_by=settings.served_by, app_version=settings.app_version)


app = create_app()
