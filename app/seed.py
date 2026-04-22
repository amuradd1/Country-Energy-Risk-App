"""Seed data: country list, Prewave direct ratings, EU mapping, regions."""
from __future__ import annotations

import uuid

from . import database as db

COUNTRIES: list[tuple[str, str, str]] = [
    ("AT", "Austria", "Europe"),
    ("CZ", "Czech Republic", "Europe"),
    ("VN", "Vietnam", "Asia"),
    ("BR", "Brazil", "South America"),
    ("FR", "France", "Europe"),
    ("CN", "China", "Asia"),
    ("MY", "Malaysia", "Asia"),
    ("GB", "United Kingdom", "Europe"),
    ("TH", "Thailand", "Asia"),
    ("KR", "South Korea", "Asia"),
    ("DE", "Germany", "Europe"),
    ("US", "USA", "North America"),
    ("JP", "Japan", "Asia"),
    ("ID", "Indonesia", "Asia"),
    ("HU", "Hungary", "Europe"),
    ("BG", "Bulgaria", "Europe"),
    ("CA", "Canada", "North America"),
    ("PH", "Philippines", "Asia"),
    ("TR", "Turkey", "Europe/Asia"),
    ("MX", "Mexico", "North America"),
    ("PK", "Pakistan", "South Asia"),
    ("ZA", "South Africa", "Africa"),
    ("AR", "Argentina", "South America"),
    ("PL", "Poland", "Europe"),
    ("SE", "Sweden", "Europe"),
    ("LK", "Sri Lanka", "South Asia"),
    ("KZ", "Kazakhstan", "Asia"),
    ("NG", "Nigeria", "Africa"),
    ("BD", "Bangladesh", "South Asia"),
    ("CL", "Chile", "South America"),
    ("CH", "Switzerland", "Europe"),
    ("NL", "Netherlands", "Europe"),
    ("AE", "UAE", "Middle East"),
    ("HR", "Croatia", "Europe"),
    ("IN", "India", "South Asia"),
    ("VE", "Venezuela", "South America"),
    ("HK", "Hong Kong", "Asia"),
    ("FI", "Finland", "Europe"),
    ("JO", "Jordan", "Middle East"),
    ("IT", "Italy", "Europe"),
    ("SG", "Singapore", "Asia"),
    ("UZ", "Uzbekistan", "Asia"),
    ("KE", "Kenya", "Africa"),
    ("UA", "Ukraine", "Europe"),
    ("RO", "Romania", "Europe"),
    ("RS", "Serbia", "Europe"),
    ("CR", "Costa Rica", "Central America"),
    ("DK", "Denmark", "Europe"),
    ("DZ", "Algeria", "Africa"),
    ("PG", "Papua New Guinea", "Oceania"),
    ("ZM", "Zambia", "Africa"),
    ("FJ", "Fiji", "Oceania"),
    ("BE", "Belgium", "Europe"),
    ("MZ", "Mozambique", "Africa"),
    ("ZW", "Zimbabwe", "Africa"),
    ("NZ", "New Zealand", "Oceania"),
    ("TT", "Trinidad and Tobago", "Caribbean"),
    ("WS", "Samoa", "Oceania"),
    ("PY", "Paraguay", "South America"),
    ("EG", "Egypt", "Africa"),
    ("ES", "Spain", "Europe"),
    ("AU", "Australia", "Oceania"),
]

PREWAVE_DIRECT: dict[str, dict] = {
    "BD": {"rating": 5, "oil_reserve_days": 5,   "lng_status": "Exhausted", "hormuz_dependency": "HIGH",
           "key_risk": "Triple LNG FM. SPR exhausted. RMG on diesel generators.", "gdp_loss_pct": 4.17},
    "PK": {"rating": 4, "oil_reserve_days": 20,  "lng_status": "Critical",  "hormuz_dependency": "HIGH",
           "key_risk": "85% crude dependency. 4-day govt workweek. IMF emergency.", "gdp_loss_pct": 3.08},
    "IN": {"rating": 3, "oil_reserve_days": 74,  "lng_status": "Stressed",  "hormuz_dependency": "CRIT",
           "key_risk": "LPG crisis: 60% Gulf-sourced. API feedstock +30%. Crude adequate.", "gdp_loss_pct": 3.89},
    "KR": {"rating": 3, "oil_reserve_days": 210, "lng_status": "Stressed",  "hormuz_dependency": "CRIT",
           "key_risk": "Crude buffered but naphtha feedstock crisis week 3. Helium 64.7% Qatar.", "gdp_loss_pct": 1.73},
    "JP": {"rating": 1, "oil_reserve_days": 254, "lng_status": "Buffered",  "hormuz_dependency": "CRIT",
           "key_risk": "Best-buffered importer. Nuclear ramp. Helium/methanol structural risk.", "gdp_loss_pct": 1.61},
    "CN": {"rating": 2, "oil_reserve_days": 108, "lng_status": "Managed",   "hormuz_dependency": "MOD",
           "key_risk": "1.3bn bbl SPR. Coal-to-methanol offsets. Fuel export ban. Strategic winner.", "gdp_loss_pct": 1.61},
    "US": {"rating": 1, "oil_reserve_days": 700, "lng_status": "Buffered",  "hormuz_dependency": "LOW",
           "key_risk": "Airgas helium FM only domestic bottleneck. Jones Act waived.", "gdp_loss_pct": 0.5},
}

EU_BASELINE: dict = {
    "rating": 3,
    "oil_reserve_days": 90,
    "lng_status": "Stressed",
    "hormuz_dependency": "MOD",
    "key_risk": "LNG price shock +70%. Winter 26/27 storage fill risk. Crackers 72%.",
    "gdp_loss_pct": 1.61,
}

EU_COUNTRIES: list[str] = [
    "AT", "CZ", "FR", "DE", "HU", "BG", "PL", "SE",
    "HR", "FI", "IT", "NL", "BE", "DK", "RO", "ES",
]


def seed_countries_and_baseline() -> str:
    """Insert country rows and initial live ratings from Prewave baseline.

    Runs on first startup. Idempotent: skips baseline insert if any live ratings exist.
    Returns the seed run_id.
    """
    db.init_db()

    run_id = f"seed-{uuid.uuid4().hex[:12]}"

    with db.get_conn() as conn:
        for code, name, region in COUNTRIES:
            if code in PREWAVE_DIRECT:
                mapped = "Direct"
            elif code in EU_COUNTRIES:
                mapped = "EU / Germany"
            else:
                mapped = None
            db.upsert_country(conn, code, name, region, mapped)

    if db.count_live_ratings() > 0:
        return run_id

    db.start_run(run_id, trigger_source="seed")

    inserted = 0
    with db.get_conn() as conn:
        for code, name, _region in COUNTRIES:
            if code in PREWAVE_DIRECT:
                p = PREWAVE_DIRECT[code]
                db.insert_rating(
                    conn,
                    country_code=code,
                    rating=p["rating"],
                    oil_reserve_days=p.get("oil_reserve_days"),
                    lng_status=p.get("lng_status"),
                    key_risk=p.get("key_risk"),
                    primary_source="Prewave SITREP Day 33",
                    secondary_sources=None,
                    confidence="High",
                    hormuz_dependency=p.get("hormuz_dependency"),
                    gdp_loss_pct=p.get("gdp_loss_pct"),
                    scoring_method="prewave_direct",
                    run_id=run_id,
                )
                inserted += 1
            elif code in EU_COUNTRIES:
                b = EU_BASELINE
                db.insert_rating(
                    conn,
                    country_code=code,
                    rating=b["rating"],
                    oil_reserve_days=b.get("oil_reserve_days"),
                    lng_status=b.get("lng_status"),
                    key_risk=b.get("key_risk"),
                    primary_source="Prewave SITREP Day 33 (EU/Germany baseline)",
                    secondary_sources=None,
                    confidence="Medium",
                    hormuz_dependency=b.get("hormuz_dependency"),
                    gdp_loss_pct=b.get("gdp_loss_pct"),
                    scoring_method="prewave_eu_mapped",
                    run_id=run_id,
                )
                inserted += 1
            else:
                # placeholder rating so the feed is never empty before first AI run
                db.insert_rating(
                    conn,
                    country_code=code,
                    rating=3,
                    oil_reserve_days=None,
                    lng_status=None,
                    key_risk="Awaiting first automated AI assessment.",
                    primary_source="Placeholder (pending AI scoring)",
                    secondary_sources=None,
                    confidence="Low",
                    hormuz_dependency=None,
                    gdp_loss_pct=None,
                    scoring_method="placeholder",
                    run_id=run_id,
                )
                inserted += 1

    db.finish_run(run_id, status="success", countries_scored=inserted)
    return run_id
