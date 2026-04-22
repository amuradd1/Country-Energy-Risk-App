"""HTML pages: public landing page + admin control panel."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import database as db
from ..routers.public import _to_record
from ..schedule import get_next_refresh_time

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request) -> HTMLResponse:
    rows = db.get_live_ratings()
    records = [_to_record(r).model_dump() for r in rows]

    last_run = db.get_last_run()
    last_refresh = db.get_last_successful_run_time()
    now = datetime.now(timezone.utc)
    next_refresh = get_next_refresh_time(now=now).strftime("%Y-%m-%dT%H:%M:%SZ")

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "rows": records,
            "last_refresh": last_refresh,
            "next_refresh": next_refresh,
            "last_run_status": last_run["status"] if last_run else None,
        },
    )


@router.get("/admin-ui", response_class=HTMLResponse, include_in_schema=False)
def admin_ui(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("admin.html", {"request": request})
