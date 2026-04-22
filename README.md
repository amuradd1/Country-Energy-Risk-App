# Country Energy Risk Feed API

A FastAPI service that serves live country energy risk ratings (1–5 scale) as JSON and CSV. Scores are refreshed daily at 06:00 UTC by an AI engine that uses the Anthropic API with web search. Excel and Power BI connect via Power Query.

## Quick Start

```bash
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY and ADMIN_API_KEY
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Swagger UI: http://localhost:8000/docs

## Endpoints

### Public

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | **Web UI** — table of all countries with live ratings, filter, source links |
| GET | `/admin-ui` | **Admin panel** — trigger refresh, override, log (requires key) |
| GET | `/api/v1/ratings.json` | All live ratings as JSON |
| GET | `/api/v1/ratings.csv` | All live ratings as CSV |
| GET | `/api/v1/ratings/{code}` | Single country by ISO code |
| GET | `/api/v1/health` | Status, last refresh, counts |
| GET | `/docs` | OpenAPI / Swagger UI |

### Admin (requires `X-Admin-Key` header)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/admin/override` | Manually override a country's rating |
| POST | `/admin/unpin` | Remove a pin from an overridden rating |
| POST | `/admin/trigger-refresh` | Manually trigger a full scoring run |
| GET | `/admin/log` | Audit log of all scoring runs |
| GET | `/admin/history/{code}` | Full rating history for a country |
| GET | `/admin/runs` | List of all scoring runs |

## Rating Scale

| Rating | Label | Definition |
|--------|-------|-----------|
| 1 | Stable | >120 days reserves, diversified supply |
| 2 | Elevated | 60–120 days, price pressure but stable |
| 3 | Stressed | 21–60 days, elevated prices, contingency active |
| 4 | Severe | 14–21 days, emergency measures active |
| 5 | Critical | <14 days, active rationing, FM declared |

## Power Query (Excel / Power BI)

**JSON:**
```m
let
    Source = Json.Document(Web.Contents("https://your-app.railway.app/api/v1/ratings.json")),
    Ratings = Source[ratings],
    Table = Table.FromList(Ratings, Splitter.SplitByNothing(), null, null, ExtraValues.Error),
    Expanded = Table.ExpandRecordColumn(Table, "Column1", {
        "country_code","country_name","region","energy_risk_rating","status_label",
        "oil_reserve_days","lng_status","key_risk","primary_source","confidence",
        "hormuz_dependency","scoring_method","scored_at"
    })
in
    Expanded
```

**CSV (simpler):**
```m
let
    Source = Csv.Document(
        Web.Contents("https://your-app.railway.app/api/v1/ratings.csv"),
        [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]
    )
in
    Source
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API key | required |
| `ADMIN_API_KEY` | Secret key for admin endpoints | `changeme` |
| `PORT` | Server port | `8000` |
| `SCORING_CRON_HOUR` | UTC hour for daily refresh | `6` |
| `DATABASE_PATH` | SQLite file path | `energy_risk.db` |
| `ANTHROPIC_MODEL` | Model used for scoring | `claude-opus-4-7` |
| `SCORING_BATCH_SIZE` | Countries per API call | `10` |

## Scoring Methodology

1. **Prewave Direct** — 7 countries (BD, PK, IN, KR, JP, CN, US): Prewave SITREP Day 33 values are the baseline; AI validates with current web search and may adjust ±1.
2. **Prewave EU Mapped** — 16 EU countries: inherit the EU/Germany baseline (rating 3, Stressed); AI adjusts ±1 based on country-specific energy profile.
3. **AI Researched** — remaining ~40 countries: full AI web search scoring from scratch.

Each daily run atomically supersedes all previous live ratings (except pinned overrides). Every AI response is stored in the audit log.

## Anti-Hallucination Safeguards

Hallucination is the single biggest risk with AI scoring, and the scoring engine is hardened against it:

1. **Web search is mandatory.** Every AI call uses the Anthropic hosted `web_search_20250305` tool. The model cannot answer from training data alone.
2. **Sources are extracted, not trusted.** The app extracts every URL returned by `web_search` during each batch and stores them in the `source_urls` column.
3. **Cited URLs are validated.** The model is required to return `primary_source_url` for each country. The app checks that URL against the set of URLs actually returned by search — fabricated URLs are rejected.
4. **Unverifiable ratings drop to Low confidence.** If the model cannot cite a real URL, the confidence field is automatically capped at Medium or Low.
5. **Nothing is invented.** The prompt explicitly forbids estimating reserve days, force majeure declarations, or emergency measures that cannot be traced to a cited source.
6. **Full audit trail.** Every raw AI response + every URL the web search visited is written to `scoring_log`, keyed by `run_id`. Inspect via `GET /admin/log` or the admin UI.
7. **First-run auto-trigger.** On first deploy (once `ANTHROPIC_API_KEY` is set) the app automatically kicks off the first full AI pass in the background so the feed populates without waiting for 06:00 UTC.

## Repeatability & Scalability

- Batched: countries scored 10 at a time (configurable via `SCORING_BATCH_SIZE`) to stay within token limits.
- Retried: Anthropic API errors retry 3× with exponential backoff; permanent failures fall back to baseline with Low confidence.
- Atomic: each run either fully replaces the live set or rolls back — readers never see partial data.
- Idempotent: re-running the same batch produces equivalent output; ratings carry a `run_id` for traceability.
- Pinned overrides are respected across automated runs until explicitly unpinned.

## Railway Deployment

1. Push this repo to GitHub.
2. Create a new Railway project → "Deploy from GitHub repo".
3. Add env vars in Railway dashboard (`ANTHROPIC_API_KEY`, `ADMIN_API_KEY`).
4. Railway auto-deploys on every push. Health check: `/api/v1/health`.
