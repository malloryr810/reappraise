-- Vision label taxonomy, read by labels.py: whether a label names an object
-- worth searching, and for objects the canonical term eBay is searched for.
-- Labels not listed here are treated as objects and searched as given.
--   generic  - catch-alls ("gadget"); a one-word entry also matches as a word
--              inside a label, as the original hard-coded denylist did
--   material - only matches the whole label: "plastic" is skipped, "plastic
--              bottle" is still searched
--   color    - same whole-label rule as material
--   object   - searched as canonical_term; labels sharing one are searched once
-- Labels are stored lower case; lookups ignore case.
-- The seed uses an upsert, so re-running this file after a partial failure is
-- safe (the CREATE auto-commits before the INSERT runs).
-- rollback: DROP TABLE IF EXISTS labels;
CREATE TABLE IF NOT EXISTS labels (
    label          VARCHAR(100) PRIMARY KEY,
    canonical_term VARCHAR(100) NULL,
    kind           ENUM('object', 'material', 'color', 'generic') NOT NULL,
    -- an object needs a non-blank term, and no other kind may have one
    CONSTRAINT chk_labels_canonical_term
        CHECK ((kind = 'object') = (canonical_term IS NOT NULL AND TRIM(canonical_term) <> ''))
) ENGINE=InnoDB;

INSERT INTO labels (label, canonical_term, kind) VALUES
    -- the original denylist from ebay.py
    ('gadget', NULL, 'generic'),
    ('technology', NULL, 'generic'),
    ('product', NULL, 'generic'),
    ('object', NULL, 'generic'),
    ('item', NULL, 'generic'),
    ('equipment', NULL, 'generic'),
    ('supplies', NULL, 'generic'),
    -- other catch-alls Vision commonly returns
    ('electronic device', NULL, 'generic'),
    ('material property', NULL, 'generic'),
    ('font', NULL, 'generic'),
    ('rectangle', NULL, 'generic'),
    -- materials
    ('plastic', NULL, 'material'),
    ('wood', NULL, 'material'),
    ('hardwood', NULL, 'material'),
    ('plywood', NULL, 'material'),
    ('wood stain', NULL, 'material'),
    ('metal', NULL, 'material'),
    ('steel', NULL, 'material'),
    ('stainless steel', NULL, 'material'),
    ('aluminium', NULL, 'material'),
    ('aluminum', NULL, 'material'),
    ('glass', NULL, 'material'),
    ('leather', NULL, 'material'),
    ('fabric', NULL, 'material'),
    ('textile', NULL, 'material'),
    ('cotton', NULL, 'material'),
    ('wool', NULL, 'material'),
    ('rubber', NULL, 'material'),
    ('paper', NULL, 'material'),
    ('cardboard', NULL, 'material'),
    ('ceramic', NULL, 'material'),
    -- colours
    ('red', NULL, 'color'),
    ('orange', NULL, 'color'),
    ('yellow', NULL, 'color'),
    ('green', NULL, 'color'),
    ('blue', NULL, 'color'),
    ('electric blue', NULL, 'color'),
    ('azure', NULL, 'color'),
    ('aqua', NULL, 'color'),
    ('teal', NULL, 'color'),
    ('turquoise', NULL, 'color'),
    ('purple', NULL, 'color'),
    ('violet', NULL, 'color'),
    ('magenta', NULL, 'color'),
    ('pink', NULL, 'color'),
    ('brown', NULL, 'color'),
    ('beige', NULL, 'color'),
    ('tan', NULL, 'color'),
    ('black', NULL, 'color'),
    ('white', NULL, 'color'),
    ('grey', NULL, 'color'),
    ('gray', NULL, 'color'),
    ('silver', NULL, 'color'),
    ('gold', NULL, 'color'),
    -- canonical merges
    ('road bicycle', 'bicycle', 'object'),
    ('mountain bike', 'bicycle', 'object'),
    ('hybrid bicycle', 'bicycle', 'object'),
    ('bike', 'bicycle', 'object'),
    ('couch', 'sofa', 'object'),
    ('studio couch', 'sofa', 'object'),
    ('mobile phone', 'smartphone', 'object')
AS new
ON DUPLICATE KEY UPDATE canonical_term = new.canonical_term, kind = new.kind;
