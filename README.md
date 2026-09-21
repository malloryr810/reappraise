# Reappraise

A photo-to-price tool for Habitat for Humanity ReStore volunteers: photograph a donated item and get an estimated resale price drawn from real eBay listings.

## Motivation

I volunteered at a Habitat for Humanity ReStore where staff manually looked up donated items and priced them at roughly 60% of market value. The lookup was slow and inconsistent across volunteers. This tool automates the full workflow from a single photo.

## How it works

- **Google Cloud Vision** identifies the item — object localization and label detection run in one API call, deduplicated and sorted by confidence. The top labels become the eBay search query.
- **eBay Browse API** finds the market price — fetches an OAuth bearer token (client credentials, cached until expiry), searches recent listings, and takes the median of up to 20 prices. Results are cached in memory for 2 hours so repeated queries don't make a second network call.
- **Condition multiplier** scales the market median to a ReStore price — `fair` (0.6) is the default and matches the store's standard 60%-of-market policy.
- **MySQL** stores every appraisal in a normalized schema: the category, the item, each sampled eBay listing, and the derived estimate. That backs a price-history view and category-level analytics queries.

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
         │  3. price estimate + the individual sampled listings
         ▼
  ┌───────────────┐
  │ repository.py │  One MySQL transaction per appraisal:
  │   + db.py     │  category → item → listings_sampled → price_estimates
  └──────┬────────┘  (SQL lives in sql/queries.sql, loaded by name)
         │  4. item_id
         ▼
    JSON response { item, labels, price_estimate, item_id }
```

## Persistence (MySQL)

```
categories 1───* items 1───* listings_sampled     (raw market data)
                       1───* price_estimates      (derived pricing)
```

| Table | One row per | Key columns |
|-------|-------------|-------------|
| `categories` | distinct Vision label | `name` (unique) |
| `items` | appraisal | `category_id`, `description` (search query), `condition_label`, `condition_multiplier` |
| `listings_sampled` | eBay listing used in the median | `item_id`, `ebay_listing_id`, `sampled_price` |
| `price_estimates` | computed estimate | `item_id`, `market_median`, `estimated_price`, `source` |

**Why normalized, not one flat table:** raw sampled prices are stored separately from the estimate derived from them. `repository.recompute_estimate()` re-derives an estimate from stored listings, with the median computed in SQL, so pricing logic can change without calling eBay again. Each item keeps its full estimate history, and analytics queries pick the latest estimate per item with `ROW_NUMBER()` so recomputed items aren't double-counted.

**`source` column:** `browse_api` (active asking prices), `sold_scrape` (completed/sold prices) or `mock` (built-in catalog when eBay is unreachable). Analytics exclude `mock` rows so placeholder numbers never skew trends.

Every statement lives in [`sql/queries.sql`](sql/queries.sql) as a named block, and the schema is in [`sql/schema.sql`](sql/schema.sql). Queries are parameterized, and each appraisal is written in a single transaction, so a failure part-way through leaves nothing behind.

### Read endpoints

| Endpoint | Query | What it shows |
|----------|-------|---------------|
| `GET /history?limit=20` | `recent_estimates` | Latest estimates joined item → category → estimate, with the listing spread aggregated through a `LATERAL` subquery |
| `GET /history/{item_id}` | `get_item`, `item_estimates`, `item_listings` | One item with its estimate history and every sampled listing |
| `GET /analytics/categories` | `avg_price_by_category` | Average estimated price and market median per category |
| `GET /analytics/below-market?ratio=0.5` | `below_category_market` | Items whose market median is under `ratio` × their category's average (a window `AVG() OVER (PARTITION BY …)`) |
| `GET /analytics/top-category` | `top_category_by_volume` | Category with the most items processed (`RANK()`, so ties are all returned) |

The frontend shows the last 10 appraisals from `/history` under the pricing form.

Persistence is optional. If `MYSQL_DATABASE` is unset the app works as before, `item_id` is `null`, and the read endpoints return 503. If the database goes down mid-request, the appraisal is still returned and the error is logged.

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
cp .env.example .env   # then fill in your keys
```

**MySQL** (optional, needed for history and analytics). On macOS:

```bash
brew install mysql && brew services start mysql
mysql -u root <<'SQL'
CREATE DATABASE reappraise;
CREATE DATABASE reappraise_test;
CREATE USER 'reappraise'@'localhost' IDENTIFIED BY 'choose-a-password';
GRANT ALL PRIVILEGES ON reappraise.*      TO 'reappraise'@'localhost';
GRANT ALL PRIVILEGES ON reappraise_test.* TO 'reappraise'@'localhost';
SQL
```

Set the `MYSQL_*` variables in `.env`, then create the tables (idempotent):

```bash
python db.py init
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
  },
  "item_id": 17
}
```

`estimated_resale_value` = `market_median × multiplier_used`. `is_mock: false` confirms prices came from real eBay listings.

## Running the tests

```bash
pytest                   # everything
pytest -m "not integration"   # skip the MySQL tests
```

70 tests. The unit tests mock every external call. The 18 integration tests in `tests/test_db_integration.py` run against a real MySQL database (`MYSQL_TEST_DATABASE`, truncated before each test). They cover the transactional write path and rollback, each read and analytics query, SQL-computed medians checked against Python's `statistics.median`, recompute, and a full `/appraise` → `/history` round trip. They are skipped automatically when MySQL isn't configured.

## Tech stack

- Python 3.11+
- FastAPI + Starlette
- Google Cloud Vision API (REST, plain API key)
- eBay Browse API (OAuth 2.0 client credentials)
- MySQL 8+ via PyMySQL (raw parameterized SQL, window functions, CTEs, `LATERAL`)
- httpx
- pytest
