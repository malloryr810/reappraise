-- ReAppraise named queries.
--
-- Each block starts with `-- name: <id>`; db.py loads them by name so this file
-- is the single source of truth for every statement the app runs. Parameters
-- use PyMySQL's %(name)s style — to run a query by hand in the mysql client,
-- substitute literal values.
--
-- "Latest estimate per item": an item can accumulate several estimates (see
-- recompute in repository.py), so aggregate queries rank estimates per item
-- with ROW_NUMBER() and keep rn = 1. Without this, recomputed items would be
-- double-counted in category averages.


-- ===========================================================================
-- Write path (one transaction per appraisal — see repository.save_appraisal)
-- ===========================================================================

-- name: upsert_category
-- LAST_INSERT_ID(expr) makes cursor.lastrowid return the existing row's id when
-- the name already exists, so insert-or-lookup is a single round trip.
INSERT INTO categories (name)
VALUES (%(name)s)
ON DUPLICATE KEY UPDATE category_id = LAST_INSERT_ID(category_id);

-- name: insert_item
INSERT INTO items (category_id, description, condition_label, condition_multiplier)
VALUES (%(category_id)s, %(description)s, %(condition_label)s, %(condition_multiplier)s);

-- name: insert_listing
-- Run via executemany(); PyMySQL rewrites it into one multi-row INSERT.
INSERT INTO listings_sampled (item_id, ebay_listing_id, sampled_price)
VALUES (%(item_id)s, %(ebay_listing_id)s, %(sampled_price)s);

-- name: insert_estimate
INSERT INTO price_estimates (item_id, market_median, estimated_price, source)
VALUES (%(item_id)s, %(market_median)s, %(estimated_price)s, %(source)s);


-- ===========================================================================
-- History read path
-- ===========================================================================

-- name: recent_estimates
-- Most recent price estimates, joined item -> category -> estimate, with the
-- sampled-listing spread aggregated per item. LATERAL (MySQL 8.0.14+) lets the
-- subquery reference i.item_id, so it's evaluated per row via the FK index
-- instead of aggregating the entire listings table.
SELECT pe.estimate_id,
       i.item_id,
       c.name              AS category,
       i.description,
       i.condition_label,
       i.condition_multiplier,
       pe.market_median,
       pe.estimated_price,
       pe.source,
       pe.created_at,
       ls.sample_size,
       ls.low,
       ls.high
FROM price_estimates pe
JOIN items i      ON i.item_id = pe.item_id
JOIN categories c ON c.category_id = i.category_id
JOIN LATERAL (
    SELECT COUNT(*)           AS sample_size,
           MIN(sampled_price) AS low,
           MAX(sampled_price) AS high
    FROM listings_sampled l
    WHERE l.item_id = i.item_id
) ls ON TRUE
ORDER BY pe.created_at DESC, pe.estimate_id DESC
LIMIT %(limit)s;

-- name: get_item
SELECT i.item_id,
       c.name AS category,
       i.description,
       i.condition_label,
       i.condition_multiplier,
       i.created_at
FROM items i
JOIN categories c ON c.category_id = i.category_id
WHERE i.item_id = %(item_id)s;

-- name: item_estimates
SELECT estimate_id, market_median, estimated_price, source, created_at
FROM price_estimates
WHERE item_id = %(item_id)s
ORDER BY created_at DESC, estimate_id DESC;

-- name: item_listings
SELECT listing_id, ebay_listing_id, sampled_price, sampled_at
FROM listings_sampled
WHERE item_id = %(item_id)s
ORDER BY sampled_price, listing_id;


-- ===========================================================================
-- Recompute (derived pricing from stored raw data — no eBay call)
-- ===========================================================================

-- name: median_from_listings
-- MySQL has no MEDIAN(). Number the sorted prices, then average the middle
-- one (odd n) or middle two (even n): for n=4, FLOOR(2.5)=2 and CEIL(2.5)=3.
WITH ranked AS (
    SELECT sampled_price,
           ROW_NUMBER() OVER (ORDER BY sampled_price) AS rn,
           COUNT(*)     OVER ()                       AS n
    FROM listings_sampled
    WHERE item_id = %(item_id)s
)
SELECT ROUND(AVG(sampled_price), 2) AS market_median,
       MAX(n)                       AS sample_size
FROM ranked
WHERE rn IN (FLOOR((n + 1) / 2), CEIL((n + 1) / 2));


-- ===========================================================================
-- Analytics (mock-priced rows excluded throughout)
-- ===========================================================================

-- name: avg_price_by_category
-- Average estimated price by category, using each item's latest real estimate.
WITH latest AS (
    SELECT pe.item_id,
           pe.market_median,
           pe.estimated_price,
           ROW_NUMBER() OVER (PARTITION BY pe.item_id
                              ORDER BY pe.created_at DESC, pe.estimate_id DESC) AS rn
    FROM price_estimates pe
    WHERE pe.source <> 'mock'
)
SELECT c.name                            AS category,
       COUNT(*)                          AS item_count,
       ROUND(AVG(le.estimated_price), 2) AS avg_estimated_price,
       ROUND(AVG(le.market_median), 2)   AS avg_market_median
FROM latest le
JOIN items i      ON i.item_id = le.item_id
JOIN categories c ON c.category_id = i.category_id
WHERE le.rn = 1
GROUP BY c.category_id, c.name
ORDER BY avg_estimated_price DESC, category;

-- name: below_category_market
-- Items whose market median is well below their category's average median —
-- e.g. ratio = 0.5 flags items trading at under half the category norm.
-- (Comparing estimated_price to its own market_median would be meaningless:
-- estimated = median x condition multiplier, so it's always 40-80% by design.)
-- The window AVG() computes the category baseline without a self-join.
WITH latest AS (
    SELECT pe.item_id,
           pe.market_median,
           pe.estimated_price,
           ROW_NUMBER() OVER (PARTITION BY pe.item_id
                              ORDER BY pe.created_at DESC, pe.estimate_id DESC) AS rn
    FROM price_estimates pe
    WHERE pe.source <> 'mock'
),
scored AS (
    SELECT i.item_id,
           c.name AS category,
           i.description,
           i.condition_label,
           le.market_median,
           le.estimated_price,
           AVG(le.market_median) OVER (PARTITION BY i.category_id) AS category_avg_median,
           COUNT(*)              OVER (PARTITION BY i.category_id) AS category_items
    FROM latest le
    JOIN items i      ON i.item_id = le.item_id
    JOIN categories c ON c.category_id = i.category_id
    WHERE le.rn = 1
)
SELECT item_id,
       category,
       description,
       condition_label,
       market_median,
       estimated_price,
       ROUND(category_avg_median, 2)                                   AS category_avg_median,
       ROUND(market_median / NULLIF(category_avg_median, 0), 2)        AS ratio_to_category
FROM scored
WHERE category_items >= %(min_peers)s
  AND market_median < %(ratio)s * category_avg_median
ORDER BY ratio_to_category, item_id
LIMIT %(limit)s;

-- name: top_category_by_volume
-- Category with the highest item volume processed. RANK() returns every
-- category tied for first rather than arbitrarily picking one. Counts all
-- processed items, including ones priced from the mock catalog.
WITH volume AS (
    SELECT c.name                                       AS category,
           COUNT(*)                                     AS item_count,
           RANK() OVER (ORDER BY COUNT(*) DESC)         AS volume_rank
    FROM categories c
    JOIN items i ON i.category_id = c.category_id
    GROUP BY c.category_id, c.name
)
SELECT category, item_count
FROM volume
WHERE volume_rank = 1
ORDER BY category;
