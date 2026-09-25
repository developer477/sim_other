# sim_other

SIM-related code that does not belong in the other Simmis folders.

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
