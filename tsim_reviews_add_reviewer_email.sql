-- Existing installations; email remains excluded from the public view.
BEGIN;
ALTER TABLE tsim_website_reviews ADD COLUMN IF NOT EXISTS reviewer_email text;
COMMIT;
