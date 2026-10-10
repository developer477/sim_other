-- Existing installations: removes only the obsolete mapping table.
BEGIN;
DROP TABLE IF EXISTS tsim_review_product_mapping RESTRICT;
COMMIT;
