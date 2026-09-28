# ReAppraise

Photo → price tool for Habitat for Humanity ReStore volunteers. Google Vision labels the item, the eBay Browse API finds a market median, and a condition multiplier (fair = 0.6, the ReStore's 60% policy) turns it into a resale price. Every appraisal is saved to MySQL for history and analytics. See README.md for the full architecture and schema.

## Running it
- The project lives at `~/code/reappraise`. It moved off `~/Desktop` on 2026-09-28 because iCloud evicted files in `venv/` and `.git/`, which made imports hang. Keep it out of iCloud-synced folders (Desktop, Documents).
- Use `venv/` only. `.venv/` was a stale duplicate and has been deleted; don't recreate it.
- Server: `venv/bin/uvicorn app:app --reload --reload-dir . --reload-exclude 'venv/*'` (binds 127.0.0.1:8000)
- Before testing, check `lsof -nP -iTCP:8000 -sTCP:LISTEN` shows one server. A stale server bound to `*:8000` was found and removed on 2026-09-21. Two PIDs sharing one socket is normal (uvicorn reloader + worker).
- Tests: `venv/bin/python -m pytest -q`. There are 211 tests: 69 app, 6 db (migration file naming, rollback notes), 49 ebay, 1 frontend (runs `node --test` on `tests/frontend/`; needs Node, and fails rather than skips without it, so CI must install Node), 27 labels (taxonomy rules), 6 vision, 53 MySQL integration. Integration tests **skip** when MySQL is down, so "all passed" only covers the DB if 0 were skipped.
- Logging is configured at import (`_configure_logging` in app.py): `time LEVEL logger: message` on the root logger, level from `LOG_LEVEL` (default INFO); httpx/httpcore are held at WARNING. uvicorn's own loggers don't propagate to root, so nothing prints twice.
- Fallback warnings (eBay errors, skipped labels, invalid taxonomy rows, mock pricing) print to the uvicorn console. Check it after every upload. At startup an ERROR is logged if the database has pending migrations; saves fail until `python db.py init` is run, and the page shows "Not saved".

## Persistence
- Local MySQL via Homebrew (`brew services start mysql`), app user `reappraise`; credentials in `.env`
- `MYSQL_TEST_DATABASE` is truncated before every integration test — never point it at the dev database (`MYSQL_DATABASE`)
- All SQL lives in `sql/queries.sql` as `-- name:` blocks and is loaded by name. Don't write SQL strings in Python.
- Schema changes go in a **new** numbered file in `sql/migrations/` (`003_….sql`; a misnamed file makes `init` fail rather than be skipped). Keep one `ALTER TABLE` per file: MySQL auto-commits DDL, so a file that fails halfway can't roll back. From 003 on, every migration carries a `-- rollback:` comment; a unit test enforces it, and an integration test runs the notes to rewind the test database to 002 and migrate it forward again. `sql/schema.sql` is the baseline; don't edit it or an already-applied migration. `python db.py init` applies pending migrations and records them in `schema_migrations`. The dev database has real data, so back it up before migrating (`mysqldump --no-tablespaces --set-gtid-purged=OFF`; the app user can't use `--single-transaction`).
- Deleting an item cascades to `listings_sampled` and `price_estimates`, and from an estimate to its `outcomes` row. Categories do **not** cascade: an empty category stays until it's deleted by hand.
- Analytics exclude `source = 'mock'` rows and use each item's latest estimate (`ROW_NUMBER()`).
- Accuracy (`/analytics/accuracy`) compares estimates with `sold_price` only, and always shows n. `final_price` is set after the volunteer has seen the estimate, so it is reported separately as "agreement with volunteer price", never as accuracy.

## eBay pricing (ebay.py) — decisions to keep
- **Browse API only → mock fallback.** The Playwright scraper was removed on 2026-09-21: it bypassed eBay's bot detection, which likely breaks eBay's terms of service. It had been added in May when API access was denied, and it's no longer needed. Don't re-add scraping.
- Mock prices must stay visible: `is_mock=True`, `source='mock'`, the frontend badge, and a WARNING log saying why. Never fall back silently.
- `search_labels()` searches **one term at a time**, never terms joined into one query (joined terms AND together and match almost nothing). Order: the volunteer's `user_description` if given → Vision labels the taxonomy calls objects, one per canonical term (at most 3), each searched as Vision gave it and then as its canonical term if that finds too few → a term with fewer than 5 priced listings moves on to the next → the largest real sample wins → mock only when nothing is found or the API errors.
- `user_description` (the volunteer's text) is stored apart from `description` (Vision's labels) and is never merged into the label list. Each estimate records `search_term` and `search_source` (`user_description` / `vision_label`); both are NULL for mock estimates and for estimates made before migration 002 was applied (items 3–5 in the dev database). `vision_label` (migration 005) is the Vision label behind a `vision_label` search, since `search_term` can be its canonical form.
- Label taxonomy (`labels.py`, `labels` table from migration 004): kinds are object / material / color / generic. Labels not in the table are objects, searched exactly as given. One-word generic entries also match as words inside a label (the old denylist rule); material and colour entries match the whole label only, so "plastic bottle" is still searched. `ebay.py` never touches the database: app.py loads the taxonomy per appraisal and passes it in. If the table can't be read or is empty, it falls back to `FALLBACK_TAXONOMY` (the original generic-word list) with a WARNING log. Never fall back silently.
- The stored category is the canonical term of the first searchable label. Accuracy analytics group estimates by (`search_source`, canonical term), through a join to `labels`: "road bicycle" and "bicycle" photo searches count together, and a typed "road bicycle" is canonicalised too but stays in its own row. Invalid `labels` rows are skipped with a WARNING; the fallback list is used only if the table can't be read or has no valid rows.

## Known limitations (documented in README, not bugs)
- Estimates are only as specific as the label: a $1,600 bike is priced against generic "bicycle" listings.
- The taxonomy seed is a starter list: only the materials, colours and merges in migration 004 are known. Unlisted material/colour labels are still searched as objects. Extend it with rows (a new migration), not code.
- Canonical merges keep specificity for pricing: "road bicycle" is searched as itself first and only falls back to "bicycle" with fewer than 5 priced listings. The merge still decides the category, the analytics grouping and dedup (a later "Bicycle" label isn't searched separately), so only merge labels that belong in one category.
- Browse API returns active asking prices, not sold prices.

## Working agreements
- The owner writes all commit messages. Never commit unless explicitly asked.
- Verify with raw data (MySQL rows, server logs), not just HTTP 200s. Show actual rows when reporting.
- Tests first for behaviour changes; keep the per-file test count accurate when reporting.
