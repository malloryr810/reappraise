# Item Appraisal Tool — ReStore Pricing Assistant

## What this project does
User photographs a donated item → Google Vision identifies it → Playwright scrapes eBay's sold/completed listings for market prices → applies a condition multiplier to estimate fair resale price for a Habitat for Humanity ReStore.

## Motivation
Built to automate the manual price lookup process used at a Habitat for Humanity ReStore. Staff would look up donated items and price them at roughly 60% of market value. This tool does that automatically from a photo.

## Pricing logic
Condition multiplier replaces the flat rate (not stacked on top):
- terrible: 0.4 / poor: 0.5 / fair: 0.6 / good: 0.7 / like_new: 0.8
- "fair" is the default and matches the ReStore's standard pricing policy

## Project structure
- `vision.py` — Google Vision REST API, plain API key, LABEL_DETECTION + OBJECT_LOCALIZATION
- `ebay.py` — Playwright-based eBay scraper (completed/sold listings), 2-hour in-memory cache, persistent browser instance, mock fallback
- `app.py` — FastAPI, POST /appraise (condition validated against CONDITION_MULTIPLIERS), GET /history, /history/{item_id}, /analytics/*
- `db.py` — MySQL config from env, transactional `connect()`, named-query loader, `python db.py init`
- `repository.py` — save_appraisal (one transaction), history reads, recompute_estimate, analytics
- `sql/schema.sql` — categories → items → listings_sampled / price_estimates
- `sql/queries.sql` — every SQL statement, as `-- name: <id>` blocks (single source of truth)
- `frontend/index.html` — upload form, results, Recent Appraisals card (GET /history)

## eBay scraper details
The eBay Developer API was blocked (developer account rejected twice). Instead, the scraper uses Playwright (headless Chromium) to load eBay's completed/sold listing pages directly — this bypasses the Akamai JavaScript browser challenge that blocks plain HTTP clients even with correct TLS fingerprints.

Two performance optimisations are active:
- **In-memory cache**: results keyed by normalised query string, 2-hour TTL. Cache stores raw price lists so any condition's multiplier can be applied without re-scraping.
- **Persistent browser**: one Chromium instance shared across all requests. Initialised lazily on first scrape call, re-launched automatically if disconnected.

CSS selector: `span.s-card__price` (eBay's current markup). Strikethrough prices (crossed-out asking prices on Best-Offer-accepted listings) are skipped because the actual accepted amount is not disclosed.

## Environment variables needed
- `GOOGLE_VISION_API_KEY` — plain API key from GCP console
- `EBAY_MOCK=true` — optional; forces mock catalog, bypasses scraper entirely
- `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` — Browse API (tried before the scraper when set)
- `MYSQL_HOST/PORT/USER/PASSWORD/DATABASE` — persistence; unset MYSQL_DATABASE disables it
- `MYSQL_TEST_DATABASE` — integration tests (truncated per test; must differ from MYSQL_DATABASE)

## Persistence
- Local MySQL via Homebrew (`brew services start mysql`), app user `reappraise`
- Analytics exclude `source = 'mock'` rows and use the latest estimate per item (ROW_NUMBER)
- DB failure during /appraise is logged and the appraisal is still returned (item_id = null)
- Run `pytest -m "not integration"` to skip MySQL-backed tests

## Current status
- 70/70 tests passing (18 are MySQL integration tests, skipped without MYSQL_TEST_DATABASE)
- MySQL persistence live; verified end to end with real eBay Browse API data
- Frontend built, including Recent Appraisals history card
- Google Vision key was returning 403 Forbidden as of 2026-09-21 (key/project issue, not code)
