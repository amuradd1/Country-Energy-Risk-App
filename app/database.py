import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from .config import get_settings

_lock = threading.Lock()

# Tables only — indexes are created after migrations so we don't try to index
# columns the migration is about to add.
SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS countries (
    country_code TEXT PRIMARY KEY,
    country_name TEXT NOT NULL,
    region TEXT,
    prewave_mapped_to TEXT,
    is_active INTEGER DEFAULT 1,
    priority_tier TEXT DEFAULT 'P1',
    country_group TEXT,
    source_whitelist TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS risk_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country_code TEXT NOT NULL,
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    status_label TEXT NOT NULL,
    oil_reserve_days INTEGER,
    lng_status TEXT,
    key_risk TEXT,
    primary_source TEXT,
    primary_source_url TEXT,
    secondary_sources TEXT,
    source_urls TEXT,
    confidence TEXT CHECK(confidence IN ('High', 'Medium', 'Low', 'Stale', 'Unverified')),
    hormuz_dependency TEXT,
    gdp_loss_pct REAL,
    scoring_method TEXT NOT NULL,
    is_live INTEGER DEFAULT 1,
    is_pinned INTEGER DEFAULT 0,
    scored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    run_id TEXT,
    original_baseline_rating INTEGER,
    original_baseline_source TEXT,
    last_verified_at TIMESTAMP,
    last_checked_at TIMESTAMP,
    last_verdict TEXT,
    verifications_count INTEGER DEFAULT 0,
    next_full_reassessment_due TIMESTAMP,
    scenario_id TEXT,
    primary_source_tier TEXT,
    FOREIGN KEY (country_code) REFERENCES countries(country_code)
);

CREATE TABLE IF NOT EXISTS scoring_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    country_code TEXT,
    raw_ai_response TEXT,
    search_queries_used TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    status TEXT,
    countries_scored INTEGER DEFAULT 0,
    trigger_source TEXT
);

CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    context_block TEXT NOT NULL,
    affected_commodities TEXT,
    affected_routes TEXT,
    started_at TIMESTAMP,
    superseded_at TIMESTAMP,
    is_active INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rating_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country_code TEXT NOT NULL,
    from_rating INTEGER,
    to_rating INTEGER,
    delta INTEGER,
    reason TEXT,
    primary_source_url TEXT,
    primary_source_tier TEXT,
    max_allowed_delta INTEGER,
    run_id TEXT,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (country_code) REFERENCES countries(country_code)
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country_code TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT,
    source_url TEXT NOT NULL,
    source_date TEXT,
    source_tier TEXT,
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    run_id TEXT,
    is_superseded INTEGER DEFAULT 0,
    FOREIGN KEY (country_code) REFERENCES countries(country_code)
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_ratings_live ON risk_ratings(country_code, is_live);
CREATE INDEX IF NOT EXISTS idx_ratings_run ON risk_ratings(run_id);
CREATE INDEX IF NOT EXISTS idx_log_run ON scoring_log(run_id);
CREATE INDEX IF NOT EXISTS idx_rating_changes_country ON rating_changes(country_code);
CREATE INDEX IF NOT EXISTS idx_evidence_country ON evidence(country_code, field, is_superseded);
"""

STATUS_LABELS = {
    1: "Stable",
    2: "Elevated",
    3: "Stressed",
    4: "Severe",
    5: "Critical",
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    conn = sqlite3.connect(settings.database_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        # 1) Create tables (CREATE TABLE IF NOT EXISTS — does nothing if pre-existing).
        conn.executescript(SCHEMA_TABLES)

        # 2) Run column-level migrations BEFORE creating indexes, in case an older
        #    DB pre-dates a column we'll index on.
        ratings_cols = {r["name"] for r in conn.execute("PRAGMA table_info(risk_ratings)").fetchall()}
        for col, sqltype in [
            ("primary_source_url", "TEXT"),
            ("source_urls", "TEXT"),
            ("run_id", "TEXT"),
            ("oil_reserve_days", "INTEGER"),
            ("lng_status", "TEXT"),
            ("key_risk", "TEXT"),
            ("primary_source", "TEXT"),
            ("secondary_sources", "TEXT"),
            ("confidence", "TEXT"),
            ("hormuz_dependency", "TEXT"),
            ("gdp_loss_pct", "REAL"),
            ("is_pinned", "INTEGER DEFAULT 0"),
            ("original_baseline_rating", "INTEGER"),
            ("original_baseline_source", "TEXT"),
            ("last_verified_at", "TIMESTAMP"),
            ("last_checked_at", "TIMESTAMP"),
            ("last_verdict", "TEXT"),
            ("verifications_count", "INTEGER DEFAULT 0"),
            ("next_full_reassessment_due", "TIMESTAMP"),
            ("scenario_id", "TEXT"),
            ("primary_source_tier", "TEXT"),
        ]:
            if col not in ratings_cols:
                conn.execute(f"ALTER TABLE risk_ratings ADD COLUMN {col} {sqltype}")

        countries_cols = {r["name"] for r in conn.execute("PRAGMA table_info(countries)").fetchall()}
        for col, sqltype in [
            ("is_active", "INTEGER DEFAULT 1"),
            ("priority_tier", "TEXT DEFAULT 'P1'"),
            ("country_group", "TEXT"),
            ("source_whitelist", "TEXT"),
        ]:
            if col not in countries_cols:
                conn.execute(f"ALTER TABLE countries ADD COLUMN {col} {sqltype}")

        # 3) Now create indexes — all referenced columns are guaranteed to exist.
        conn.executescript(SCHEMA_INDEXES)


def upsert_country(
    conn: sqlite3.Connection,
    code: str,
    name: str,
    region: str,
    prewave_mapped_to: Optional[str],
    *,
    is_active: int = 1,
    priority_tier: str = "P1",
    country_group: Optional[str] = None,
    source_whitelist: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO countries (country_code, country_name, region, prewave_mapped_to,
                               is_active, priority_tier, country_group, source_whitelist)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(country_code) DO UPDATE SET
            country_name=excluded.country_name,
            region=excluded.region,
            prewave_mapped_to=excluded.prewave_mapped_to,
            is_active=excluded.is_active,
            priority_tier=excluded.priority_tier,
            country_group=excluded.country_group,
            source_whitelist=excluded.source_whitelist
        """,
        (code, name, region, prewave_mapped_to, is_active, priority_tier, country_group, source_whitelist),
    )


def get_all_countries(active_only: bool = False) -> list[dict[str, Any]]:
    with get_conn() as conn:
        sql = "SELECT * FROM countries"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY country_name"
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]


def get_country(code: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM countries WHERE country_code = ?", (code,)).fetchone()
        return dict(row) if row else None


# ---- Scenarios ----------------------------------------------------------------

def upsert_scenario(
    conn: sqlite3.Connection,
    *,
    scenario_id: str,
    name: str,
    description: Optional[str],
    context_block: str,
    affected_commodities: Optional[str] = None,
    affected_routes: Optional[str] = None,
    started_at: Optional[str] = None,
    is_active: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO scenarios (scenario_id, name, description, context_block,
                               affected_commodities, affected_routes,
                               started_at, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scenario_id) DO UPDATE SET
            name=excluded.name,
            description=excluded.description,
            context_block=excluded.context_block,
            affected_commodities=excluded.affected_commodities,
            affected_routes=excluded.affected_routes,
            started_at=excluded.started_at
        """,
        (scenario_id, name, description, context_block,
         affected_commodities, affected_routes,
         started_at or utcnow_iso(), is_active),
    )


def set_active_scenario(scenario_id: str) -> None:
    with get_conn() as conn:
        conn.execute("BEGIN")
        try:
            conn.execute("UPDATE scenarios SET is_active = 0, superseded_at = ? WHERE is_active = 1",
                         (utcnow_iso(),))
            conn.execute("UPDATE scenarios SET is_active = 1, superseded_at = NULL WHERE scenario_id = ?",
                         (scenario_id,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_active_scenario() -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM scenarios WHERE is_active = 1 ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def list_scenarios() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scenarios ORDER BY is_active DESC, started_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def insert_rating(
    conn: sqlite3.Connection,
    *,
    country_code: str,
    rating: int,
    oil_reserve_days: Optional[int],
    lng_status: Optional[str],
    key_risk: Optional[str],
    primary_source: Optional[str],
    secondary_sources: Optional[str],
    confidence: Optional[str],
    hormuz_dependency: Optional[str],
    gdp_loss_pct: Optional[float],
    scoring_method: str,
    run_id: str,
    is_live: int = 1,
    is_pinned: int = 0,
    primary_source_url: Optional[str] = None,
    source_urls: Optional[str] = None,
) -> int:
    status_label = STATUS_LABELS[rating]
    cur = conn.execute(
        """
        INSERT INTO risk_ratings
          (country_code, rating, status_label, oil_reserve_days, lng_status, key_risk,
           primary_source, primary_source_url, secondary_sources, source_urls,
           confidence, hormuz_dependency, gdp_loss_pct, scoring_method,
           is_live, is_pinned, run_id, scored_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            country_code, rating, status_label, oil_reserve_days, lng_status, key_risk,
            primary_source, primary_source_url, secondary_sources, source_urls,
            confidence, hormuz_dependency, gdp_loss_pct, scoring_method,
            is_live, is_pinned, run_id, utcnow_iso(),
        ),
    )
    return cur.lastrowid or 0


def get_live_ratings(active_only: bool = True) -> list[dict[str, Any]]:
    """Return live ratings joined with country metadata.

    By default filters to is_active=1 countries (the priority list).
    Pass active_only=False from admin endpoints that need full history.
    """
    with get_conn() as conn:
        sql = """
            SELECT r.*,
                   c.country_name, c.region, c.prewave_mapped_to,
                   c.is_active, c.priority_tier, c.country_group
            FROM risk_ratings r
            JOIN countries c ON c.country_code = r.country_code
            WHERE r.is_live = 1
        """
        if active_only:
            sql += " AND c.is_active = 1"
        sql += " ORDER BY c.country_name"
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]


def get_live_rating_for(code: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT r.*,
                   c.country_name, c.region, c.prewave_mapped_to,
                   c.is_active, c.priority_tier, c.country_group
            FROM risk_ratings r
            JOIN countries c ON c.country_code = r.country_code
            WHERE r.is_live = 1 AND r.country_code = ?
            """,
            (code,),
        ).fetchone()
        return dict(row) if row else None


def get_rating_history(code: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.*, c.country_name
            FROM risk_ratings r
            JOIN countries c ON c.country_code = r.country_code
            WHERE r.country_code = ?
            ORDER BY r.scored_at DESC
            """,
            (code,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_pinned_codes() -> set[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT country_code FROM risk_ratings WHERE is_live = 1 AND is_pinned = 1"
        ).fetchall()
        return {r["country_code"] for r in rows}


def supersede_live(conn: sqlite3.Connection, keep_pinned: bool = True) -> None:
    """Mark all current live ratings as superseded. Pinned rows are kept live by default."""
    if keep_pinned:
        conn.execute("UPDATE risk_ratings SET is_live = 0 WHERE is_live = 1 AND is_pinned = 0")
    else:
        conn.execute("UPDATE risk_ratings SET is_live = 0 WHERE is_live = 1")


def unpin_country(code: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE risk_ratings SET is_pinned = 0 WHERE country_code = ? AND is_live = 1",
            (code,),
        )
        return cur.rowcount


def start_run(run_id: str, trigger_source: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, status, trigger_source) VALUES (?, ?, ?, ?)",
            (run_id, utcnow_iso(), "running", trigger_source),
        )


def finish_run(run_id: str, status: str, countries_scored: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, countries_scored = ? WHERE run_id = ?",
            (utcnow_iso(), status, countries_scored, run_id),
        )


def log_scoring(run_id: str, country_code: Optional[str], raw_response: str, queries: Optional[str] = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO scoring_log (run_id, country_code, raw_ai_response, search_queries_used)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, country_code, raw_response, queries),
        )


def get_log_entries(limit: int = 200) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT l.*, r.status as run_status, r.started_at as run_started_at,
                   r.finished_at as run_finished_at, r.trigger_source
            FROM scoring_log l
            LEFT JOIN runs r ON r.run_id = l.run_id
            ORDER BY l.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_last_run() -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_last_successful_run_time() -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT finished_at FROM runs WHERE status = 'success' ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        return row["finished_at"] if row else None


def count_live_ratings() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) as n FROM risk_ratings WHERE is_live = 1").fetchone()
        return int(row["n"]) if row else 0
