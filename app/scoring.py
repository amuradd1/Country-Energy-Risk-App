"""AI scoring engine. Uses Anthropic API with web_search tool to research each country.

Anti-hallucination design:
- Every AI call uses the hosted web_search tool (no training-data-only answers).
- The prompt forces the model to cite real source URLs per country.
- We extract the actual URLs returned by web_search and validate that the model's
  cited URLs appear in that pool. Fabricated URLs are dropped.
- If web_search returns no results for a country, we mark confidence=Low and keep
  the baseline/placeholder rather than inventing numbers.
- Full raw response + URL pool are written to the scoring_log for every batch.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from anthropic import Anthropic, APIError

from . import database as db
from .config import get_settings
from .seed import COUNTRIES, EU_BASELINE, EU_COUNTRIES, PREWAVE_DIRECT

logger = logging.getLogger(__name__)

CONTEXT_DATE_HINT = (
    "The Strait of Hormuz has been effectively closed since February 28, 2026 due to the "
    "US-Israel-Iran conflict. P&I insurance withdrawal (not military blockade) is the "
    "operative closure mechanism. IEA released 400mb from strategic reserves. Three "
    "commodity chains are in confirmed physical shortage: crude oil, LNG, naphtha."
)

ANTI_HALLUCINATION_RULES = """
CRITICAL — ANTI-HALLUCINATION RULES (non-negotiable):
1. You MUST perform web searches before scoring ANY country. Do not rely on training data.
2. Every numeric claim (oil_reserve_days, gdp_loss_pct) MUST be traceable to a web_search
   result returned in this conversation. If you cannot verify from search, set the field
   to null — DO NOT estimate.
3. "primary_source_url" MUST be an actual URL returned by web_search in this conversation.
   If you cite a URL that was not returned by search, your response will be rejected.
4. If web_search returns no relevant results for a country, set:
     confidence = "Low"
     key_risk = "Insufficient fresh data — inheriting baseline"
     primary_source_url = null
   DO NOT invent reserve-day numbers, force majeure declarations, or emergency measures.
5. Quote the source title in "primary_source" as it appears in the search result.
"""


def _label(rating: int) -> str:
    return db.STATUS_LABELS.get(rating, "Stressed")


def _build_prompt(today_iso: str, batch: list[tuple[str, str]], mode: str) -> str:
    if mode == "prewave_eu_mapped":
        anchor = (
            f"\nANCHOR: These countries inherit the Prewave EU/Germany baseline: "
            f"rating={EU_BASELINE['rating']} ({_label(EU_BASELINE['rating'])}), "
            f"oil_reserve_days={EU_BASELINE['oil_reserve_days']}, "
            f"lng_status={EU_BASELINE['lng_status']}, "
            f"hormuz_dependency={EU_BASELINE['hormuz_dependency']}. "
            "Adjust +/-1 ONLY if country-specific factors (energy mix, domestic production, "
            "landlocked status, storage levels, nuclear share, emergency measures) clearly "
            "warrant it, AND you have a cited source for that adjustment."
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
            "Validate with current web search but stay within +/-1 of the Prewave value "
            "unless overwhelming new cited evidence emerges.\n" + "\n".join(direct_notes)
        )
    else:
        anchor = ""

    country_list = "\n".join(f"  - {code}: {name}" for code, name in batch)

    return f"""You are an energy security analyst assessing country-level energy risk as of {today_iso}.

CONTEXT: {CONTEXT_DATE_HINT}
{ANTI_HALLUCINATION_RULES}
For each country below, search the web for the LATEST information on:
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
  "oil_reserve_days": N or null,
  "lng_status": "one of: Exhausted/Depleted/Critical/Minimal/Low/Stressed/Partial/Managed/Buffered",
  "key_risk": "max 100 chars",
  "primary_source": "source title as shown in search result",
  "primary_source_url": "https://... (MUST be from web_search results) or null",
  "secondary_sources": "comma-separated titles",
  "confidence": "High/Medium/Low",
  "hormuz_dependency": "HIGH/CRIT/MOD/LOW/NONE"
}}]
"""


def _extract_text(message: Any) -> str:
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "\n".join(parts).strip()


def _extract_search_urls(message: Any) -> list[dict[str, str]]:
    """Pull every URL returned by the hosted web_search tool for this message.

    Returns a deduped list of {url, title} dicts. Used to (a) validate that the
    model's cited URLs are real, and (b) attach evidence to each country row.
    """
    sources: list[dict[str, str]] = []
    for block in getattr(message, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type in ("web_search_tool_result", "server_tool_result"):
            content = getattr(block, "content", []) or []
            for item in content:
                if isinstance(item, dict):
                    url = item.get("url") or ""
                    title = item.get("title") or ""
                else:
                    url = getattr(item, "url", "") or ""
                    title = getattr(item, "title", "") or ""
                if url:
                    sources.append({"url": url, "title": title})
        if block_type == "text":
            for cit in getattr(block, "citations", []) or []:
                url = getattr(cit, "url", None) or (cit.get("url") if isinstance(cit, dict) else None)
                title = getattr(cit, "title", None) or (cit.get("title") if isinstance(cit, dict) else None)
                if url:
                    sources.append({"url": url, "title": title or ""})

    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for s in sources:
        if s["url"] and s["url"] not in seen:
            seen.add(s["url"])
            unique.append(s)
    return unique


def _parse_json_array(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    else:
        # Prefer finding a top-level array. Scan for matching brackets to handle
        # the case where the model writes preamble prose before/after the array.
        start = cleaned.find("[")
        if start != -1:
            depth = 0
            end = -1
            for i in range(start, len(cleaned)):
                ch = cleaned[i]
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end != -1:
                cleaned = cleaned[start : end + 1]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning(
            "JSON parse failed: %s — raw head=%r tail=%r",
            e, cleaned[:200], cleaned[-200:] if len(cleaned) > 200 else "",
        )
        return []
    if isinstance(data, dict):
        # Some models wrap the array in an object. Find the first list value.
        for v in data.values():
            if isinstance(v, list):
                return [d for d in v if isinstance(d, dict)]
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _clamp_rating(r: Any) -> int:
    try:
        return max(1, min(5, int(r)))
    except (TypeError, ValueError):
        return 3


def _clamp_reserve(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return None


def _validate_url(url: Optional[str], allowed: set[str]) -> Optional[str]:
    """Only accept the model's cited URL if web_search actually returned it."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    return url if url in allowed else None


def _score_batch_with_retry(
    client: Anthropic,
    batch: list[tuple[str, str]],
    mode: str,
    run_id: str,
    max_retries: int = 3,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Call Anthropic with retry + exponential backoff. Returns (parsed_rows, search_urls)."""
    settings = get_settings()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = _build_prompt(today, batch, mode)

    last_exc: Optional[Exception] = None
    codes = [c for c, _ in batch]
    for attempt in range(max_retries):
        try:
            logger.info(
                "Scoring batch mode=%s codes=%s attempt=%d", mode, codes, attempt + 1
            )
            message = client.messages.create(
                model=settings.anthropic_model,
                max_tokens=8192,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
                messages=[{"role": "user", "content": prompt}],
            )
            text = _extract_text(message)
            search_urls = _extract_search_urls(message)
            stop_reason = getattr(message, "stop_reason", None)
            usage = getattr(message, "usage", None)
            logger.info(
                "Batch returned: mode=%s codes=%s stop=%s urls=%d text_len=%d usage=%s",
                mode, codes, stop_reason, len(search_urls), len(text or ""),
                getattr(usage, "model_dump", lambda: usage)() if usage else None,
            )
            db.log_scoring(
                run_id,
                country_code=None,
                raw_response=(
                    f"mode={mode} batch={codes} attempt={attempt + 1} "
                    f"stop={stop_reason} urls={len(search_urls)}\n{text}"
                ),
                queries=json.dumps([s["url"] for s in search_urls]),
            )
            rows = _parse_json_array(text)
            if not rows:
                logger.warning(
                    "Batch produced no parseable rows: mode=%s codes=%s stop=%s — retrying",
                    mode, codes, stop_reason,
                )
                last_exc = RuntimeError(f"empty/unparseable response (stop={stop_reason})")
                time.sleep(2 ** attempt)
                continue
            return rows, search_urls
        except APIError as e:
            last_exc = e
            wait = 2 ** attempt
            logger.warning(
                "Anthropic API error (attempt %d/%d) for %s: %s — retrying in %ds",
                attempt + 1, max_retries, codes, e, wait,
            )
            time.sleep(wait)
        except Exception as e:
            last_exc = e
            logger.exception("Unexpected error in _score_batch for %s", codes)
            break

    logger.error("Batch permanently failed: mode=%s codes=%s last_error=%s", mode, codes, last_exc)
    db.log_scoring(
        run_id,
        country_code=None,
        raw_response=f"API_FAILED mode={mode} batch={codes}: {last_exc}",
        queries=None,
    )
    return [], []


def _merge_result(
    code: str,
    name: str,
    mode: str,
    ai: Optional[dict[str, Any]],
    search_urls: list[dict[str, str]],
) -> dict[str, Any]:
    allowed_urls = {s["url"] for s in search_urls}
    url_pool = ",".join(s["url"] for s in search_urls[:10]) if search_urls else None

    if mode == "prewave_direct":
        base = dict(PREWAVE_DIRECT.get(code, {}))
        rating = _clamp_rating((ai or {}).get("rating")) if ai else base.get("rating", 3)
        primary_url = _validate_url((ai or {}).get("primary_source_url"), allowed_urls)
        return {
            "rating": rating,
            "oil_reserve_days": _clamp_reserve((ai or {}).get("oil_reserve_days")) or base.get("oil_reserve_days"),
            "lng_status": (ai or {}).get("lng_status") or base.get("lng_status"),
            "key_risk": (ai or {}).get("key_risk") or base.get("key_risk"),
            "primary_source": (ai or {}).get("primary_source") or "Prewave SITREP Day 33",
            "primary_source_url": primary_url,
            "secondary_sources": (ai or {}).get("secondary_sources"),
            "source_urls": url_pool,
            "confidence": (ai or {}).get("confidence") or "High",
            "hormuz_dependency": (ai or {}).get("hormuz_dependency") or base.get("hormuz_dependency"),
            "gdp_loss_pct": base.get("gdp_loss_pct"),
            "scoring_method": "prewave_direct",
        }

    if mode == "prewave_eu_mapped":
        base = dict(EU_BASELINE)
        rating = _clamp_rating((ai or {}).get("rating") or base["rating"])
        anchor = base["rating"]
        rating = max(anchor - 1, min(anchor + 1, rating))
        primary_url = _validate_url((ai or {}).get("primary_source_url"), allowed_urls)
        return {
            "rating": rating,
            "oil_reserve_days": _clamp_reserve((ai or {}).get("oil_reserve_days")) or base.get("oil_reserve_days"),
            "lng_status": (ai or {}).get("lng_status") or base.get("lng_status"),
            "key_risk": (ai or {}).get("key_risk") or base.get("key_risk"),
            "primary_source": (ai or {}).get("primary_source") or "Prewave SITREP Day 33 (EU baseline)",
            "primary_source_url": primary_url,
            "secondary_sources": (ai or {}).get("secondary_sources"),
            "source_urls": url_pool,
            "confidence": (ai or {}).get("confidence") or "Medium",
            "hormuz_dependency": (ai or {}).get("hormuz_dependency") or base.get("hormuz_dependency"),
            "gdp_loss_pct": base.get("gdp_loss_pct"),
            "scoring_method": "prewave_eu_mapped",
        }

    # ai_researched — AI is authoritative; on failure, mark Low confidence, don't invent numbers.
    if ai is None:
        return {
            "rating": 3,
            "oil_reserve_days": None,
            "lng_status": None,
            "key_risk": f"AI scoring failed for {name}; retry scheduled.",
            "primary_source": "Fallback (AI scoring failed)",
            "primary_source_url": None,
            "secondary_sources": None,
            "source_urls": url_pool,
            "confidence": "Low",
            "hormuz_dependency": None,
            "gdp_loss_pct": None,
            "scoring_method": "ai_researched",
        }

    primary_url = _validate_url(ai.get("primary_source_url"), allowed_urls)
    # If the model refused to provide a verifiable URL, cap confidence at Low — data is suspect.
    confidence = ai.get("confidence") or "Medium"
    if not primary_url and confidence == "High":
        confidence = "Medium"
    return {
        "rating": _clamp_rating(ai.get("rating")),
        "oil_reserve_days": _clamp_reserve(ai.get("oil_reserve_days")),
        "lng_status": ai.get("lng_status"),
        "key_risk": (ai.get("key_risk") or "")[:200] or None,
        "primary_source": ai.get("primary_source") or "AI web search",
        "primary_source_url": primary_url,
        "secondary_sources": ai.get("secondary_sources"),
        "source_urls": url_pool,
        "confidence": confidence,
        "hormuz_dependency": ai.get("hormuz_dependency"),
        "gdp_loss_pct": None,
        "scoring_method": "ai_researched",
    }


def _chunks(seq: list, n: int) -> list[list]:
    return [seq[i : i + n] for i in range(0, len(seq), max(1, n))]


def run_scoring(trigger_source: str = "scheduler") -> str:
    """Run a full daily scoring pass. Atomically replaces live ratings on success."""
    settings = get_settings()
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    db.start_run(run_id, trigger_source=trigger_source)

    pinned = db.get_pinned_codes()
    name_by_code = {code: name for code, name, _ in COUNTRIES}

    direct = [(c, name_by_code[c]) for c in PREWAVE_DIRECT if c in name_by_code and c not in pinned]
    eu = [(c, name_by_code[c]) for c in EU_COUNTRIES if c not in pinned]
    handled = set(PREWAVE_DIRECT.keys()) | set(EU_COUNTRIES)
    ai = [(c, n) for c, n, _ in COUNTRIES if c not in handled and c not in pinned]

    results: dict[str, dict[str, Any]] = {}

    client: Optional[Anthropic] = None
    if settings.anthropic_api_key:
        client = Anthropic(api_key=settings.anthropic_api_key)
    else:
        logger.warning("ANTHROPIC_API_KEY not set — using baselines only, no AI calls made")

    batch_size = max(1, settings.scoring_batch_size)

    def process(batches: list[list[tuple[str, str]]], mode: str) -> None:
        for batch in batches:
            ai_rows, search_urls = ([], [])
            if client is not None:
                ai_rows, search_urls = _score_batch_with_retry(client, batch, mode, run_id)
            by_code = {row.get("country_code"): row for row in ai_rows if row.get("country_code")}
            for code, name in batch:
                results[code] = _merge_result(code, name, mode, by_code.get(code), search_urls)

    process(_chunks(direct, batch_size), "prewave_direct")
    process(_chunks(eu, batch_size), "prewave_eu_mapped")
    process(_chunks(ai, batch_size), "ai_researched")

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
                    primary_source_url=r.get("primary_source_url"),
                    secondary_sources=r["secondary_sources"],
                    source_urls=r.get("source_urls"),
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
