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
- `app.py` — FastAPI, POST /appraise, condition as optional query param

## eBay scraper details
The eBay Developer API was blocked (developer account rejected twice). Instead, the scraper uses Playwright (headless Chromium) to load eBay's completed/sold listing pages directly — this bypasses the Akamai JavaScript browser challenge that blocks plain HTTP clients even with correct TLS fingerprints.

Two performance optimisations are active:
- **In-memory cache**: results keyed by normalised query string, 2-hour TTL. Cache stores raw price lists so any condition's multiplier can be applied without re-scraping.
- **Persistent browser**: one Chromium instance shared across all requests. Initialised lazily on first scrape call, re-launched automatically if disconnected.

CSS selector: `span.s-card__price` (eBay's current markup). Strikethrough prices (crossed-out asking prices on Best-Offer-accepted listings) are skipped because the actual accepted amount is not disclosed.

## Environment variables needed
- `GOOGLE_VISION_API_KEY` — plain API key from GCP console
- `EBAY_MOCK=true` — optional; forces mock catalog, bypasses scraper entirely

## Current status
- 35/35 tests passing
- Google Vision working with real images
- eBay scraper live — fetches real sold-listing prices via Playwright
- In-memory cache (2-hour TTL) and persistent browser both active
- No frontend yet

## Next task when resuming
Build a simple frontend (file upload form, results display). Then update README with a screenshot.
