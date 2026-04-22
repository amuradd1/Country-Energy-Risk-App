"""Shared scheduling helpers for the weekly scoring refresh."""
from __future__ import annotations

from datetime import datetime, timezone

from apscheduler.triggers.cron import CronTrigger

from .config import Settings, get_settings


def get_scoring_trigger(settings: Settings | None = None) -> CronTrigger:
    current_settings = settings or get_settings()
    return CronTrigger(
        day_of_week=current_settings.scoring_cron_day_of_week,
        hour=current_settings.scoring_cron_hour,
        minute=0,
        second=0,
        timezone="UTC",
    )


def get_next_refresh_time(
    now: datetime | None = None,
    settings: Settings | None = None,
) -> datetime:
    current_time = now or datetime.now(timezone.utc)
    next_fire_time = get_scoring_trigger(settings).get_next_fire_time(None, current_time)
    if next_fire_time is None:
        raise RuntimeError("Weekly scoring trigger did not produce a next refresh time.")
    return next_fire_time
