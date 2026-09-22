-- What a volunteer typed about the item (brand, model, specifics), kept apart
-- from `description`, which records Vision's labels. Keeping both preserves
-- what the model detected versus what a person corrected it to.
ALTER TABLE items
    ADD COLUMN user_description VARCHAR(255) NULL AFTER description;
