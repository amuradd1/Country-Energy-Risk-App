"""Admin API endpoints — protected by X-Admin-Key header."""
from __future__ import annotations

import threading
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException

from .. import database as db
from ..config import get_settings
from ..models import (
    LogEntry,
    OverrideRequest,
    OverrideResponse,
    RatingRecord,
    TriggerResponse,
    UnpinRequest,
)

router = APIRouter()

_run_lock = threading.Lock()


def _require_admin(x_admin_key: str = Header(...)) -> None:
    settings = get_settings()
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")


@router.post("/override", response_model=OverrideResponse, tags=["admin"])
def override_rating(
    body: OverrideRequest,
    _auth: None = Depends(_require_admin),
) -> OverrideResponse:
    code = body.country_code.upper()
    if db.get_country(code) is None:
        raise HTTPException(status_code=404, detail=f"Unknown country code '{code}'")

    run_id = f"override-{uuid.uuid4().hex[:8]}"
    db.start_run(run_id, trigger_source="admin_override")

    with db.get_conn() as conn:
        conn.execute("BEGIN")
        try:
            # Supersede existing live row for this country only
            conn.execute(
                "UPDATE risk_ratings SET is_live = 0 WHERE country_code = ? AND is_live = 1",
                (code,),
            )
            db.insert_rating(
                conn,
                country_code=code,
                rating=body.rating,
                oil_reserve_days=body.oil_reserve_days,
                lng_status=body.lng_status,
                key_risk=body.key_risk,
                primary_source=body.primary_source or "Manual Override",
                secondary_sources=None,
                confidence=body.confidence or "High",
                hormuz_dependency=body.hormuz_dependency,
                gdp_loss_pct=None,
                scoring_method="manual_override",
                run_id=run_id,
                is_live=1,
                is_pinned=1 if body.pin else 0,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    db.finish_run(run_id, status="success", countries_scored=1)
    pin_msg = " (pinned — will survive automated runs)" if body.pin else ""
    return OverrideResponse(
        ok=True,
        country_code=code,
        rating=body.rating,
        pinned=body.pin,
        message=f"Rating for {code} set to {body.rating}{pin_msg}",
    )


@router.post("/unpin", tags=["admin"])
def unpin_country(
    body: UnpinRequest,
    _auth: None = Depends(_require_admin),
) -> dict[str, Any]:
    code = body.country_code.upper()
    updated = db.unpin_country(code)
    return {"ok": True, "country_code": code, "rows_updated": updated}


@router.post("/trigger-refresh", response_model=TriggerResponse, tags=["admin"])
def trigger_refresh(
    _auth: None = Depends(_require_admin),
) -> TriggerResponse:
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A scoring run is already in progress")
    try:
        from ..scoring import run_scoring  # lazy import

        run_id = run_scoring(trigger_source="admin_trigger")
        return TriggerResponse(ok=True, run_id=run_id, message="Scoring run completed successfully")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Scoring run failed: {exc}") from exc
    finally:
        _run_lock.release()


@router.get("/log", tags=["admin"])
def get_log(
    limit: int = 200,
    _auth: None = Depends(_require_admin),
) -> list[dict[str, Any]]:
    return db.get_log_entries(min(limit, 1000))


@router.get("/history/{country_code}", response_model=list[RatingRecord], tags=["admin"])
def get_history(
    country_code: str,
    _auth: None = Depends(_require_admin),
) -> list[RatingRecord]:
    code = country_code.upper()
    rows = db.get_rating_history(code)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No history found for '{code}'")
    return [
        RatingRecord(
            country_code=r["country_code"],
            country_name=r["country_name"],
            region=r.get("region"),
            energy_risk_rating=r["rating"],
            status_label=r["status_label"],
            oil_reserve_days=r.get("oil_reserve_days"),
            lng_status=r.get("lng_status"),
            key_risk=r.get("key_risk"),
            primary_source=r.get("primary_source"),
            secondary_sources=r.get("secondary_sources"),
            confidence=r.get("confidence"),
            hormuz_dependency=r.get("hormuz_dependency"),
            gdp_loss_pct=r.get("gdp_loss_pct"),
            scoring_method=r["scoring_method"],
            is_pinned=bool(r.get("is_pinned", 0)),
            scored_at=r["scored_at"],
        )
        for r in rows
    ]


@router.get("/runs", tags=["admin"])
def get_runs(
    limit: int = 50,
    _auth: None = Depends(_require_admin),
) -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (min(limit, 200),)
        ).fetchall()
    return [dict(r) for r in rows]
