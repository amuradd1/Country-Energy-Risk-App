"""Seed data: country list, priority tiers, EU bloc tagging, source whitelists,
Prewave baseline ratings, and the active scenario.

Phase 1 of v2 architecture. Phase 2 will replace the scoring path itself.
"""
from __future__ import annotations

import uuid

from . import database as db


# Active priority list (43 countries). Anything not in this list is in DROPPED_CODES below.
COUNTRIES: list[tuple[str, str, str]] = [
    ("AR", "Argentina", "South America"),
    ("AU", "Australia", "Oceania"),
    ("AT", "Austria", "Europe"),
    ("BR", "Brazil", "South America"),
    ("BG", "Bulgaria", "Europe"),
    ("CA", "Canada", "North America"),
    ("CL", "Chile", "South America"),
    ("CN", "China", "Asia"),
    ("HR", "Croatia", "Europe"),
    ("CZ", "Czech Republic", "Europe"),
    ("FI", "Finland", "Europe"),
    ("FR", "France", "Europe"),
    ("DE", "Germany", "Europe"),
    ("HK", "Hong Kong", "Asia"),
    ("HU", "Hungary", "Europe"),
    ("IN", "India", "South Asia"),
    ("ID", "Indonesia", "Asia"),
    ("IT", "Italy", "Europe"),
    ("JP", "Japan", "Asia"),
    ("JO", "Jordan", "Middle East"),
    ("KZ", "Kazakhstan", "Asia"),
    ("KE", "Kenya", "Africa"),
    ("MY", "Malaysia", "Asia"),
    ("MX", "Mexico", "North America"),
    ("NL", "Netherlands", "Europe"),
    ("NG", "Nigeria", "Africa"),
    ("PK", "Pakistan", "South Asia"),
    ("PH", "Philippines", "Asia"),
    ("PL", "Poland", "Europe"),
    ("SG", "Singapore", "Asia"),
    ("ZA", "South Africa", "Africa"),
    ("KR", "South Korea", "Asia"),
    ("LK", "Sri Lanka", "South Asia"),
    ("SE", "Sweden", "Europe"),
    ("CH", "Switzerland", "Europe"),
    ("TH", "Thailand", "Asia"),
    ("TR", "Turkey", "Europe/Asia"),
    ("UA", "Ukraine", "Europe"),
    ("AE", "UAE", "Middle East"),
    ("GB", "United Kingdom", "Europe"),
    ("US", "USA", "North America"),
    ("VE", "Venezuela", "South America"),
    ("VN", "Vietnam", "Asia"),
]

# Soft-deleted countries — kept in DB with is_active=0 so historical ratings remain.
DROPPED_CODES: list[tuple[str, str, str]] = [
    ("BD", "Bangladesh", "South Asia"),
    ("UZ", "Uzbekistan", "Asia"),
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
]

# 13-country Continental Europe bloc — single AI call, all members inherit the rating.
EU_BLOC_CODES: list[str] = [
    "AT", "BG", "HR", "CZ", "FI", "FR", "DE", "HU", "IT", "NL", "PL", "SE", "CH",
]

# Prewave Day 33 direct ratings (where applicable to the 43-country priority list).
# BD is in DROPPED_CODES so it's no longer used at runtime, but the data is retained for history.
PREWAVE_DIRECT: dict[str, dict] = {
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

# Country-specific source whitelists. Used by the v2 challenger prompt so the model
# searches domestic + authoritative outlets first. ~3-5 sources per country, mix of
# major domestic news + national energy ministry + relevant trade body. Always
# supplemented by IEA / EIA / Reuters / Bloomberg / FT / AP as fallback tier-2 sources.
SOURCE_WHITELIST: dict[str, list[str]] = {
    "AR": ["clarin.com", "lanacion.com.ar", "ambito.com", "energia.gob.ar", "telam.com.ar"],
    "AU": ["afr.com", "abc.net.au", "aemo.com.au", "energy.gov.au", "theaustralian.com.au"],
    "BR": ["valor.globo.com", "estadao.com.br", "anp.gov.br", "epe.gov.br", "g1.globo.com"],
    "CA": ["theglobeandmail.com", "cbc.ca", "cer-rec.gc.ca", "natural-resources.canada.ca"],
    "CL": ["latercera.com", "cne.cl", "df.cl", "elmercurio.com"],
    "CN": ["caixin.com", "scmp.com", "nea.gov.cn", "xinhuanet.com", "globaltimes.cn"],
    "HK": ["scmp.com", "thestandard.com.hk", "emsd.gov.hk"],
    "IN": ["economictimes.indiatimes.com", "thehindu.com", "petroleum.nic.in", "ppac.gov.in", "livemint.com"],
    "ID": ["thejakartapost.com", "kompas.com", "esdm.go.id", "tempo.co"],
    "JP": ["nikkei.com", "japantimes.co.jp", "meti.go.jp", "asahi.com", "reuters.com/places/japan"],
    "JO": ["jordantimes.com", "memr.gov.jo", "petra.gov.jo"],
    "KZ": ["tengrinews.kz", "kazinform.kz", "energo.gov.kz"],
    "KE": ["nation.africa", "epra.go.ke", "businessdailyafrica.com", "kbc.co.ke"],
    "MY": ["thestar.com.my", "theedgemarkets.com", "kpdn.gov.my", "petronas.com"],
    "MX": ["eluniversal.com.mx", "reforma.com", "gob.mx/sener", "expansion.mx"],
    "NG": ["punchng.com", "thisdaylive.com", "nuprc.gov.ng", "premiumtimesng.com"],
    "PK": ["dawn.com", "tribune.com.pk", "ogra.org.pk", "thenews.com.pk", "geo.tv"],
    "PH": ["inquirer.net", "rappler.com", "doe.gov.ph", "bworldonline.com"],
    "SG": ["straitstimes.com", "channelnewsasia.com", "ema.gov.sg", "businesstimes.com.sg"],
    "ZA": ["news24.com", "businesslive.co.za", "energy.gov.za", "iol.co.za"],
    "KR": ["koreaherald.com", "koreatimes.co.kr", "motie.go.kr", "english.hani.co.kr"],
    "LK": ["dailymirror.lk", "sundaytimes.lk", "energy.gov.lk", "newsfirst.lk"],
    "TH": ["bangkokpost.com", "nationthailand.com", "energy.go.th", "thaipbsworld.com"],
    "TR": ["hurriyetdailynews.com", "dailysabah.com", "enerji.gov.tr", "anadoluagency.com.tr"],
    "UA": ["kyivindependent.com", "pravda.com.ua", "mev.gov.ua", "interfax.com.ua"],
    "AE": ["thenationalnews.com", "gulfnews.com", "moei.gov.ae", "khaleejtimes.com"],
    "GB": ["ft.com", "thetimes.co.uk", "bbc.co.uk", "gov.uk/desnz", "telegraph.co.uk"],
    "US": ["wsj.com", "bloomberg.com", "energy.gov", "eia.gov", "reuters.com"],
    "VE": ["el-nacional.com", "efectococuyo.com", "minpetroleo.gob.ve"],
    "VN": ["vnexpress.net", "vietnamnet.vn", "moit.gov.vn", "thesaigontimes.vn"],
    "CH": ["nzz.ch", "swissinfo.ch", "bfe.admin.ch", "tagesanzeiger.ch"],
    # EU-bloc shared sources used when scoring the bloc as a whole:
    "_EU_BLOC": ["ft.com", "politico.eu", "bruegel.org", "ec.europa.eu", "entsog.eu",
                 "entsoe.eu", "reuters.com/markets/europe", "spglobal.com/commodityinsights"],
}

# Always-acceptable global tier-2 sources — supplement domestic outlets.
GLOBAL_TIER2_SOURCES: list[str] = [
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "apnews.com",
    "economist.com", "spglobal.com", "argusmedia.com",
]

# Authoritative tier-1 (government / multilateral) sources used for ±2 whipsaw rule.
GLOBAL_TIER1_SOURCES: list[str] = [
    "iea.org", "eia.gov", "imf.org", "worldbank.org", "opec.org",
    "irena.org", "gecf.org", "europa.eu", ".gov", ".gov.uk", ".gov.in",
    ".gov.au", ".gov.za", "go.jp", ".gov.sg",
]


# Initial Hormuz scenario context — extracted from the previously-hardcoded
# CONTEXT_DATE_HINT in scoring.py. Lives in the scenarios table from now on so admins
# can update without code pushes when the active worldview changes.
INITIAL_SCENARIO = {
    "scenario_id": "hormuz_2026",
    "name": "Strait of Hormuz Closure 2026",
    "description": (
        "US-Israel-Iran conflict in early 2026; P&I insurance withdrawal effectively "
        "closes the Strait of Hormuz to crude/LNG/naphtha shipping. Replace with a new "
        "scenario when world conditions change materially."
    ),
    "context_block": (
        "The Strait of Hormuz has been effectively closed since February 28, 2026 "
        "due to the US-Israel-Iran conflict. P&I insurance withdrawal (not military "
        "blockade) is the operative closure mechanism. IEA released 400mb from "
        "strategic reserves. Three commodity chains are in confirmed physical "
        "shortage: crude oil, LNG, naphtha."
    ),
    "affected_commodities": "crude oil,LNG,naphtha,helium",
    "affected_routes": "Strait of Hormuz",
    "started_at": "2026-02-28T00:00:00Z",
}


def _whitelist_for(code: str) -> str | None:
    """Return comma-separated whitelist for a country, including global tier-2 fallbacks."""
    domestic = SOURCE_WHITELIST.get(code, [])
    if not domestic and code not in EU_BLOC_CODES:
        return None
    if code in EU_BLOC_CODES:
        # EU bloc countries inherit shared EU sources alongside any domestic ones
        merged = domestic + SOURCE_WHITELIST["_EU_BLOC"] + GLOBAL_TIER2_SOURCES
    else:
        merged = domestic + GLOBAL_TIER2_SOURCES
    # de-dupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for s in merged:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return ",".join(out)


def seed_countries_and_baseline() -> str:
    """Insert / upsert all country rows (active + dropped), seed the scenario,
    and seed initial baseline ratings if the table is empty.

    Idempotent: re-running keeps everyone in the right tier and reflects any
    code-side changes to the lists above. Soft-delete state is enforced.
    """
    db.init_db()

    run_id = f"seed-{uuid.uuid4().hex[:12]}"

    with db.get_conn() as conn:
        # 1) active priority list — every country is P1 by default
        for code, name, region in COUNTRIES:
            group = "EU_BLOC" if code in EU_BLOC_CODES else None
            mapped = "Direct" if code in PREWAVE_DIRECT else (
                "EU / Germany" if code in EU_BLOC_CODES else None
            )
            db.upsert_country(
                conn, code, name, region, mapped,
                is_active=1,
                priority_tier="P1",
                country_group=group,
                source_whitelist=_whitelist_for(code),
            )

        # 2) soft-deleted countries — kept in DB with is_active=0
        for code, name, region in DROPPED_CODES:
            db.upsert_country(
                conn, code, name, region, None,
                is_active=0,
                priority_tier="P3",
                country_group=None,
                source_whitelist=None,
            )

        # 3) seed the active scenario (idempotent upsert; preserve is_active state)
        existing = db.get_active_scenario()
        is_active_flag = 1 if existing is None or existing.get("scenario_id") == INITIAL_SCENARIO["scenario_id"] else 0
        db.upsert_scenario(
            conn,
            scenario_id=INITIAL_SCENARIO["scenario_id"],
            name=INITIAL_SCENARIO["name"],
            description=INITIAL_SCENARIO["description"],
            context_block=INITIAL_SCENARIO["context_block"],
            affected_commodities=INITIAL_SCENARIO["affected_commodities"],
            affected_routes=INITIAL_SCENARIO["affected_routes"],
            started_at=INITIAL_SCENARIO["started_at"],
            is_active=is_active_flag,
        )

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
            elif code in EU_BLOC_CODES:
                b = EU_BASELINE
                db.insert_rating(
                    conn,
                    country_code=code,
                    rating=b["rating"],
                    oil_reserve_days=b.get("oil_reserve_days"),
                    lng_status=b.get("lng_status"),
                    key_risk=b.get("key_risk"),
                    primary_source="Prewave SITREP Day 33 (EU bloc baseline)",
                    secondary_sources=None,
                    confidence="Medium",
                    hormuz_dependency=b.get("hormuz_dependency"),
                    gdp_loss_pct=b.get("gdp_loss_pct"),
                    scoring_method="prewave_eu_mapped",
                    run_id=run_id,
                )
                inserted += 1
            else:
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


# Backwards-compat alias — older modules still reference EU_COUNTRIES; keep both names
# pointing at the same list so phase 2 doesn't have to chase imports.
EU_COUNTRIES: list[str] = EU_BLOC_CODES
