"""AI scoring engine with a challenger workflow.

Phase 2 behavior:
- Most weekly runs challenge the current live rating instead of reassessing from scratch.
- The model must return one of three verdicts: no_change, insufficient_evidence, change.
- Only fresh, source-qualified evidence can verify or move a rating.
- Periodic full reassessments still happen on a slower cadence to catch slow drift.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from anthropic import Anthropic, APIError, RateLimitError

from . import database as db
from .config import get_settings

logger = logging.getLogger(__name__)

INTER_CALL_SLEEP_SEC = 60.0
WEB_SEARCH_MAX_USES = 1
CHALLENGER_LOOKBACK_DAYS = 14
PASS_TWO_LOOKBACK_DAYS = 30

ALLOWED_CHALLENGER_VERDICTS = {"no_change", "insufficient_evidence", "change"}
ACCEPTED_SOURCE_TIERS = {
    "tier1_authority",
    "government_regulator",
    "country_whitelist",
    "tier2_global",
}
STRONG_DELTA_TIERS = {"tier1_authority", "government_regulator"}
HIGH_CONFIDENCE_TIERS = {"tier1_authority", "government_regulator", "country_whitelist"}

TIER1_AUTHORITY_DOMAINS = {
    "iea.org",
    "eia.gov",
    "energy.gov",
    "opec.org",
    "ec.europa.eu",
    "europa.eu",
}
STRUCTURAL_AUTHORITY_DOMAINS = {
    "iea.org",
    "eia.gov",
    "bp.com",
    "energyinst.org",
}
TIER2_GLOBAL_DOMAINS = {
    "reuters.com",
    "bloomberg.com",
    "ft.com",
    "wsj.com",
    "apnews.com",
    "nikkei.com",
}

CHALLENGER_RULES = """
CRITICAL — CHALLENGER RULES:
1. Your job is NOT to score from scratch. Your job is to challenge the CURRENT live rating.
2. Search for fresh evidence in the requested lookback window that either confirms or contradicts the current rating.
3. If you cannot find fresh, country-specific evidence with a qualifying URL, verdict MUST be "insufficient_evidence".
4. Do not invent source dates, reserve-day figures, emergency measures, or force majeure declarations.
5. Respond only with valid JSON matching the requested schema.
"""

FULL_REASSESSMENT_RULES = """
CRITICAL — FULL REASSESSMENT RULES:
1. Search the web before scoring. Do not rely on training data.
2. Use fresh, country-specific evidence and cite a real URL from the search results.
3. If you cannot verify a field, set it to null instead of estimating.
4. Respond only with valid JSON matching the requested schema.
"""


def _label(rating: int) -> str:
    return db.STATUS_LABELS.get(rating, "Stressed")


def _normalize_domain(value: str) -> str:
    return value.strip().lower().removeprefix("www.")


def _domain_from_url(url: Optional[str]) -> Optional[str]:
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    domain = parsed.netloc or ""
    return _normalize_domain(domain) if domain else None


def _domain_matches(domain: Optional[str], candidate: str) -> bool:
    if not domain:
        return False
    norm = _normalize_domain(candidate)
    return domain == norm or domain.endswith(f".{norm}")


def _looks_government_domain(domain: Optional[str]) -> bool:
    if not domain:
        return False
    patterns = (
        ".gov",
        ".gouv",
        ".go.",
        ".gc.ca",
        ".gov.uk",
        ".gob.",
        ".gv.",
    )
    if domain.startswith("gov.") or domain.startswith("government."):
        return True
    return any(pattern in domain for pattern in patterns)


def _split_csv(raw: Any) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _parse_timestamp(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_source_date(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.lower() == "null":
        return None
    fmts = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z")
    for fmt in fmts:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_text(value: Any, limit: int = 240) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    return text[:limit]


def _clamp_rating(rating: Any) -> int:
    try:
        return max(1, min(5, int(rating)))
    except (TypeError, ValueError):
        return 3


def _clamp_optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _normalize_hormuz(value: Any, fallback: Optional[str] = None) -> Optional[str]:
    if value is None:
        return fallback
    text = str(value).strip().upper()
    if text in {"HIGH", "CRIT", "MOD", "LOW", "NONE"}:
        return text
    return fallback


def _classify_source_tier(url: Optional[str], whitelist: list[str]) -> Optional[str]:
    domain = _domain_from_url(url)
    if not domain:
        return None
    if any(_domain_matches(domain, candidate) for candidate in TIER1_AUTHORITY_DOMAINS):
        return "tier1_authority"
    if _looks_government_domain(domain):
        return "government_regulator"
    if any(_domain_matches(domain, candidate) for candidate in whitelist):
        return "country_whitelist"
    if any(_domain_matches(domain, candidate) for candidate in TIER2_GLOBAL_DOMAINS):
        return "tier2_global"
    if any(_domain_matches(domain, candidate) for candidate in STRUCTURAL_AUTHORITY_DOMAINS):
        return "structural_authority"
    return "untrusted"


def _is_fresh(source_date: Optional[datetime], now: datetime, max_age_days: int) -> bool:
    if source_date is None:
        return False
    return source_date <= now and (now - source_date) <= timedelta(days=max_age_days)


def _validate_url(url: Optional[str], allowed: set[str]) -> Optional[str]:
    if not url or not isinstance(url, str):
        return None
    candidate = url.strip()
    return candidate if candidate in allowed else None


def _confidence_rank(confidence: Optional[str]) -> int:
    order = {"High": 4, "Medium": 3, "Low": 2, "Stale": 1, "Unverified": 0}
    return order.get(confidence or "", 2)


def _min_confidence(a: Optional[str], b: Optional[str]) -> str:
    options = [c for c in (a, b) if c]
    if not options:
        return "Low"
    return min(options, key=_confidence_rank)


def _confidence_from_source_tier(source_tier: Optional[str]) -> str:
    if source_tier in HIGH_CONFIDENCE_TIERS:
        return "High"
    if source_tier == "tier2_global":
        return "Medium"
    if source_tier == "structural_authority":
        return "Medium"
    return "Low"


def _decayed_confidence(current: dict[str, Any], now: datetime) -> str:
    last_verified = _parse_timestamp(current.get("last_verified_at"))
    current_conf = current.get("confidence") or "Low"
    if last_verified is None:
        return "Unverified"
    age_days = (now - last_verified).days
    if age_days >= 21:
        return "Stale"
    if age_days >= 14:
        return "Stale"
    if current_conf == "High":
        return "Medium"
    if current_conf == "Medium":
        return "Low"
    return current_conf if current_conf in {"Low", "Stale", "Unverified"} else "Low"


def _next_full_reassessment_due(current: dict[str, Any], rating: int, confidence: str, now: datetime) -> str:
    weeks = 8
    if rating >= 4 or confidence in {"Low", "Stale", "Unverified"} or current.get("hormuz_dependency") in {"HIGH", "CRIT"}:
        weeks = 4
    return _iso(now + timedelta(weeks=weeks))


def _parse_json_object(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    else:
        start = cleaned.find("{")
        if start != -1:
            depth = 0
            end = -1
            for idx in range(start, len(cleaned)):
                char = cleaned[idx]
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        end = idx
                        break
            if end != -1:
                cleaned = cleaned[start : end + 1]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _extract_text(message: Any) -> str:
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "\n".join(parts).strip()


def _extract_search_urls(message: Any) -> list[dict[str, str]]:
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
            for citation in getattr(block, "citations", []) or []:
                if isinstance(citation, dict):
                    url = citation.get("url")
                    title = citation.get("title") or ""
                else:
                    url = getattr(citation, "url", None)
                    title = getattr(citation, "title", "") or ""
                if url:
                    sources.append({"url": url, "title": title})

    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for source in sources:
        url = source["url"]
        if url and url not in seen:
            seen.add(url)
            unique.append(source)
    return unique


def _parse_retry_after(exc: Exception) -> Optional[float]:
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    headers = getattr(resp, "headers", None) or {}
    for key in ("retry-after", "Retry-After", "anthropic-ratelimit-input-tokens-reset"):
        val = headers.get(key) if hasattr(headers, "get") else None
        if val:
            try:
                return max(0.0, float(val))
            except (TypeError, ValueError):
                return None
    return None


def _base_result(current: dict[str, Any]) -> dict[str, Any]:
    return {
        "rating": int(current.get("rating") or 3),
        "oil_reserve_days": current.get("oil_reserve_days"),
        "lng_status": current.get("lng_status"),
        "key_risk": current.get("key_risk"),
        "primary_source": current.get("primary_source"),
        "primary_source_url": current.get("primary_source_url"),
        "secondary_sources": current.get("secondary_sources"),
        "source_urls": current.get("source_urls"),
        "confidence": current.get("confidence") or "Low",
        "hormuz_dependency": current.get("hormuz_dependency"),
        "gdp_loss_pct": current.get("gdp_loss_pct"),
        "scoring_method": current.get("scoring_method") or "challenger_hold",
        "original_baseline_rating": current.get("original_baseline_rating") or current.get("rating") or 3,
        "original_baseline_source": current.get("original_baseline_source") or current.get("primary_source"),
        "last_verified_at": current.get("last_verified_at"),
        "last_checked_at": current.get("last_checked_at"),
        "last_verdict": current.get("last_verdict"),
        "verifications_count": int(current.get("verifications_count") or 0),
        "next_full_reassessment_due": current.get("next_full_reassessment_due"),
        "scenario_id": current.get("scenario_id"),
        "primary_source_tier": current.get("primary_source_tier"),
        "change_record": None,
        "evidence_records": [],
        "log": {},
    }


def _source_pool(search_urls: list[dict[str, str]]) -> Optional[str]:
    urls = [item["url"] for item in search_urls[:10] if item.get("url")]
    return ",".join(urls) if urls else None


def _search_title_map(search_urls: list[dict[str, str]]) -> dict[str, str]:
    return {item["url"]: item.get("title") or "" for item in search_urls if item.get("url")}


def _build_challenge_prompt(
    *,
    today_iso: str,
    country: dict[str, Any],
    current: dict[str, Any],
    scenario_context: Optional[str],
    pass_name: str,
    lookback_days: int,
) -> str:
    whitelist = _split_csv(country.get("source_whitelist"))
    if whitelist:
        whitelist_line = ", ".join(whitelist)
    else:
        whitelist_line = "No explicit whitelist on file — prioritize official government/regulator sources and country-specific reporting."

    pass_instructions = {
        "pass1_domestic": (
            "Pass 1: prioritize domestic business press, local government notices, and country-specific reporting. "
            "A qualifying source should name the country explicitly."
        ),
        "pass2_government": (
            "Pass 2: pass 1 was insufficient. Prioritize the country's energy ministry, regulator, reserve agency, "
            "or emergency measures sources. Use ministry/regulator/government domains whenever possible."
        ),
    }

    return f"""You are reviewing whether the current energy-risk rating for {country['country_name']} ({country['country_code']})
should change as of {today_iso}.

SCENARIO CONTEXT:
{scenario_context or "No active scenario context."}

CURRENT LIVE RATING:
- rating: {current['rating']} ({_label(int(current['rating'] or 3))})
- key risk: {current.get('key_risk') or "none recorded"}
- oil reserve days: {current.get('oil_reserve_days')}
- LNG status: {current.get('lng_status')}
- Hormuz dependency: {current.get('hormuz_dependency')}
- original baseline: {current.get('original_baseline_rating')} from {current.get('original_baseline_source') or 'unknown'}
- last verified at: {current.get('last_verified_at') or 'never'}
- last checked at: {current.get('last_checked_at') or 'never'}

SOURCE POLICY:
- Country whitelist: {whitelist_line}
- Accepted primary URLs must be from the country whitelist OR a recognized government/regulator source OR a major global outlet.
- Only use evidence published in the last {lookback_days} days unless the pass explicitly forces official context and no fresher source exists.

{pass_instructions[pass_name]}
{CHALLENGER_RULES}

Look for contradictions or confirmations such as:
- emergency measures added or lifted
- strategic reserve changes larger than 20%
- force majeure declared or resolved
- supply deals materially improving the situation
- government statements indicating clear deterioration or stabilization

Respond ONLY with JSON:
{{
  "country_code": "{country['country_code']}",
  "verdict": "no_change | insufficient_evidence | change",
  "new_rating": 1-5 or null,
  "delta_reason": "short explanation",
  "key_risk": "updated key risk or null",
  "oil_reserve_days": integer or null,
  "lng_status": "Exhausted/Depleted/Critical/Minimal/Low/Stressed/Partial/Managed/Buffered or null",
  "hormuz_dependency": "HIGH/CRIT/MOD/LOW/NONE or null",
  "primary_source": "title from search result or null",
  "primary_source_url": "https://... or null",
  "primary_source_date": "YYYY-MM-DD or null",
  "secondary_source": "title or null",
  "secondary_source_url": "https://... or null",
  "secondary_source_date": "YYYY-MM-DD or null",
  "note": "one sentence note"
}}
"""


def _build_full_reassessment_prompt(
    *,
    today_iso: str,
    country: dict[str, Any],
    current: dict[str, Any],
    scenario_context: Optional[str],
) -> str:
    whitelist = _split_csv(country.get("source_whitelist"))
    whitelist_line = ", ".join(whitelist) if whitelist else "No explicit whitelist on file."
    return f"""You are performing a full energy-risk reassessment for {country['country_name']} ({country['country_code']})
as of {today_iso}.

SCENARIO CONTEXT:
{scenario_context or "No active scenario context."}

CURRENT LIVE RATING (for reference only):
- current rating: {current['rating']} ({_label(int(current['rating'] or 3))})
- last key risk: {current.get('key_risk') or "none recorded"}
- original baseline: {current.get('original_baseline_rating')} from {current.get('original_baseline_source') or 'unknown'}

SOURCE POLICY:
- Country whitelist: {whitelist_line}
- Prefer fresh country-specific reporting, government/regulator sources, and major market reporting.
- Use the latest evidence you can find. Set unverifiable fields to null instead of estimating.

{FULL_REASSESSMENT_RULES}

Assess:
- oil/fuel reserve levels
- LNG/gas supply status
- emergency measures
- Hormuz dependency
- current force majeure or acute supply disruptions
- government energy-security statements

Respond ONLY with JSON:
{{
  "country_code": "{country['country_code']}",
  "rating": 1-5,
  "oil_reserve_days": integer or null,
  "lng_status": "Exhausted/Depleted/Critical/Minimal/Low/Stressed/Partial/Managed/Buffered or null",
  "key_risk": "max 160 chars",
  "primary_source": "title from search result or null",
  "primary_source_url": "https://... or null",
  "primary_source_date": "YYYY-MM-DD or null",
  "secondary_source": "title or null",
  "secondary_source_url": "https://... or null",
  "secondary_source_date": "YYYY-MM-DD or null",
  "confidence": "High/Medium/Low",
  "hormuz_dependency": "HIGH/CRIT/MOD/LOW/NONE or null",
  "assessment_note": "one sentence note"
}}
"""


def _pace_calls(last_api_call_at: list[float]) -> None:
    elapsed = time.monotonic() - last_api_call_at[0]
    wait = INTER_CALL_SLEEP_SEC - elapsed
    if last_api_call_at[0] > 0 and wait > 0:
        logger.info("Pacing: sleeping %.1fs before next AI call", wait)
        time.sleep(wait)


def _call_json_with_retry(
    *,
    client: Anthropic,
    prompt: str,
    run_id: str,
    country_code: str,
    pass_name: str,
    max_tokens: int,
    parser: Callable[[str], Optional[dict[str, Any]]],
    last_api_call_at: list[float],
    max_retries: int = 3,
) -> tuple[Optional[dict[str, Any]], list[dict[str, str]], str]:
    settings = get_settings()
    last_exc: Optional[Exception] = None
    last_text = ""
    last_urls: list[dict[str, str]] = []

    for attempt in range(max_retries):
        try:
            _pace_calls(last_api_call_at)
            logger.info("AI call country=%s pass=%s attempt=%d", country_code, pass_name, attempt + 1)
            message = client.messages.create(
                model=settings.anthropic_model,
                max_tokens=max_tokens,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": WEB_SEARCH_MAX_USES}],
                messages=[{"role": "user", "content": prompt}],
            )
            last_api_call_at[0] = time.monotonic()
            text = _extract_text(message)
            search_urls = _extract_search_urls(message)
            parsed = parser(text)
            last_text = text
            last_urls = search_urls
            if parsed is not None:
                return parsed, search_urls, text
            last_exc = RuntimeError("empty/unparseable model response")
            logger.warning("Parse failed for country=%s pass=%s attempt=%d", country_code, pass_name, attempt + 1)
            time.sleep(2 ** attempt)
        except RateLimitError as exc:
            last_exc = exc
            wait = min((_parse_retry_after(exc) or 60.0) + 2.0, 120.0)
            logger.warning(
                "Rate limit for country=%s pass=%s attempt=%d — sleeping %.1fs",
                country_code,
                pass_name,
                attempt + 1,
                wait,
            )
            time.sleep(wait)
        except APIError as exc:
            last_exc = exc
            wait = 2 ** attempt
            logger.warning(
                "Anthropic API error for country=%s pass=%s attempt=%d: %s — retrying in %ds",
                country_code,
                pass_name,
                attempt + 1,
                exc,
                wait,
            )
            time.sleep(wait)
        except Exception as exc:
            last_exc = exc
            logger.exception("Unexpected error country=%s pass=%s", country_code, pass_name)
            break

    db.log_scoring(
        run_id,
        country_code=country_code,
        raw_response=f"API_FAILED pass={pass_name}: {last_exc}\n{last_text}",
        queries=json.dumps([item["url"] for item in last_urls]),
        verdict="insufficient_evidence",
        pass_name=pass_name,
    )
    return None, last_urls, last_text


def _max_allowed_delta(primary_tier: Optional[str], secondary_tier: Optional[str]) -> int:
    if primary_tier in STRONG_DELTA_TIERS:
        return 2
    if primary_tier == "tier2_global" and secondary_tier == "tier2_global":
        return 2
    return 1


def _needs_full_reassessment(current: dict[str, Any], now: datetime) -> bool:
    if current.get("scoring_method") == "placeholder":
        return True
    if current.get("confidence") == "Unverified":
        return True
    due = _parse_timestamp(current.get("next_full_reassessment_due"))
    return due is None or due <= now


def _log_country_result(
    *,
    run_id: str,
    country_code: str,
    raw_text: str,
    search_urls: list[dict[str, str]],
    verdict: str,
    pass_name: str,
    source_url: Optional[str],
    source_tier: Optional[str],
) -> None:
    db.log_scoring(
        run_id,
        country_code=country_code,
        raw_response=raw_text or "(no raw response)",
        queries=json.dumps([item["url"] for item in search_urls]),
        verdict=verdict,
        pass_name=pass_name,
        source_url=source_url,
        source_tier=source_tier,
    )


def _build_hold_result(
    *,
    current: dict[str, Any],
    verdict: str,
    scenario_id: Optional[str],
    now: datetime,
    pass_name: str,
    note: Optional[str] = None,
) -> dict[str, Any]:
    result = _base_result(current)
    result["last_checked_at"] = _iso(now)
    result["last_verdict"] = verdict
    result["scenario_id"] = scenario_id
    result["confidence"] = _decayed_confidence(current, now)
    result["scoring_method"] = "challenger_hold" if verdict == "insufficient_evidence" else current.get("scoring_method")
    if note:
        result["log"]["note"] = note
    if _needs_full_reassessment(current, now):
        result["next_full_reassessment_due"] = current.get("next_full_reassessment_due") or _iso(now)
    return result


def _log_hold_without_ai(run_id: str, country_code: str) -> None:
    db.log_scoring(
        run_id,
        country_code=country_code,
        raw_response="No Anthropic client configured; holding current live rating.",
        queries=None,
        verdict="insufficient_evidence",
        pass_name="no_client",
    )


def _maybe_add_evidence(
    result: dict[str, Any],
    *,
    source_url: Optional[str],
    source_date: Optional[datetime],
    source_tier: Optional[str],
    run_id: str,
    country_code: str,
    raw: dict[str, Any],
) -> None:
    if not source_url:
        return
    date_text = source_date.strftime("%Y-%m-%d") if source_date else None
    evidence_fields = {
        "rating": str(result["rating"]),
        "key_risk": _clean_text(raw.get("key_risk") or raw.get("delta_reason")),
    }
    if result.get("oil_reserve_days") is not None:
        evidence_fields["oil_reserve_days"] = str(result["oil_reserve_days"])
    if result.get("lng_status"):
        evidence_fields["lng_status"] = str(result["lng_status"])
    if result.get("hormuz_dependency"):
        evidence_fields["hormuz_dependency"] = str(result["hormuz_dependency"])
    result["evidence_records"] = [
        {
            "country_code": country_code,
            "field": field,
            "value": value,
            "source_url": source_url,
            "source_date": date_text,
            "source_tier": source_tier,
            "run_id": run_id,
        }
        for field, value in evidence_fields.items()
        if value
    ]


def _apply_challenger_response(
    *,
    country: dict[str, Any],
    current: dict[str, Any],
    raw: Optional[dict[str, Any]],
    search_urls: list[dict[str, str]],
    raw_text: str,
    run_id: str,
    pass_name: str,
    now: datetime,
    scenario_id: Optional[str],
) -> dict[str, Any]:
    if raw is None:
        result = _build_hold_result(
            current=current,
            verdict="insufficient_evidence",
            scenario_id=scenario_id,
            now=now,
            pass_name=pass_name,
            note="Model response was empty or unparseable.",
        )
        _log_country_result(
            run_id=run_id,
            country_code=country["country_code"],
            raw_text=raw_text,
            search_urls=search_urls,
            verdict="insufficient_evidence",
            pass_name=pass_name,
            source_url=None,
            source_tier=None,
        )
        return result

    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in ALLOWED_CHALLENGER_VERDICTS:
        verdict = "insufficient_evidence"

    allowed_urls = {item["url"] for item in search_urls}
    titles_by_url = _search_title_map(search_urls)
    whitelist = _split_csv(country.get("source_whitelist"))

    primary_url = _validate_url(_clean_text(raw.get("primary_source_url"), limit=500), allowed_urls)
    primary_tier = _classify_source_tier(primary_url, whitelist)
    primary_date = _parse_source_date(raw.get("primary_source_date"))

    secondary_url = _validate_url(_clean_text(raw.get("secondary_source_url"), limit=500), allowed_urls)
    secondary_tier = _classify_source_tier(secondary_url, whitelist)
    secondary_date = _parse_source_date(raw.get("secondary_source_date"))

    source_ok = primary_url is not None and primary_tier in ACCEPTED_SOURCE_TIERS and _is_fresh(primary_date, now, CHALLENGER_LOOKBACK_DAYS)
    fresh_secondary = secondary_url is not None and secondary_tier in ACCEPTED_SOURCE_TIERS and _is_fresh(secondary_date, now, CHALLENGER_LOOKBACK_DAYS)

    if verdict == "insufficient_evidence" or not source_ok:
        result = _build_hold_result(
            current=current,
            verdict="insufficient_evidence",
            scenario_id=scenario_id,
            now=now,
            pass_name=pass_name,
        )
        _log_country_result(
            run_id=run_id,
            country_code=country["country_code"],
            raw_text=raw_text,
            search_urls=search_urls,
            verdict="insufficient_evidence",
            pass_name=pass_name,
            source_url=primary_url,
            source_tier=primary_tier,
        )
        return result

    result = _base_result(current)
    result["primary_source"] = _clean_text(raw.get("primary_source")) or titles_by_url.get(primary_url) or current.get("primary_source")
    result["primary_source_url"] = primary_url
    result["secondary_sources"] = _clean_text(raw.get("secondary_source")) or current.get("secondary_sources")
    result["source_urls"] = _source_pool(search_urls)
    result["primary_source_tier"] = primary_tier
    result["last_checked_at"] = _iso(now)
    result["scenario_id"] = scenario_id
    result["key_risk"] = _clean_text(raw.get("key_risk") or raw.get("delta_reason"), limit=200) or current.get("key_risk")
    result["oil_reserve_days"] = _clamp_optional_int(raw.get("oil_reserve_days")) if raw.get("oil_reserve_days") is not None else current.get("oil_reserve_days")
    result["lng_status"] = _clean_text(raw.get("lng_status")) or current.get("lng_status")
    result["hormuz_dependency"] = _normalize_hormuz(raw.get("hormuz_dependency"), current.get("hormuz_dependency"))
    result["confidence"] = _confidence_from_source_tier(primary_tier)
    result["next_full_reassessment_due"] = _next_full_reassessment_due(current, result["rating"], result["confidence"], now)

    if verdict == "no_change":
        result["last_verified_at"] = _iso(now)
        result["last_verdict"] = "no_change"
        result["verifications_count"] = int(current.get("verifications_count") or 0) + 1
        result["scoring_method"] = "challenger_no_change"
        _maybe_add_evidence(
            result,
            source_url=primary_url,
            source_date=primary_date,
            source_tier=primary_tier,
            run_id=run_id,
            country_code=country["country_code"],
            raw=raw,
        )
        _log_country_result(
            run_id=run_id,
            country_code=country["country_code"],
            raw_text=raw_text,
            search_urls=search_urls,
            verdict="no_change",
            pass_name=pass_name,
            source_url=primary_url,
            source_tier=primary_tier,
        )
        return result

    proposed_rating = raw.get("new_rating")
    if proposed_rating is None:
        result = _build_hold_result(
            current=current,
            verdict="insufficient_evidence",
            scenario_id=scenario_id,
            now=now,
            pass_name=pass_name,
        )
        _log_country_result(
            run_id=run_id,
            country_code=country["country_code"],
            raw_text=raw_text,
            search_urls=search_urls,
            verdict="insufficient_evidence",
            pass_name=pass_name,
            source_url=primary_url,
            source_tier=primary_tier,
        )
        return result

    current_rating = int(current.get("rating") or 3)
    requested_rating = _clamp_rating(proposed_rating)
    max_delta = _max_allowed_delta(primary_tier, secondary_tier if fresh_secondary else None)
    delta = requested_rating - current_rating
    allowed_delta = max(-max_delta, min(max_delta, delta))
    final_rating = max(1, min(5, current_rating + allowed_delta))

    if final_rating == current_rating:
        result["last_verified_at"] = _iso(now)
        result["last_verdict"] = "no_change"
        result["verifications_count"] = int(current.get("verifications_count") or 0) + 1
        result["scoring_method"] = "challenger_no_change"
        _maybe_add_evidence(
            result,
            source_url=primary_url,
            source_date=primary_date,
            source_tier=primary_tier,
            run_id=run_id,
            country_code=country["country_code"],
            raw=raw,
        )
        _log_country_result(
            run_id=run_id,
            country_code=country["country_code"],
            raw_text=raw_text,
            search_urls=search_urls,
            verdict="no_change",
            pass_name=pass_name,
            source_url=primary_url,
            source_tier=primary_tier,
        )
        return result

    result["rating"] = final_rating
    result["last_verified_at"] = _iso(now)
    result["last_verdict"] = "change"
    result["verifications_count"] = int(current.get("verifications_count") or 0) + 1
    result["scoring_method"] = "challenger_change"
    result["next_full_reassessment_due"] = _next_full_reassessment_due(current, final_rating, result["confidence"], now)
    result["change_record"] = {
        "country_code": country["country_code"],
        "from_rating": current_rating,
        "to_rating": final_rating,
        "reason": _clean_text(raw.get("delta_reason") or raw.get("note"), limit=240),
        "primary_source_url": primary_url,
        "primary_source_tier": primary_tier,
        "max_allowed_delta": max_delta,
        "run_id": run_id,
    }
    _maybe_add_evidence(
        result,
        source_url=primary_url,
        source_date=primary_date,
        source_tier=primary_tier,
        run_id=run_id,
        country_code=country["country_code"],
        raw=raw,
    )
    _log_country_result(
        run_id=run_id,
        country_code=country["country_code"],
        raw_text=raw_text,
        search_urls=search_urls,
        verdict="change",
        pass_name=pass_name,
        source_url=primary_url,
        source_tier=primary_tier,
    )
    return result


def _apply_full_reassessment(
    *,
    country: dict[str, Any],
    current: dict[str, Any],
    raw: Optional[dict[str, Any]],
    search_urls: list[dict[str, str]],
    raw_text: str,
    run_id: str,
    now: datetime,
    scenario_id: Optional[str],
) -> dict[str, Any]:
    if raw is None:
        result = _build_hold_result(
            current=current,
            verdict="insufficient_evidence",
            scenario_id=scenario_id,
            now=now,
            pass_name="full_reassessment",
            note="Full reassessment response was empty or unparseable.",
        )
        _log_country_result(
            run_id=run_id,
            country_code=country["country_code"],
            raw_text=raw_text,
            search_urls=search_urls,
            verdict="insufficient_evidence",
            pass_name="full_reassessment",
            source_url=None,
            source_tier=None,
        )
        return result

    allowed_urls = {item["url"] for item in search_urls}
    titles_by_url = _search_title_map(search_urls)
    whitelist = _split_csv(country.get("source_whitelist"))

    primary_url = _validate_url(_clean_text(raw.get("primary_source_url"), limit=500), allowed_urls)
    primary_tier = _classify_source_tier(primary_url, whitelist)
    primary_date = _parse_source_date(raw.get("primary_source_date"))
    if primary_url is None or primary_tier not in ACCEPTED_SOURCE_TIERS or not _is_fresh(primary_date, now, PASS_TWO_LOOKBACK_DAYS):
        result = _build_hold_result(
            current=current,
            verdict="insufficient_evidence",
            scenario_id=scenario_id,
            now=now,
            pass_name="full_reassessment",
        )
        _log_country_result(
            run_id=run_id,
            country_code=country["country_code"],
            raw_text=raw_text,
            search_urls=search_urls,
            verdict="insufficient_evidence",
            pass_name="full_reassessment",
            source_url=primary_url,
            source_tier=primary_tier,
        )
        return result

    result = _base_result(current)
    old_rating = int(current.get("rating") or 3)
    new_rating = _clamp_rating(raw.get("rating"))
    model_confidence = _clean_text(raw.get("confidence"), limit=16)
    tier_confidence = _confidence_from_source_tier(primary_tier)

    result["rating"] = new_rating
    result["oil_reserve_days"] = _clamp_optional_int(raw.get("oil_reserve_days"))
    result["lng_status"] = _clean_text(raw.get("lng_status"))
    result["key_risk"] = _clean_text(raw.get("key_risk"), limit=200)
    result["primary_source"] = _clean_text(raw.get("primary_source")) or titles_by_url.get(primary_url) or current.get("primary_source")
    result["primary_source_url"] = primary_url
    result["secondary_sources"] = _clean_text(raw.get("secondary_source")) or current.get("secondary_sources")
    result["source_urls"] = _source_pool(search_urls)
    result["confidence"] = _min_confidence(model_confidence, tier_confidence)
    result["hormuz_dependency"] = _normalize_hormuz(raw.get("hormuz_dependency"), current.get("hormuz_dependency"))
    result["scoring_method"] = "full_reassessment"
    result["last_verified_at"] = _iso(now)
    result["last_checked_at"] = _iso(now)
    result["last_verdict"] = "full_reassessment"
    result["verifications_count"] = int(current.get("verifications_count") or 0) + 1
    result["next_full_reassessment_due"] = _next_full_reassessment_due(current, new_rating, result["confidence"], now)
    result["scenario_id"] = scenario_id
    result["primary_source_tier"] = primary_tier

    if new_rating != old_rating:
        result["change_record"] = {
            "country_code": country["country_code"],
            "from_rating": old_rating,
            "to_rating": new_rating,
            "reason": _clean_text(raw.get("assessment_note") or raw.get("key_risk"), limit=240),
            "primary_source_url": primary_url,
            "primary_source_tier": primary_tier,
            "max_allowed_delta": 4,
            "run_id": run_id,
        }

    _maybe_add_evidence(
        result,
        source_url=primary_url,
        source_date=primary_date,
        source_tier=primary_tier,
        run_id=run_id,
        country_code=country["country_code"],
        raw=raw,
    )
    _log_country_result(
        run_id=run_id,
        country_code=country["country_code"],
        raw_text=raw_text,
        search_urls=search_urls,
        verdict="full_reassessment",
        pass_name="full_reassessment",
        source_url=primary_url,
        source_tier=primary_tier,
    )
    return result


def _score_country(
    *,
    client: Optional[Anthropic],
    country: dict[str, Any],
    current: dict[str, Any],
    scenario_context: Optional[str],
    scenario_id: Optional[str],
    run_id: str,
    last_api_call_at: list[float],
    now: datetime,
) -> dict[str, Any]:
    if client is None:
        _log_hold_without_ai(run_id, country["country_code"])
        return _build_hold_result(
            current=current,
            verdict="insufficient_evidence",
            scenario_id=scenario_id,
            now=now,
            pass_name="no_client",
            note="ANTHROPIC_API_KEY not configured.",
        )

    today_iso = now.strftime("%Y-%m-%d")
    if _needs_full_reassessment(current, now):
        prompt = _build_full_reassessment_prompt(
            today_iso=today_iso,
            country=country,
            current=current,
            scenario_context=scenario_context,
        )
        raw, search_urls, raw_text = _call_json_with_retry(
            client=client,
            prompt=prompt,
            run_id=run_id,
            country_code=country["country_code"],
            pass_name="full_reassessment",
            max_tokens=1400,
            parser=_parse_json_object,
            last_api_call_at=last_api_call_at,
        )
        return _apply_full_reassessment(
            country=country,
            current=current,
            raw=raw,
            search_urls=search_urls,
            raw_text=raw_text,
            run_id=run_id,
            now=now,
            scenario_id=scenario_id,
        )

    prompt_one = _build_challenge_prompt(
        today_iso=today_iso,
        country=country,
        current=current,
        scenario_context=scenario_context,
        pass_name="pass1_domestic",
        lookback_days=CHALLENGER_LOOKBACK_DAYS,
    )
    raw_one, urls_one, text_one = _call_json_with_retry(
        client=client,
        prompt=prompt_one,
        run_id=run_id,
        country_code=country["country_code"],
        pass_name="pass1_domestic",
        max_tokens=1200,
        parser=_parse_json_object,
        last_api_call_at=last_api_call_at,
    )
    result_one = _apply_challenger_response(
        country=country,
        current=current,
        raw=raw_one,
        search_urls=urls_one,
        raw_text=text_one,
        run_id=run_id,
        pass_name="pass1_domestic",
        now=now,
        scenario_id=scenario_id,
    )
    if result_one["last_verdict"] != "insufficient_evidence":
        return result_one

    prompt_two = _build_challenge_prompt(
        today_iso=today_iso,
        country=country,
        current=current,
        scenario_context=scenario_context,
        pass_name="pass2_government",
        lookback_days=PASS_TWO_LOOKBACK_DAYS,
    )
    raw_two, urls_two, text_two = _call_json_with_retry(
        client=client,
        prompt=prompt_two,
        run_id=run_id,
        country_code=country["country_code"],
        pass_name="pass2_government",
        max_tokens=1200,
        parser=_parse_json_object,
        last_api_call_at=last_api_call_at,
    )
    return _apply_challenger_response(
        country=country,
        current=current,
        raw=raw_two,
        search_urls=urls_two,
        raw_text=text_two,
        run_id=run_id,
        pass_name="pass2_government",
        now=now,
        scenario_id=scenario_id,
    )


def run_scoring(trigger_source: str = "scheduler") -> str:
    """Run a challenger-based scheduled scoring pass."""
    settings = get_settings()
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    db.start_run(run_id, trigger_source=trigger_source)

    now = datetime.now(timezone.utc)
    scenario = db.get_active_scenario()
    scenario_id = scenario.get("scenario_id") if scenario else None
    scenario_context = scenario.get("context_block") if scenario else None

    countries = db.get_all_countries(active_only=True)
    current_rows = {row["country_code"]: row for row in db.get_live_ratings(active_only=False)}
    pinned = db.get_pinned_codes()

    client: Optional[Anthropic] = None
    if settings.anthropic_api_key:
        client = Anthropic(api_key=settings.anthropic_api_key)
    else:
        logger.warning("ANTHROPIC_API_KEY not set — phase 2 run will hold current ratings and mark them unchecked")

    last_api_call_at = [0.0]
    results: dict[str, dict[str, Any]] = {}

    for country in countries:
        code = country["country_code"]
        if code in pinned:
            continue
        current = current_rows.get(code)
        if current is None:
            logger.warning("No live row found for %s — skipping", code)
            continue
        results[code] = _score_country(
            client=client,
            country=country,
            current=current,
            scenario_context=scenario_context,
            scenario_id=scenario_id,
            run_id=run_id,
            last_api_call_at=last_api_call_at,
            now=now,
        )

    inserted = 0
    with db.get_conn() as conn:
        conn.execute("BEGIN")
        try:
            db.supersede_live(conn, keep_pinned=True)
            for code, result in results.items():
                db.insert_rating(
                    conn,
                    country_code=code,
                    rating=result["rating"],
                    oil_reserve_days=result.get("oil_reserve_days"),
                    lng_status=result.get("lng_status"),
                    key_risk=result.get("key_risk"),
                    primary_source=result.get("primary_source"),
                    primary_source_url=result.get("primary_source_url"),
                    secondary_sources=result.get("secondary_sources"),
                    source_urls=result.get("source_urls"),
                    confidence=result.get("confidence"),
                    hormuz_dependency=result.get("hormuz_dependency"),
                    gdp_loss_pct=result.get("gdp_loss_pct"),
                    scoring_method=result.get("scoring_method") or "challenger_hold",
                    run_id=run_id,
                    is_live=1,
                    is_pinned=0,
                    original_baseline_rating=result.get("original_baseline_rating"),
                    original_baseline_source=result.get("original_baseline_source"),
                    last_verified_at=result.get("last_verified_at"),
                    last_checked_at=result.get("last_checked_at"),
                    last_verdict=result.get("last_verdict"),
                    verifications_count=int(result.get("verifications_count") or 0),
                    next_full_reassessment_due=result.get("next_full_reassessment_due"),
                    scenario_id=result.get("scenario_id"),
                    primary_source_tier=result.get("primary_source_tier"),
                )
                change_record = result.get("change_record")
                if change_record is not None:
                    db.record_rating_change(conn, **change_record)
                for evidence_record in result.get("evidence_records", []):
                    db.record_evidence(conn, **evidence_record)
                inserted += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            db.finish_run(run_id, status="failed", countries_scored=0)
            raise

    db.finish_run(run_id, status="success", countries_scored=inserted)
    return run_id