from typing import Literal, Optional

from pydantic import BaseModel, Field


class RatingRecord(BaseModel):
    country_code: str
    country_name: str
    region: Optional[str] = None
    energy_risk_rating: int = Field(ge=1, le=5)
    status_label: str
    oil_reserve_days: Optional[int] = None
    lng_status: Optional[str] = None
    key_risk: Optional[str] = None
    primary_source: Optional[str] = None
    primary_source_url: Optional[str] = None
    secondary_sources: Optional[str] = None
    source_urls: Optional[list[str]] = None
    confidence: Optional[str] = None
    hormuz_dependency: Optional[str] = None
    gdp_loss_pct: Optional[float] = None
    scoring_method: str
    is_pinned: bool = False
    scored_at: str


class Metadata(BaseModel):
    generated_at: str
    methodology: str
    sources: list[str]
    country_count: int
    next_refresh: Optional[str] = None
    last_run_status: Optional[str] = None


class RatingsResponse(BaseModel):
    metadata: Metadata
    ratings: list[RatingRecord]


class HealthResponse(BaseModel):
    status: str
    last_refresh: Optional[str] = None
    last_run_status: Optional[str] = None
    live_ratings_count: int
    total_countries: int
    next_refresh: Optional[str] = None


class OverrideRequest(BaseModel):
    country_code: str
    rating: int = Field(ge=1, le=5)
    key_risk: Optional[str] = None
    oil_reserve_days: Optional[int] = None
    lng_status: Optional[str] = None
    confidence: Optional[Literal["High", "Medium", "Low"]] = "High"
    hormuz_dependency: Optional[str] = None
    primary_source: Optional[str] = "Manual Override"
    pin: bool = False


class OverrideResponse(BaseModel):
    ok: bool
    country_code: str
    rating: int
    pinned: bool
    message: str


class TriggerResponse(BaseModel):
    ok: bool
    run_id: str
    message: str


class UnpinRequest(BaseModel):
    country_code: str


class LogEntry(BaseModel):
    id: int
    run_id: str
    country_code: Optional[str] = None
    raw_ai_response: Optional[str] = None
    search_queries_used: Optional[str] = None
    created_at: str
    run_status: Optional[str] = None
    run_started_at: Optional[str] = None
    run_finished_at: Optional[str] = None
    trigger_source: Optional[str] = None


class AIScoreResult(BaseModel):
    country_code: str
    rating: int = Field(ge=1, le=5)
    oil_reserve_days: Optional[int] = None
    lng_status: Optional[str] = None
    key_risk: Optional[str] = None
    primary_source: Optional[str] = None
    secondary_sources: Optional[str] = None
    confidence: Optional[Literal["High", "Medium", "Low"]] = "Medium"
    hormuz_dependency: Optional[str] = None
