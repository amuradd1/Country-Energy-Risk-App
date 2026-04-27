# Country Energy Risk Feed
*Executive briefing*

---

## At a glance

| | |
|---|---|
| **Coverage** | 43 supplier countries |
| **Refresh** | Weekly, Monday 06:00 UTC |
| **Cost** | ~£8 / month |
| **Output** | 1–5 risk score, sourced + dated |
| **Consumption** | Power BI, JSON/CSV API |

---

## How a rating is produced

1. **Anchor** — Country starts at a public baseline (Prewave SITREP).
2. **Verify weekly** — AI searches the country's domestic and government sources. It does not re-score from scratch; it asks *"does the rating still hold?"*
3. **Audit** — Every URL, date, and rating change is logged.

---

## Why we trust it

- **No memory answers.** Every claim must cite a fresh URL.
- **URLs are validated.** Country-specific sources or tier-1 newswires only.
- **Movement is capped.** ±1 per week, ±2 only with government / IEA sources.
- **"I don't know" is allowed.** System never fakes verification.

---

## When something fails

| Failure | Response |
|---|---|
| No fresh news for a country | Holds last rating; badge shows staleness; falls back to IEA structural data after 4–8 weeks |
| Anchor source goes silent | AI verification continues independently |
| Crisis context shifts | Active scenario is one editable database row |

---

## What it is and isn't

- **Is** — decision support for weekly procurement reviews
- **Isn't** — real-time tactical feed; licensed redistribution data

---

## Architecture in one table

| Layer | Component |
|---|---|
| Sources | Prewave SITREP + Anthropic web search + 43-country curated whitelist |
| Engine | Python / FastAPI on Railway, weekly cron |
| Storage | SQLite with full audit trail |
| Consumption | Power BI, JSON / CSV API, internal HTML dashboard |

---

## Bottom line

Vendor-grade country energy intelligence, owned in-house, at coffee-budget cost.
Self-healing if any single source fails; fully audited for every rating change.
