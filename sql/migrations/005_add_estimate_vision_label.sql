-- The Vision label behind a vision_label search. With the label taxonomy the
-- searched term can be a canonical form ("bicycle" for "Road bicycle"), so the
-- label Vision actually returned is kept alongside it. NULL for description
-- searches, mock estimates, and estimates made before this migration.
-- rollback: ALTER TABLE price_estimates DROP COLUMN vision_label;
ALTER TABLE price_estimates
    ADD COLUMN vision_label VARCHAR(255) NULL AFTER search_source;
