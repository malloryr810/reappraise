# Reappraise

Reappraise is a FastAPI service that estimates the resale value of a secondhand item from a photo. Upload an image, and the service identifies what's in it using Google Cloud Vision, then scrapes eBay's completed/sold listings to return a low / median / high price range drawn from real sale data. It's designed as a clean three-layer pipeline — image recognition, price lookup, and HTTP orchestration — each in its own module so the pieces are easy to swap or extend.

## Architecture

```
POST /appraise (multipart image)
          │
          ▼
  ┌───────────────┐
  │    app.py     │  FastAPI — validates input, orchestrates calls,
  │               │            formats the JSON response
  └──────┬────────┘
         │
         │  1. image bytes
         ▼
  ┌───────────────┐
  │   vision.py   │  Google Cloud Vision API
  │               │  label detection + object localization (one API call)
  └──────┬────────┘
         │
         │  2. ranked labels  →  joined into a search query
         ▼
  ┌───────────────────────────────────────────┐
  │                 ebay.py                   │
  │                                           │
  │  In-memory cache (2-hour TTL)             │
  │    hit → return cached prices immediately │
  │    miss ↓                                 │
  │                                           │
  │  Playwright (headless Chromium)           │
  │    loads eBay completed/sold listings     │
  │    parses span.s-card__price elements     │
  │    skips strikethrough / range prices     │
  │                                           │
  │  Mock fallback (EBAY_MOCK=true or error)  │
  └──────┬────────────────────────────────────┘
         │
         │  3. price estimate
         ▼
    JSON response
  { item, labels,
    price_estimate }
```

## Local setup

**Prerequisites:** Python 3.11+, a Google Cloud API key with the Cloud Vision API enabled.

```bash
git clone <repo-url>
cd reappraise
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env        # fill in GOOGLE_VISION_API_KEY
uvicorn app:app --reload
```

The service starts on `http://localhost:8000`. Interactive API docs are at `http://localhost:8000/docs`.

> **No eBay credentials needed.** The scraper fetches real sold-listing prices directly from eBay's website using a headless Chromium browser — no developer account or API key required. Set `EBAY_MOCK=true` to use the built-in mock catalog instead (useful for offline development or CI).

## Try it

```bash
curl -X POST http://localhost:8000/appraise \
     -F "file=@/path/to/photo.jpg"
```

With a condition override:

```bash
curl -X POST "http://localhost:8000/appraise?condition=good" \
     -F "file=@/path/to/photo.jpg"
```

Example response:

```json
{
  "item": "digital camera",
  "labels": [
    { "name": "digital camera", "confidence": 0.97 },
    { "name": "camera",         "confidence": 0.90 },
    { "name": "electronics",    "confidence": 0.85 }
  ],
  "price_estimate": {
    "low": 16.57,
    "market_median": 142.80,
    "estimated_resale_value": 85.68,
    "high": 469.00,
    "currency": "USD",
    "sample_size": 20,
    "is_mock": false,
    "condition": "fair",
    "multiplier_used": 0.6
  }
}
```

`is_mock: false` confirms prices came from real eBay sold listings. `estimated_resale_value` is `market_median × multiplier_used`.

## Condition multipliers

| Condition | Multiplier | Notes |
|-----------|-----------|-------|
| `terrible` | 0.40 | |
| `poor` | 0.50 | |
| `fair` | **0.60** | Default — matches ReStore's standard 60% policy |
| `good` | 0.70 | |
| `like_new` | 0.80 | |

## How it works

**1. Image upload**
`POST /appraise` accepts a multipart image file and an optional `condition` query parameter. FastAPI validates content type and rejects empty files before any external calls are made.

**2. Item identification — `vision.py`**
Image bytes go to Google Cloud Vision in a single API call running two detectors: *label detection* (broad tags like "Electronics") and *object localization* (specific items like "Digital Camera"). Object localization results are ranked first — they tend to name the thing being sold rather than its surroundings. Results are lowercased, deduplicated, and sorted by confidence score. The top three labels are joined into the eBay search query.

**3. Price lookup — `ebay.py`**
The query is checked against an in-memory cache (2-hour TTL, keyed by normalised query string). On a cache hit, the stored price list is returned immediately with the condition multiplier reapplied — no network call. On a miss, Playwright launches (or reuses) a persistent headless Chromium browser, loads eBay's completed/sold listings page, waits for JavaScript to render prices, and extracts `span.s-card__price` elements. Strikethrough prices (crossed-out asking prices on Best-Offer-accepted listings whose actual accepted amount is undisclosed) are skipped. Up to 20 prices are collected and cached. **Median is used instead of mean** so one outlier listing doesn't skew the estimate. On any error — network failure, browser crash, zero results — the scraper falls back to the built-in mock catalog rather than returning a 502.

**4. Response assembly — `app.py`**
Label list and price estimate are combined into a single JSON response. External call failures surface as 502 errors with descriptive messages. Missing data (no labels detected, no listings found) returns a 422 with an actionable message.

## Running the tests

```bash
pytest tests/ -v
```

35 tests covering unit logic for each module and FastAPI `TestClient` integration tests for the full request/response cycle, including error paths, cache behaviour, browser reuse, and mock fallback. Playwright calls are mocked — no browser or network required to run the suite.

## Project structure

```
app.py              FastAPI service — routing, validation, orchestration
vision.py           Google Cloud Vision client
ebay.py             eBay scraper: Playwright, 2-hour cache, persistent browser, mock fallback
tests/
  test_vision.py    Unit tests for label detection and deduplication logic
  test_ebay.py      Unit tests for scraper, cache, browser reuse, mock catalog
  test_app.py       Integration tests via FastAPI TestClient (happy path + errors)
.env.example        Credential setup instructions
```
