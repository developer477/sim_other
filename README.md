# sim_other

SIM-related code that does not belong in the other Simmis folders.

## Approved WooCommerce review import (#7945)

`tsim_sync_reviews.py` imports approved website reviews once into
`tsim_website_reviews`. Each run lists approved IDs and ratings, excludes rating-0
entries (including replies), then downloads content only for IDs absent from
PostgreSQL. Creation dates and maximum IDs are not
watermarks: an older pending review can be approved later. Existing imports are
never updated or deleted. Amazon reviews are not imported.

Requires Python 3.7+, `psycopg2`, and PostgreSQL 12+. Connection defaults are
database `e2fax`, user `domains`, socket `/tmp`; standard libpq environment
variables and `.pgpass` can override them. Authentication uses HTTPS Basic auth;
credentials, response bodies and reviewer data are not logged. Redirects are
rejected rather than forwarding credentials.

Credentials come from `config_values` with these exact names and keys:

| name | Consumer key | Consumer secret |
| --- | --- | --- |
| `www.tsim.in` | `TSIM_IN_API_consumer_key` | `TSIM_IN_API_consumer_secret` |
| `www.tsim.mobi` | `TSIM_MOBI_API_consumer_key` | `TSIM_MOBI_API_consumer_secret` |

Create a fresh table using `tsim_reviews_schema.sql` as the table-owning database
role. When replacing an existing installation, drop `tsim_public_website_reviews`
before dropping `tsim_website_reviews`, then apply the schema. The
schema creates `tsim_website_reviews` and the `tsim_public_website_reviews` view.
Grant the worker SELECT on `config_values` and `shopshastra_products_latest`,
SELECT/INSERT on the review table, and usage on its identity
sequence if its role differs from the owner.

For each batch, parent CID comes from `shopshastra_products_latest.meta_data`
using the exact `domainname` and review `product_id`, with `parent_id = 0`.
The metadata entry with key `cid` populates the stored `cid` column.
No product API calls or separate mapping table are needed. Missing or ambiguous
CIDs remain NULL and are excluded by the public view. Previously imported rows
are not revisited; existing NULL CIDs can be backfilled separately later.
The importer stores `_review_variation_id`, `_review_variation_sku`, and
`_review_variation_name` directly from review `meta_data`, without looking up
child products or matching attributes. Missing legacy metadata stays NULL.
The literal legacy placeholder `none` also becomes NULL for these three fields.
Install `tsim_review_rest_metadata.php` in each website's existing review snippet
to expose these keys in authenticated WooCommerce review responses. The hook
skips metadata reads for discovery requests selecting only IDs and ratings.
The single-review and importer list requests were verified on `www.tsim.in`
for review `1187603`; the hook still needs installing/verifying on `www.tsim.mobi`.
No direct MySQL connection is required.
`locale` remains NULL until a source supplies it.
The table, index, and public view use stored `cid` directly, without catalog
joins. Unused `canonical_parent`, `variation_attributes`, and
`canonical_variation` columns have been removed.
Reviewer email is stored internally and excluded from the public view and logs.
Previously imported rows are not downloaded again.

```sh
psql -X -v ON_ERROR_STOP=1 -f tsim_reviews_schema.sql
python3 tsim_sync_reviews.py                         # report only, no DB writes
python3 tsim_sync_reviews.py --site www.tsim.in       # one site, report only
python3 tsim_sync_reviews.py --apply                 # insert missing reviews
python3 -B -m unittest discover -s tests -p 'test_tsim_sync_reviews.py' -v
```

Report mode downloads missing review details for validation but starts a read-only
database session. Apply commits each valid record independently; a failed record
remains absent and is retried next run. Source identity has a unique constraint,
and concurrent/repeated imports cannot overwrite content. Each source runs
independently; record/site failures produce a nonzero exit status. Structured
stdout summaries include approved/missing/ready/imported/unmapped/failed/deferred
counts. IDs no longer approved between listing and detail retrieval are deferred.
Changes during ID pagination may defer an ID until the next complete scan.
Record failures include validation/insertion stage, exception type, safe reason,
and PostgreSQL SQLSTATE when available. Ratings must be 1–5. Discovery summaries
count excluded rating-0 entries separately; their content is not downloaded.

Run an initial report and verify catalog CID resolution before applying. An operator may schedule
the command every 15 minutes after validating production volume and permissions.
No scheduler, schema application, or production import is installed automatically.
Public review APIs and app changes belong to the separate display issue #7946.

## Customer feedback notifications

`feedback_email.py` queues requests in `tsim_notification`; the existing
`tsim_send_notification.py` delivers them. It requires Python 3 and `psycopg2`,
and connects to local PostgreSQL database `e2fax` as `domains`.

All plans require a usage record updated within the existing two-month window.
Daily (`gbpd`) and unlimited (`unl` / `unlimited`) SKUs bypass the consumption
test, including zero totals. Fixed bundles require at least 35% consumption of a
positive allowance. The recency check applies to every plan to avoid selecting
a historical backlog of daily/unlimited trips.
Requests become eligible two days after `from_date + validity`. Existing email
exclusions and duplicate suppression apply to the whole request.

Users present in `ta_notification_id` receive email and push together, with
aligned arrays and independent pending statuses. Other customers receive an
email inviting them to install the app. Optional `config_values` keys:

| Audience/channel | Subject key | Body key |
| --- | --- | --- |
| App user email | `feedback_email_subject_v2` | `feedback_email_message_v2` |
| Other customer email | `feedback_email_non_app_subject` | `feedback_email_non_app_message` |
| Push | `feedback_push_subject` | `feedback_push_message` |

Missing templates use built-in fallbacks. Templates support `<customer_name>`
and `<review_link>`; the link uses the customer's TSIM domain. The app does not
handle notification taps, so the default push asks users to rate TSIM in their
app store without including a URL. Keep configured push templates free of URLs
and instructions to tap the notification.

```sh
python3 feedback_email.py --dry-run
python3 -B -m unittest discover -s tests -p 'test_feedback_email.py' -v
```

Dry-run reads data and writes its normal log, but does not insert notifications,
update the exception file, or send the summary email. Local tests mock database
and mail access. Running without `--dry-run` queues notifications and sends an
operator summary; no new delivery scheduler is required.

## WCML exchange-rate sync

`update_wcml_rates.php` runs as `domains` on the PostgreSQL/MySQL server. Requires
PHP 8.0+ CLI with `pdo_pgsql` and `pdo_mysql`; no WordPress files or WP-CLI required.
It reads the newest `exchange_rate_daily` row and updates the serialized
`_wcml_settings` option in the configured WordPress MySQL database.

### Configuration

PostgreSQL defaults: database `e2fax`, user `domains`, socket directory `/tmp`.
Standard libpq variables (`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`)
and `.pgpass` can override the connection. `domains` needs SELECT on
`config_values` and `exchange_rate_daily`; PostgreSQL is never written by the script.

Store these entries in PostgreSQL `config_values`, all with
`name = 'wcml_exchange_rates'`. Use actual MySQL credentials and the site's prefix:

| key | value / default |
| --- | --- |
| `mysql_dsn` | Required, e.g. `mysql:host=localhost;dbname=YOUR_WP_DB;charset=utf8mb4` |
| `mysql_user` | Required; user with SELECT and UPDATE on the site's options table |
| `mysql_password` | Required; the MySQL user's password (empty string allowed) |
| `mysql_prefix` | `wp_` |
| `max_age_days` | `3`; reject older or future-dated source rows |

Example insertion structure (replace placeholders; do not insert duplicates):

```sql
INSERT INTO config_values (name, key, value) VALUES
('wcml_exchange_rates', 'mysql_dsn', 'mysql:host=localhost;dbname=YOUR_WP_DB;charset=utf8mb4'),
('wcml_exchange_rates', 'mysql_user', 'YOUR_MYSQL_USER'),
('wcml_exchange_rates', 'mysql_password', 'YOUR_MYSQL_PASSWORD'),
('wcml_exchange_rates', 'mysql_prefix', 'wp_');
```

For a specific MySQL socket use `mysql:unix_socket=/path/to/mysql.sock;dbname=YOUR_WP_DB;charset=utf8mb4`.
Keep WCML's built-in automatic API updates disabled; the script refuses to compete with them.
The MySQL database and table prefix select the target site. WordPress's stored
`home` URL is not read or checked; any existing `expected_host` entry is unused.

### Run

Run from a shell belonging to `domains`:

```sh
php /path/to/sim_other/update_wcml_rates.php          # preview only
php /path/to/sim_other/update_wcml_rates.php --apply  # save and read back
```

Schedule in the `domains` crontab after the daily PostgreSQL rates are refreshed,
for example (adjust PHP/script paths and timing):

```cron
15 6 * * * /usr/bin/php /path/to/sim_other/update_wcml_rates.php --apply
```

Output lists source date, base currency and old/new rates; errors exit nonzero.
Caught failures, including dry-run failures, also send an email to
`services@tsim.in` and `deven@tsim.in` using PHP's `mail()` and the server's local
mail transport. Alerts include the server, UTC time, mode and error, but no database
credentials or raw database exception messages. If submission fails, the script
reports that on stderr and still exits nonzero. Successful runs send no email.
Mail transport must be configured for the `domains` user; acceptance by `mail()`
does not confirm delivery. Interpreter startup errors and forced termination
cannot be reported by this handler.
The script changes only already-configured secondary currencies. It calculates
`usd_target / usd_base` (USD itself is 1), applies the existing WCML lifting charge,
and rounds to six decimals, matching WCML's service behavior. A missing or invalid
required quote aborts the whole update. Unused source currencies are ignored.

Unrelated settings, formatting and product-specific fixed prices are preserved.
Changed currencies retain their old value in `previous_rate`; an identical rerun
does not write. A single conditional MySQL update detects concurrent settings or
base currency changes and aborts instead of overwriting them.
The built-in API service's `last_updated` timestamp is left unchanged; use this
job's output for the PostgreSQL source date and sync result.

**Persistent cache:** direct MySQL writes do not invalidate WordPress's object cache.
If the site uses Redis/Memcached or another persistent object cache, arrange to purge
its `options` group (including `alloptions` and `_wcml_settings`) after the job.
Without this, the database may show new rates while WordPress still uses old ones.
Page caches may also need a refresh. No cache purge is implemented in this script.

Settings format and conversion checked against the official WordPress.org WCML
5.5.8 source (`class-wcml-exchange-rates.php` and `class-woocommerce-wpml.php`):
[plugin download](https://downloads.wordpress.org/plugin/woocommerce-multilingual.5.5.8.zip).
WCML documents its rates as relative to the shop's default currency:
[exchange-rate documentation](https://wpml.org/wcml-hook/wcml_exchange_rates/).

### Local checks

```sh
php -l update_wcml_rates.php
php tests/test_wcml_rates.php
```

Tests cover conversions, setting preservation, repeat runs and invalid source data.
They do not connect to MySQL or PostgreSQL. Run the dry run on the server before
enabling the scheduled update; deployed plugin compatibility and database writes
have not been verified against tsim.mobi.
