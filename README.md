# Reappraise

A photo-to-price tool for Habitat for Humanity ReStore volunteers: photograph a donated item and get an estimated resale price drawn from real eBay listings.

## Motivation

I volunteered at a Habitat for Humanity ReStore where staff manually looked up donated items and priced them at roughly 60% of market value. The lookup was slow and inconsistent across volunteers. This tool automates the full workflow from a single photo.

## How it works

- **Google Cloud Vision** identifies the item — object localization and label detection run in one API call, deduplicated and sorted by confidence. The top labels become the eBay search query.
- **eBay Browse API** finds the market price — fetches an OAuth bearer token (client credentials, cached until expiry), searches recent listings, and takes the median of up to 20 prices. Results are cached in memory for 2 hours so repeated queries don't make a second network call.
- **Condition multiplier** scales the market median to a ReStore price — `fair` (0.6) is the default and matches the store's standard 60%-of-market policy.

## Architecture

```
POST /appraise (multipart image)
          │
          ▼
  ┌───────────────┐
  │    app.py     │  FastAPI — validates input, orchestrates calls,
  │               │            returns JSON response
  └──────┬────────┘
         │  1. image bytes
         ▼
  ┌───────────────┐
  │   vision.py   │  Google Cloud Vision API
  │               │  OBJECT_LOCALIZATION + LABEL_DETECTION
  └──────┬────────┘
         │  2. ranked labels → search query
         ▼
  ┌──────────────────────────────────────────────┐
  │                  ebay.py                     │
  │                                              │
  │  In-memory cache (2-hour TTL)                │
  │    hit → return cached prices immediately    │
  │    miss ↓                                    │
  │                                              │
  │  OAuth 2.0 client credentials flow           │
  │    POST /identity/v1/oauth2/token            │
  │    → Bearer token (cached until expiry)      │
  │                                              │
  │  eBay Browse API                             │
  │    GET /buy/browse/v1/item_summary/search    │
  │    median of up to 20 listing prices         │
  │                                              │
  │  Mock fallback (EBAY_MOCK=true or API error) │
  └──────┬───────────────────────────────────────┘
         │  3. price estimate
         ▼
    JSON response { item, labels, price_estimate }
```

## Condition multipliers

| Condition | Multiplier | Notes |
|-----------|-----------|-------|
| `terrible` | 0.40 | |
| `poor` | 0.50 | |
| `fair` | **0.60** | Default — matches ReStore's standard 60% policy |
| `good` | 0.70 | |
| `like_new` | 0.80 | |

## Local setup

**Prerequisites:** Python 3.11+, a [Google Cloud API key](https://console.cloud.google.com/) with Cloud Vision enabled, and an [eBay developer account](https://developer.ebay.com/) with Browse API access.

```bash
git clone <repo-url>
cd reappraise
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Edit `.env`:

```
GOOGLE_VISION_API_KEY=your-key
EBAY_CLIENT_ID=your-app-id
EBAY_CLIENT_SECRET=your-cert-id
```

```bash
uvicorn app:app --reload --host 0.0.0.0
```

Frontend at `http://localhost:8000` · API docs at `http://localhost:8000/docs`

`--host 0.0.0.0` makes the server reachable from other devices on the same network — useful for testing the camera UI on a phone.

Set `EBAY_MOCK=true` to skip the eBay API and use the built-in price catalog instead. No credentials needed; useful for local development without network calls.

## Try it

```bash
curl -X POST http://localhost:8000/appraise \
     -F "file=@loveseat.jpg"
```

```json
{
  "item": "loveseat",
  "labels": [
    { "name": "loveseat",  "confidence": 0.9421 },
    { "name": "furniture", "confidence": 0.8913 },
    { "name": "couch",     "confidence": 0.8104 }
  ],
  "price_estimate": {
    "low": 179.99,
    "market_median": 241.49,
    "estimated_resale_value": 144.89,
    "high": 899.01,
    "currency": "USD",
    "sample_size": 20,
    "is_mock": false,
    "condition": "fair",
    "multiplier_used": 0.6
  }
}
```

`estimated_resale_value` = `market_median × multiplier_used`. `is_mock: false` confirms prices came from real eBay listings.

## Running the tests

```bash
pytest tests/ -v
```

35 tests covering Vision parsing, eBay pricing logic, cache behaviour, and full request/response integration. No external calls required — all API calls are mocked.

## Tech stack

- Python 3.11+
- FastAPI + Starlette
- Google Cloud Vision API (REST, plain API key)
- eBay Browse API (OAuth 2.0 client credentials)
- httpx
- pytest
