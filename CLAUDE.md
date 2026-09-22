# ReAppraise

Photo → price tool for Habitat for Humanity ReStore volunteers. Google Vision labels the item, the eBay Browse API finds a market median, and a condition multiplier (fair = 0.6, the ReStore's 60% policy) turns it into a resale price. Every appraisal is saved to MySQL for history and analytics. See README.md for the full architecture and schema.

## Running it
- Use `venv/` only. `.venv/` was a stale duplicate and has been deleted; don't recreate it.
- Server: `venv/bin/uvicorn app:app --reload --reload-dir . --reload-exclude 'venv/*'` (binds 127.0.0.1:8000)
- Before testing, check `lsof -nP -iTCP:8000 -sTCP:LISTEN` shows one server. A stale server bound to `*:8000` was found and removed on 2026-09-21. Two PIDs sharing one socket is normal (uvicorn reloader + worker).
- Tests: `venv/bin/python -m pytest -q`. There are 104 tests: 27 app, 44 ebay, 6 vision, 27 MySQL integration. Integration tests **skip** when MySQL is down, so "all passed" only covers the DB if 0 were skipped.
- Fallback warnings (eBay errors, skipped labels, mock pricing) print to the uvicorn console. Check it after every upload.

## Persistence
- Local MySQL via Homebrew (`brew services start mysql`), app user `reappraise`; credentials in `.env`
- `MYSQL_TEST_DATABASE` is truncated before every integration test — never point it at the dev database (`MYSQL_DATABASE`)
- All SQL lives in `sql/queries.sql` as `-- name:` blocks and is loaded by name. Don't write SQL strings in Python.
- Schema changes go in a **new** numbered file in `sql/migrations/` (`003_….sql`). `sql/schema.sql` is the baseline; don't edit it or an already-applied migration. `python db.py init` applies pending migrations and records them in `schema_migrations`. The dev database has real data, so back it up before migrating (`mysqldump --no-tablespaces --set-gtid-purged=OFF`; the app user can't use `--single-transaction`).
- Deleting an item cascades to `listings_sampled` and `price_estimates`. Categories do **not** cascade: an empty category stays until it's deleted by hand.
- Analytics exclude `source = 'mock'` rows and use each item's latest estimate (`ROW_NUMBER()`).

## eBay pricing (ebay.py) — decisions to keep
- **Browse API only → mock fallback.** The Playwright scraper was removed on 2026-09-21: it bypassed eBay's bot detection, which likely breaks eBay's terms of service. It had been added in May when API access was denied, and it's no longer needed. Don't re-add scraping.
- Mock prices must stay visible: `is_mock=True`, `source='mock'`, the frontend badge, and a WARNING log saying why. Never fall back silently.
- `search_labels()` searches **one term at a time**, never terms joined into one query (joined terms AND together and match almost nothing). Order: the volunteer's `user_description` if given → Vision labels with generic ones skipped (at most 3) → a term with fewer than 5 priced listings moves on to the next → the largest real sample wins → mock only when nothing is found or the API errors.
- `user_description` (the volunteer's text) is stored apart from `description` (Vision's labels) and is never merged into the label list. Each estimate records `search_term` and `search_source` (`user_description` / `vision_label`); both are NULL for mock estimates and for estimates made before 2026-09-21.
- Generic-label denylist (`_GENERIC_LABEL_WORDS`) matches whole words inside a label. The stored category is the first specific label (`specific_labels()` in app.py).

## Known limitations (documented in README, not bugs)
- Estimates are only as specific as the label: a $1,600 bike is priced against generic "bicycle" listings.
- The denylist is a starter list. Material/colour-only labels still get through ("plastic" priced a wallet at a $12.62 median). The planned fix is to filter by kind of label rather than extend the word list. The optional description is the workaround in the meantime.
- Similar labels aren't merged: "bicycle" and "road bicycle" are separate categories.
- Browse API returns active asking prices, not sold prices.

## Working agreements
- The owner writes all commit messages. Never commit unless explicitly asked.
- Verify with raw data (MySQL rows, server logs), not just HTTP 200s. Show actual rows when reporting.
- Tests first for behaviour changes; keep the per-file test count accurate when reporting.
