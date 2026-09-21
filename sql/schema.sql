-- ReAppraise persistence schema (MySQL 8+).
--
-- Raw market data (listings_sampled) is kept separate from derived pricing
-- (price_estimates) so an estimate can be recomputed from stored listings
-- without re-hitting eBay. An item can therefore have several estimates over
-- time; read queries pick the latest one per item.
--
-- Idempotent: safe to run repeatedly (`python db.py init`).

CREATE TABLE IF NOT EXISTS categories (
    category_id INT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(100) NOT NULL UNIQUE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS items (
    item_id              INT AUTO_INCREMENT PRIMARY KEY,
    category_id          INT NOT NULL,
    description          VARCHAR(255),
    condition_label      VARCHAR(50),
    condition_multiplier DECIMAL(4,2),
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (category_id) REFERENCES categories(category_id),
    INDEX idx_items_created_at (created_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS listings_sampled (
    listing_id      INT AUTO_INCREMENT PRIMARY KEY,
    item_id         INT NOT NULL,
    -- NULL when the source page didn't expose an ID (some scraped cards)
    ebay_listing_id VARCHAR(100),
    sampled_price   DECIMAL(10,2),
    sampled_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE,
    CHECK (sampled_price >= 0)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS price_estimates (
    estimate_id     INT AUTO_INCREMENT PRIMARY KEY,
    item_id         INT NOT NULL,
    market_median   DECIMAL(10,2),
    estimated_price DECIMAL(10,2),
    -- Where the market data came from. 'browse_api' = active asking prices,
    -- 'sold_scrape' = completed/sold prices, 'mock' = built-in catalog used
    -- when eBay is unreachable. Analytics queries exclude 'mock' rows so
    -- placeholder numbers never skew real trends.
    source          VARCHAR(20) NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (item_id) REFERENCES items(item_id) ON DELETE CASCADE,
    INDEX idx_estimates_created_at (created_at),
    CHECK (source IN ('browse_api', 'sold_scrape', 'mock'))
) ENGINE=InnoDB;
