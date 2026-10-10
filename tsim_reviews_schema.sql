BEGIN;

CREATE TABLE tsim_website_reviews (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_site text NOT NULL CHECK (source_site IN ('www.tsim.in', 'www.tsim.mobi')),
    source_review_id bigint NOT NULL,
    source_product_id bigint NOT NULL,
    canonical_parent text,
    cid text,
    source_variation_id bigint,
    variation_sku text,
    variation_name text,
    variation_attributes jsonb,
    canonical_variation text,
    rating smallint NOT NULL CHECK (rating BETWEEN 1 AND 5),
    review_text text NOT NULL,
    reviewer_name text NOT NULL,
    reviewer_email text,
    verified_purchase boolean NOT NULL DEFAULT false,
    locale text,
    regional_origin text GENERATED ALWAYS AS (
        CASE source_site WHEN 'www.tsim.in' THEN 'IN' ELSE 'ROW' END
    ) STORED,
    source_created_at timestamptz NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_site, source_review_id)
);

CREATE INDEX tsim_website_reviews_parent_region_idx
    ON tsim_website_reviews (canonical_parent, regional_origin, source_created_at DESC);

-- Public reads must use this view, excluding unresolved parents.
CREATE VIEW tsim_public_website_reviews AS
    SELECT id, source_site, source_review_id, source_product_id,
           canonical_parent, cid, source_variation_id, variation_sku,
           variation_name, variation_attributes, canonical_variation,
           rating, review_text, reviewer_name, verified_purchase, locale,
           regional_origin, source_created_at, imported_at
    FROM tsim_website_reviews
    WHERE canonical_parent IS NOT NULL;

COMMIT;
