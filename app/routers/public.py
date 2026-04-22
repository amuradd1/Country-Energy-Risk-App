"""Public API endpoints — JSON feed, CSV feed, single country, health."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .. import database as db
from ..config import get_settings
from ..models import HealthResponse, Metadata, RatingRecord, RatingsResponse

router = APIRouter()

METHODOLOGY = "Composite energy security rating (1=Stable/best, 5=Critical/worst)"
SOURCES = [
    "Prewave SITREP Day 33",
    "IEA",
    "EIA",
    "National government disclosures",
    "News agencies",
]
CSV_FIELDS = [
    "country_code",
    "country_name",
    "region",
    "energy_risk_rating",
    "status_label",
    "oil_reserve_days",
    "lng_status",
    "key_risk",
    "primary_source",
    "primary_source_url",
    "secondary_sources",
    "source_urls",
    "confidence",
    "hormuz_dependency",
    "gdp_loss_pct",
    "scoring_method",
    "is_pinned",
    "scored_at",
]


def _split_urls(raw: Any) -> list[str] | None:
    if not raw:
        return None
    parts = [u.strip() for u in str(raw).split(",") if u.strip()]
    return parts or None


def _to_record(row: dict[str, Any]) -> RatingRecord:
    return RatingRecord(
        country_code=row["country_code"],
        country_name=row["country_name"],
        region=row.get("region"),
        energy_risk_rating=row["rating"],
        status_label=row["status_label"],
        oil_reserve_days=row.get("oil_reserve_days"),
        lng_status=row.get("lng_status"),
        key_risk=row.get("key_risk"),
        primary_source=row.get("primary_source"),
        primary_source_url=row.get("primary_source_url"),
        secondary_sources=row.get("secondary_sources"),
        source_urls=_split_urls(row.get("source_urls")),
        confidence=row.get("confidence"),
        hormuz_dependency=row.get("hormuz_dependency"),
        gdp_loss_pct=row.get("gdp_loss_pct"),
        scoring_method=row["scoring_method"],
        is_pinned=bool(row.get("is_pinned", 0)),
        scored_at=row["scored_at"],
    )


def _build_metadata(count: int) -> Metadata:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    last_run = db.get_last_run()
    next_refresh = (
        now.replace(hour=settings.scoring_cron_hour, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Metadata(
        generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        methodology=METHODOLOGY,
        sources=SOURCES,
        country_count=count,
        next_refresh=next_refresh,
        last_run_status=last_run["status"] if last_run else None,
    )


@router.get("/ratings.json", response_model=RatingsResponse, tags=["public"])
def get_ratings_json() -> RatingsResponse:
    rows = db.get_live_ratings()
    records = [_to_record(r) for r in rows]
    return RatingsResponse(metadata=_build_metadata(len(records)), ratings=records)


@router.get("/ratings.csv", tags=["public"])
def get_ratings_csv() -> StreamingResponse:
    rows = db.get_live_ratings()
    records = [_to_record(r) for r in rows]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for rec in records:
        writer.writerow(
            {
                "country_code": rec.country_code,
                "country_name": rec.country_name,
                "region": rec.region or "",
                "energy_risk_rating": rec.energy_risk_rating,
                "status_label": rec.status_label,
                "oil_reserve_days": rec.oil_reserve_days if rec.oil_reserve_days is not None else "",
                "lng_status": rec.lng_status or "",
                "key_risk": rec.key_risk or "",
                "primary_source": rec.primary_source or "",
                "primary_source_url": rec.primary_source_url or "",
                "secondary_sources": rec.secondary_sources or "",
                "source_urls": " | ".join(rec.source_urls) if rec.source_urls else "",
                "confidence": rec.confidence or "",
                "hormuz_dependency": rec.hormuz_dependency or "",
                "gdp_loss_pct": rec.gdp_loss_pct if rec.gdp_loss_pct is not None else "",
                "scoring_method": rec.scoring_method,
                "is_pinned": "1" if rec.is_pinned else "0",
                "scored_at": rec.scored_at,
            }
        )
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=energy_risk_ratings.csv"},
    )


@router.get("/ratings/{country_code}", response_model=RatingRecord, tags=["public"])
def get_rating(country_code: str) -> RatingRecord:
    code = country_code.upper()
    row = db.get_live_rating_for(code)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No live rating found for country code '{code}'")
    return _to_record(row)


@router.get("/health", response_model=HealthResponse, tags=["public"])
def health() -> HealthResponse:
    settings = get_settings()
    live_count = db.count_live_ratings()
    total_countries = len(db.get_all_countries())
    last_run = db.get_last_run()
    last_refresh = db.get_last_successful_run_time()
    now = datetime.now(timezone.utc)
    next_refresh = (
        now.replace(hour=settings.scoring_cron_hour, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return HealthResponse(
        status="ok",
        last_refresh=last_refresh,
        last_run_status=last_run["status"] if last_run else None,
        live_ratings_count=live_count,
        total_countries=total_countries,
        next_refresh=next_refresh,
    )
