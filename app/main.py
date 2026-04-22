"""FastAPI application entry point."""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from . import database as db
from .config import get_settings
from .routers import admin, public, ui
from .scheduler import start_scheduler, stop_scheduler
from .seed import seed_countries_and_baseline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

_first_run_started = False
_first_run_lock = threading.Lock()


def _placeholders_exist() -> bool:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM risk_ratings WHERE is_live=1 AND scoring_method='placeholder'"
        ).fetchone()
    return bool(row and row["n"] > 0)


def _maybe_trigger_first_run() -> None:
    """If the feed still has placeholder rows and an Anthropic key is configured,
    fire the first full AI scoring pass in a background thread so the public
    feed gets populated without waiting for the next weekly cron."""
    global _first_run_started
    settings = get_settings()
    if not settings.anthropic_api_key:
        logger.info("No ANTHROPIC_API_KEY set — skipping first-run auto-trigger")
        return
    if not _placeholders_exist():
        logger.info("No placeholder ratings — first AI run already completed")
        return
    with _first_run_lock:
        if _first_run_started:
            return
        _first_run_started = True

    def _bg() -> None:
        try:
            from .scoring import run_scoring  # lazy import
            logger.info("First AI run starting in background — this may take several minutes")
            run_id = run_scoring(trigger_source="auto_first_run")
            logger.info("First AI run completed: %s", run_id)
        except Exception:
            logger.exception("First AI run failed — will retry on next trigger")

    t = threading.Thread(target=_bg, daemon=True, name="first-ai-run")
    t.start()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Startup — seeding database")
    try:
        run_id = seed_countries_and_baseline()
        logger.info("Seed complete (run_id=%s)", run_id)
    except Exception:
        logger.exception("Seed failed — proceeding anyway")

    start_scheduler()
    _maybe_trigger_first_run()
    yield
    stop_scheduler()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Country Energy Risk Feed",
    description=(
        "Live country energy risk ratings (1=Stable, 5=Critical). "
        "Powered by Prewave SITREP data and weekly AI-driven web research."
    ),
    version="1.1.0",
    lifespan=lifespan,
)

app.include_router(ui.router)
app.include_router(public.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/admin")
