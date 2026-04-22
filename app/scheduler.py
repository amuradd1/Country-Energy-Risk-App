"""APScheduler weekly scoring refresh."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .config import get_settings
from .schedule import get_scoring_trigger

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _run_scheduled_refresh() -> None:
    from .scoring import run_scoring  # local import avoids circular at module load

    logger.info("Scheduled weekly scoring run starting")
    try:
        run_id = run_scoring(trigger_source="scheduler")
        logger.info("Scheduled scoring run completed: %s", run_id)
    except Exception:
        logger.exception("Scheduled scoring run failed")


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return

    settings = get_settings()
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _run_scheduled_refresh,
        trigger=get_scoring_trigger(settings),
        id="weekly_scoring",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(
        "Scheduler started — weekly scoring on %s at %02d:00 UTC",
        settings.scoring_cron_day_of_week,
        settings.scoring_cron_hour,
    )


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
