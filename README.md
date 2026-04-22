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
| GET | `/api/v1/ratings.json` | All live ratings as JSON |
| GET | `/api/v1/ratings.csv` | All live ratings as CSV |
| GET | `/api/v1/ratings/{code}` | Single country by ISO code |
| GET | `/api/v1/health` | Status, last refresh, counts |

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

## Railway Deployment

1. Push this repo to GitHub.
2. Create a new Railway project → "Deploy from GitHub repo".
3. Add env vars in Railway dashboard (`ANTHROPIC_API_KEY`, `ADMIN_API_KEY`).
4. Railway auto-deploys on every push. Health check: `/api/v1/health`.
