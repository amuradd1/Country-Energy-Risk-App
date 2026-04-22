"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from .routers import admin, public
from .scheduler import start_scheduler, stop_scheduler
from .seed import seed_countries_and_baseline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting up — seeding database")
    try:
        run_id = seed_countries_and_baseline()
        logger.info("Seed complete (run_id=%s)", run_id)
    except Exception:
        logger.exception("Seed failed — proceeding anyway")

    start_scheduler()
    yield
    stop_scheduler()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Country Energy Risk Feed",
    description=(
        "Live country energy risk ratings (1=Stable, 5=Critical). "
        "Powered by Prewave SITREP data and daily AI-driven web research."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(public.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/admin")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")
