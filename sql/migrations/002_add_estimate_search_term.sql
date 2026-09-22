-- The eBay query that produced an estimate and where it came from. A volunteer's
-- description can be given yet still fall back to Vision labels when it finds too
-- few listings, so the source can't be inferred from items.user_description.
-- Both stay NULL for mock estimates and for estimates made before this migration.
ALTER TABLE price_estimates
    ADD COLUMN search_term   VARCHAR(255) NULL AFTER source,
    ADD COLUMN search_source VARCHAR(20)  NULL AFTER search_term,
    ADD CONSTRAINT chk_estimates_search_source
        CHECK (search_source IN ('user_description', 'vision_label'));
