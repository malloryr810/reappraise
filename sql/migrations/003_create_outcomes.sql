-- What happened to an appraised item: the price a volunteer actually set
-- (final_price) and, once it sells, what it sold for (sold_price). Accuracy is
-- measured against sold_price only; final_price is set after the volunteer has
-- seen the estimate, so it's reported as agreement, never as accuracy.
-- One outcome per estimate (the UNIQUE key doubles as the estimate_id index), so
-- an item can't be counted twice. Deleting an estimate deletes its outcome.
-- rollback: DROP TABLE IF EXISTS outcomes;
CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id  INT AUTO_INCREMENT PRIMARY KEY,
    estimate_id INT NOT NULL,
    final_price DECIMAL(10,2) NOT NULL,
    sold_price  DECIMAL(10,2) NULL,
    sold_at     DATETIME NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (estimate_id) REFERENCES price_estimates(estimate_id) ON DELETE CASCADE,
    UNIQUE KEY uq_outcomes_estimate_id (estimate_id),
    -- Percentage error divides by the actual price, so zero isn't allowed
    CHECK (final_price > 0),
    CHECK (sold_price IS NULL OR sold_price > 0)
) ENGINE=InnoDB;
