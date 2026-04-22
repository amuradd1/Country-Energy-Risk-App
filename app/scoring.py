"""AI scoring engine. Uses Anthropic API with web_search tool to research each country."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from anthropic import Anthropic

from . import database as db
from .config import get_settings
from .seed import COUNTRIES, EU_BASELINE, EU_COUNTRIES, PREWAVE_DIRECT

logger = logging.getLogger(__name__)

CONTEXT_DATE_HINT = "The Strait of Hormuz has been effectively closed since February 28, 2026 due to the US-Israel-Iran conflict. P&I insurance withdrawal (not military blockade) is the operative closure mechanism. IEA released 400mb from strategic reserves. Three commodity chains are in confirmed physical shortage: crude oil, LNG, naphtha."


def _build_prompt(today_iso: str, batch: list[tuple[str, str]], mode: str) -> str:
    """Build the scoring prompt for a batch.

    mode:
      - "ai_researched": full web search
      - "prewave_eu_mapped": anchored on EU baseline, AI adjusts +/-1
      - "prewave_direct": anchored on Prewave direct value, AI validates
    """
    if mode == "prewave_eu_mapped":
        anchor = (
            f"\nANCHOR: These countries inherit the Prewave EU/Germany baseline: "
            f"rating={EU_BASELINE['rating']} ({_label(EU_BASELINE['rating'])}), "
            f"oil_reserve_days={EU_BASELINE['oil_reserve_days']}, "
            f"lng_status={EU_BASELINE['lng_status']}, "
            f"hormuz_dependency={EU_BASELINE['hormuz_dependency']}. "
            "Adjust +/-1 ONLY if country-specific factors (energy mix, domestic production, landlocked, "
            "storage levels, nuclear share, emergency measures) clearly warrant it."
        )
    elif mode == "prewave_direct":
        direct_notes = []
        for code, _name in batch:
            p = PREWAVE_DIRECT.get(code)
            if p:
                direct_notes.append(
                    f"  {code}: Prewave says rating={p['rating']} ({_label(p['rating'])}), "
                    f"oil_days={p.get('oil_reserve_days')}, lng={p.get('lng_status')}, "
                    f"hormuz={p.get('hormuz_dependency')}, risk=\"{p.get('key_risk')}\""
                )
        anchor = (
            "\nANCHOR: Prewave SITREP Day 33 provides DIRECT ratings for these countries. "
            "Validate with current web search but stay within +/-1 of the Prewave value unless "
            "overwhelming new evidence emerges.\n" + "\n".join(direct_notes)
        )
    else:
        anchor = ""

    country_list = "\n".join(f"  - {code}: {name}" for code, name in batch)

    return f"""You are an energy security analyst assessing country-level energy risk as of {today_iso}.

CONTEXT: {CONTEXT_DATE_HINT}

For each country below, search for the LATEST information on:
- Oil/fuel reserve levels (days of supply)
- LNG/gas supply status
- Any emergency measures (rationing, 4-day workweeks, school closures, price caps)
- Hormuz dependency (% of oil/gas imports routed through the Strait)
- Force majeure declarations affecting the country
- Government statements on energy security

Then assign a rating from 1-5 (where 1 = best, 5 = worst):
1 = STABLE: >120 days reserves, diversified supply, strategic reserves intact
2 = ELEVATED: 60-120 days, price pressure but stable, diversification underway
3 = STRESSED: 21-60 days, elevated prices >30%, disruptions, contingency active
4 = SEVERE: 14-21 days, emergency measures active, panic buying
5 = CRITICAL: <14 days reserves, active rationing, FM declared, supply collapsed
{anchor}

COUNTRIES TO ASSESS:
{country_list}

Respond ONLY with a valid JSON array, no markdown, no preamble, no trailing commentary:
[{{
  "country_code": "XX",
  "rating": N,
  "oil_reserve_days": N,
  "lng_status": "one of: Exhausted/Depleted/Critical/Minimal/Low/Stressed/Partial/Managed/Buffered",
  "key_risk": "max 100 chars",
  "primary_source": "source name",
  "secondary_sources": "comma-separated",
  "confidence": "High/Medium/Low",
  "hormuz_dependency": "HIGH/CRIT/MOD/LOW/NONE"
}}]
"""


def _label(rating: int) -> str:
    return db.STATUS_LABELS.get(rating, "Stressed")


def _extract_text(message: Any) -> str:
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(getattr(block, "text", "") or "")
    return "\n".join(parts).strip()


def _parse_json_array(text: str) -> list[dict[str, Any]]:
    """Parse a JSON array out of a model response that may have wrapper markdown/prose."""
    if not text:
        return []
    cleaned = text.strip()
    # Strip triple-backtick fences if present
    fence_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1)
    else:
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start != -1 and end != -1 and end > start:
            cleaned = cleaned[start : end + 1]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning("JSON parse failed: %s", e)
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


def _clamp_rating(r: Any) -> int:
    try:
        n = int(r)
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, n))


def _clamp_reserve(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return None


def _score_batch(
    client: Anthropic,
    batch: list[tuple[str, str]],
    mode: str,
    run_id: str,
) -> list[dict[str, Any]]:
    settings = get_settings()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = _build_prompt(today, batch, mode)

    try:
        message = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=4096,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.exception("Anthropic API call failed for batch mode=%s", mode)
        db.log_scoring(
            run_id,
            country_code=None,
            raw_response=f"API_ERROR mode={mode} batch={[c for c, _ in batch]}: {e}",
            queries=None,
        )
        return []

    text = _extract_text(message)
    db.log_scoring(
        run_id,
        country_code=None,
        raw_response=f"mode={mode} batch={[c for c, _ in batch]}\n{text}",
        queries=None,
    )
    return _parse_json_array(text)


def _merge_result(
    code: str,
    name: str,
    mode: str,
    ai: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Combine AI response with Prewave baseline fallbacks per scoring priority."""
    if mode == "prewave_direct":
        base = dict(PREWAVE_DIRECT.get(code, {}))
        rating = _clamp_rating(ai.get("rating")) if ai else base.get("rating", 3)
        return {
            "rating": rating,
            "oil_reserve_days": _clamp_reserve((ai or {}).get("oil_reserve_days")) or base.get("oil_reserve_days"),
            "lng_status": (ai or {}).get("lng_status") or base.get("lng_status"),
            "key_risk": (ai or {}).get("key_risk") or base.get("key_risk"),
            "primary_source": (ai or {}).get("primary_source") or "Prewave SITREP Day 33",
            "secondary_sources": (ai or {}).get("secondary_sources"),
            "confidence": (ai or {}).get("confidence") or "High",
            "hormuz_dependency": (ai or {}).get("hormuz_dependency") or base.get("hormuz_dependency"),
            "gdp_loss_pct": base.get("gdp_loss_pct"),
            "scoring_method": "prewave_direct",
        }

    if mode == "prewave_eu_mapped":
        base = dict(EU_BASELINE)
        rating = _clamp_rating((ai or {}).get("rating") or base["rating"])
        # clamp to baseline +/- 1
        anchor = base["rating"]
        rating = max(anchor - 1, min(anchor + 1, rating))
        return {
            "rating": rating,
            "oil_reserve_days": _clamp_reserve((ai or {}).get("oil_reserve_days")) or base.get("oil_reserve_days"),
            "lng_status": (ai or {}).get("lng_status") or base.get("lng_status"),
            "key_risk": (ai or {}).get("key_risk") or base.get("key_risk"),
            "primary_source": (ai or {}).get("primary_source") or "Prewave SITREP Day 33 (EU baseline)",
            "secondary_sources": (ai or {}).get("secondary_sources"),
            "confidence": (ai or {}).get("confidence") or "Medium",
            "hormuz_dependency": (ai or {}).get("hormuz_dependency") or base.get("hormuz_dependency"),
            "gdp_loss_pct": base.get("gdp_loss_pct"),
            "scoring_method": "prewave_eu_mapped",
        }

    # ai_researched — AI is authoritative; if missing, fall back to a neutral 3/Stressed
    if ai is None:
        return {
            "rating": 3,
            "oil_reserve_days": None,
            "lng_status": None,
            "key_risk": f"AI scoring failed for {name}; awaiting retry.",
            "primary_source": "Fallback (AI scoring failed)",
            "secondary_sources": None,
            "confidence": "Low",
            "hormuz_dependency": None,
            "gdp_loss_pct": None,
            "scoring_method": "ai_researched",
        }
    return {
        "rating": _clamp_rating(ai.get("rating")),
        "oil_reserve_days": _clamp_reserve(ai.get("oil_reserve_days")),
        "lng_status": ai.get("lng_status"),
        "key_risk": (ai.get("key_risk") or "")[:200] or None,
        "primary_source": ai.get("primary_source") or "AI web search",
        "secondary_sources": ai.get("secondary_sources"),
        "confidence": ai.get("confidence") or "Medium",
        "hormuz_dependency": ai.get("hormuz_dependency"),
        "gdp_loss_pct": None,
        "scoring_method": "ai_researched",
    }


def _chunks(seq: list, n: int) -> list[list]:
    return [seq[i : i + n]] if n <= 0 else [seq[i : i + n] for i in range(0, len(seq), n)]


def run_scoring(trigger_source: str = "scheduler") -> str:
    """Run full daily scoring pass. Atomically replaces live ratings on success."""
    settings = get_settings()
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    db.start_run(run_id, trigger_source=trigger_source)

    pinned = db.get_pinned_codes()
    name_by_code = {code: name for code, name, _ in COUNTRIES}

    # Build batches by scoring mode, excluding pinned countries.
    direct = [(c, name_by_code[c]) for c in PREWAVE_DIRECT if c not in pinned]
    eu = [(c, name_by_code[c]) for c in EU_COUNTRIES if c not in pinned]
    handled = set(PREWAVE_DIRECT.keys()) | set(EU_COUNTRIES)
    ai = [(c, n) for c, n, _ in COUNTRIES if c not in handled and c not in pinned]

    results: dict[str, dict[str, Any]] = {}

    client: Optional[Anthropic] = None
    if settings.anthropic_api_key:
        client = Anthropic(api_key=settings.anthropic_api_key)
    else:
        logger.warning("ANTHROPIC_API_KEY not set — skipping AI calls, using baselines only.")

    batch_size = max(1, settings.scoring_batch_size)

    def process(batches: list[list[tuple[str, str]]], mode: str) -> None:
        for batch in batches:
            ai_rows: list[dict[str, Any]] = []
            if client is not None:
                ai_rows = _score_batch(client, batch, mode, run_id)
            by_code = {row.get("country_code"): row for row in ai_rows if row.get("country_code")}
            for code, name in batch:
                results[code] = _merge_result(code, name, mode, by_code.get(code))

    process(_chunks(direct, batch_size), "prewave_direct")
    process(_chunks(eu, batch_size), "prewave_eu_mapped")
    process(_chunks(ai, batch_size), "ai_researched")

    # Atomic swap: supersede non-pinned live rows, then insert the fresh batch.
    inserted = 0
    with db.get_conn() as conn:
        conn.execute("BEGIN")
        try:
            db.supersede_live(conn, keep_pinned=True)
            for code, r in results.items():
                db.insert_rating(
                    conn,
                    country_code=code,
                    rating=r["rating"],
                    oil_reserve_days=r["oil_reserve_days"],
                    lng_status=r["lng_status"],
                    key_risk=r["key_risk"],
                    primary_source=r["primary_source"],
                    secondary_sources=r["secondary_sources"],
                    confidence=r["confidence"],
                    hormuz_dependency=r["hormuz_dependency"],
                    gdp_loss_pct=r.get("gdp_loss_pct"),
                    scoring_method=r["scoring_method"],
                    run_id=run_id,
                    is_live=1,
                    is_pinned=0,
                )
                inserted += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            db.finish_run(run_id, status="failed", countries_scored=0)
            raise

    db.finish_run(run_id, status="success", countries_scored=inserted)
    return run_id
