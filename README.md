# Reappraise

Reappraise is a FastAPI service that estimates the resale value of a secondhand item from a photo. Upload an image, and the service identifies what's in it using Google Cloud Vision, then searches eBay's Browse API to return a low / median / high price range drawn from active listings. It's designed as a clean three-layer pipeline — image recognition, price lookup, and HTTP orchestration — each in its own module so the pieces are easy to swap or extend.

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
  ┌───────────────┐
  │    ebay.py    │  eBay Browse API (or built-in mock catalog)
  │               │  OAuth2 client-credentials token,
  │               │  active listings → price stats
  └──────┬────────┘
         │
         │  3. price estimate
         ▼
    JSON response
  { item, labels,
    price_estimate }
```

## Local setup

**Prerequisites:** Python 3.11+, a Google Cloud service account with the Cloud Vision API enabled (see `.env.example` for step-by-step instructions).

```bash
git clone <repo-url>
cd reappraise
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in your credentials
uvicorn app:app --reload
```

The service starts on `http://localhost:8000`. Interactive API docs (Swagger UI) are at `http://localhost:8000/docs`.

> **No eBay credentials yet?** Leave `EBAY_APP_ID` and `EBAY_CLIENT_SECRET` blank. The client automatically switches to a built-in mock catalog keyed by item category, so you can develop and demo without an eBay developer account.

## Try it

```bash
curl -X POST http://localhost:8000/appraise \
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
    "low": 40.0,
    "median": 120.0,
    "high": 350.0,
    "currency": "USD",
    "sample_size": 20,
    "is_mock": false
  }
}
```

The `is_mock` flag tells you whether the price range came from live eBay listings or the fallback catalog, so you always know what you're looking at.

## How it works

**1. Image upload**
`POST /appraise` accepts a multipart image file. FastAPI validates the content type and rejects empty files before any external API calls are made, so bad requests fail fast and cheaply.

**2. Item identification — `vision.py`**
The image bytes are sent to Google Cloud Vision in a single API call that runs two detectors at once: *label detection* (broad scene tags like "Electronics") and *object localization* (specific items like "Digital Camera"). Object localization results are inserted into the results first, so if both detectors identify the same name, the object score wins — it tends to name the thing you actually want to sell rather than the scene around it. All names are lowercased and deduplicated before being ranked by confidence score.

**3. Price lookup — `ebay.py`**
The top three labels are joined into a search query (e.g. `"digital camera electronics"`) and sent to eBay's Browse API. The service fetches up to 20 active listings sorted by price ascending, then computes the minimum, median, and maximum. **Median is used instead of mean** so that a single wildly overpriced listing doesn't inflate the estimate. When eBay credentials are absent the client transparently falls back to a hardcoded mock catalog, making local development and demo runs credential-free.

**4. Response assembly — `app.py`**
The orchestrator combines the label list and price estimate into a single JSON response. Both external API calls are wrapped in `try/except` blocks: failures surface as 502 errors with a descriptive message rather than unhandled 500s. Missing data (no labels detected, no eBay listings found) returns a 422 with an actionable message.

## Running the tests

```bash
pytest tests/ -v
```

The test suite covers unit tests for each module and FastAPI `TestClient` integration tests for the full request/response cycle, including error paths.

## Project structure

```
app.py              FastAPI service — routing, validation, orchestration
vision.py           Google Cloud Vision client
ebay.py             eBay Browse API client with automatic mock fallback
tests/
  test_vision.py    Unit tests for label detection and deduplication logic
  test_ebay.py      Unit tests for price lookup, mock catalog, and token caching
  test_app.py       Integration tests via FastAPI TestClient (happy path + errors)
.env.example        Credential setup instructions for Google and eBay
```
