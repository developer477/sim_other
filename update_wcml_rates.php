#!/usr/bin/env php
<?php
declare(strict_types=1);

/** Sync PostgreSQL USD quotes to WCML's serialized MySQL option. */
function positive_rate($value, string $label): float {
    if (!is_numeric($value) || !is_finite((float) $value) || (float) $value <= 0) {
        throw new RuntimeException("Missing or invalid rate: $label");
    }
    return (float) $value;
}

function decode_settings(string $raw): array {
    $settings = @unserialize($raw, ['allowed_classes' => false]);
    if (!is_array($settings) || empty($settings['currency_options']) || !is_array($settings['currency_options'])) {
        throw new RuntimeException('Unrecognized _wcml_settings: expected serialized currency_options.');
    }
    // Reject objects anywhere in the option rather than reserializing incomplete objects.
    $check = function ($value, int $depth = 0) use (&$check): void {
        if ($depth > 64) {
            throw new RuntimeException('WCML settings are recursive or excessively nested.');
        }
        if (is_object($value) || is_resource($value)) {
            throw new RuntimeException('Unsupported object in WCML settings.');
        }
        if (is_array($value)) {
            foreach ($value as $child) {
                $check($child, $depth + 1);
            }
        }
    };
    $check($settings);
    return $settings;
}

function plan_rates(array $settings, string $base, array $row): array {
    $exchange = $settings['multi_currency']['exchange_rates'] ?? [];
    if (!empty($exchange['automatic'])) {
        throw new RuntimeException('Disable WCML automatic exchange-rate updates before using this script.');
    }
    if (!preg_match('/^[A-Z]{3}$/D', $base)) {
        throw new RuntimeException('Invalid WooCommerce base currency.');
    }
    $quote = static function (string $currency) use ($row): float {
        return $currency === 'USD' ? 1.0 : positive_rate($row['usd_' . strtolower($currency)] ?? null, $currency);
    };
    $denominator = $quote($base);
    // Match WCML's own service conversion: apply its lifting charge and round to six decimals.
    $charge = $exchange['lifting_charge'] ?? 0;
    if (!is_numeric($charge) || !is_finite((float) $charge) || (float) $charge < 0) {
        throw new RuntimeException('Invalid WCML lifting charge.');
    }
    $changes = [];
    foreach ($settings['currency_options'] as $currency => &$options) {
        if (!is_string($currency) || !preg_match('/^[A-Z]{3}$/D', $currency) || !is_array($options)) {
            throw new RuntimeException('Invalid WCML currency configuration.');
        }
        if ($currency === $base) {
            continue;
        }
        $rate = positive_rate(round($quote($currency) / $denominator * (1 + (float) $charge / 100), 6), $currency);
        $old = $options['rate'] ?? null;
        if (!is_numeric($old) || (float) $old !== $rate) {
            $changes[$currency] = ['old' => $old, 'new' => $rate];
            $options['previous_rate'] = $old;
            $options['rate'] = $rate;
        }
    }
    unset($options);
    return [$settings, $changes];
}

function latest_rates(PDO $pg, int $maxAge): array {
    // CURRENT_DATE uses the source database's timezone, not the cron host's timezone.
    $rows = $pg->query('SELECT r.*, CURRENT_DATE - r.date AS source_age_days '
        . 'FROM exchange_rate_daily r ORDER BY r.date DESC NULLS LAST LIMIT 2')->fetchAll(PDO::FETCH_ASSOC);
    if (!$rows || $rows[0]['date'] === null || $rows[0]['source_age_days'] === null) {
        throw new RuntimeException('No dated exchange rates found.');
    }
    if (isset($rows[1]) && $rows[0]['date'] === $rows[1]['date']) {
        throw new RuntimeException('Multiple exchange-rate rows for the latest date.');
    }
    $age = (int) $rows[0]['source_age_days'];
    if ($age < 0 || $age > $maxAge) {
        throw new RuntimeException("Exchange-rate date {$rows[0]['date']} is future-dated or older than $maxAge days.");
    }
    return $rows[0];
}

function load_config(PDO $pg): array {
    $query = $pg->prepare('SELECT key, value FROM config_values WHERE name = :name');
    $query->execute(['name' => 'wcml_exchange_rates']);
    $config = [];
    foreach ($query->fetchAll(PDO::FETCH_ASSOC) as $row) {
        if (array_key_exists($row['key'], $config)) {
            throw new RuntimeException('Duplicate wcml_exchange_rates config key: ' . $row['key']);
        }
        $config[$row['key']] = $row['value'];
    }
    foreach (['mysql_dsn', 'mysql_user', 'mysql_password'] as $key) {
        if (!isset($config[$key]) || !is_string($config[$key])) {
            throw new RuntimeException("Missing config_values entry: wcml_exchange_rates/$key");
        }
    }
    if (isset($config['max_age_days'])) {
        $days = filter_var($config['max_age_days'], FILTER_VALIDATE_INT, ['options' => ['min_range' => 0]]);
        if ($days === false) {
            throw new RuntimeException('max_age_days must be a nonnegative integer.');
        }
        $config['max_age_days'] = $days;
    }
    return $config;
}

function run_sync(PDO $pg, array $config, bool $apply): void {
    $prefix = $config['mysql_prefix'] ?? 'wp_';
    if (!is_string($prefix) || !preg_match('/^[A-Za-z0-9_]+$/D', $prefix)) {
        throw new RuntimeException('Invalid MySQL table prefix.');
    }
    $maxAge = $config['max_age_days'] ?? 3;
    if (!is_int($maxAge) || $maxAge < 0) {
        throw new RuntimeException('max_age_days must be a nonnegative integer.');
    }
    $pdoOptions = [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION, PDO::ATTR_EMULATE_PREPARES => false];
    if (strpos($config['mysql_dsn'], 'mysql:') !== 0) {
        throw new RuntimeException('mysql_dsn must start with mysql:.');
    }
    $mysql = new PDO($config['mysql_dsn'], $config['mysql_user'], $config['mysql_password'], $pdoOptions);
    $table = '`' . $prefix . 'options`';
    $values = $mysql->query("SELECT option_name, option_value FROM $table "
        . "WHERE option_name IN ('home', 'woocommerce_currency', '_wcml_settings')")->fetchAll(PDO::FETCH_KEY_PAIR);
    $host = parse_url($values['home'] ?? '', PHP_URL_HOST);
    if (!is_string($host) || strcasecmp($host, $config['expected_host'] ?? 'tsim.mobi') !== 0) {
        throw new RuntimeException('WordPress home host does not match expected_host.');
    }
    $raw = $values['_wcml_settings'] ?? '';
    $settings = decode_settings($raw);
    $base = $values['woocommerce_currency'] ?? '';
    $row = latest_rates($pg, $maxAge);
    [$updated, $changes] = plan_rates($settings, $base, $row);
    printf("%s source=%s host=%s base=%s lifting_charge=%s%%\n", $apply ? 'APPLY' : 'DRY RUN',
        $row['date'], $host, $base, $settings['multi_currency']['exchange_rates']['lifting_charge'] ?? 0);
    foreach ($changes as $currency => $change) {
        printf("%s: %s -> %.6f\n", $currency, $change['old'] ?? '(missing)', $change['new']);
    }
    if (!$changes) {
        echo "Rates already match; no write.\n";
        return;
    }
    if (!$apply) {
        echo "No writes made. Run with --apply to save.\n";
        return;
    }
    $encoded = serialize($updated);
    // One atomic compare-and-swap prevents overwriting settings edited since our read.
    // Include the base currency and site identity in the same guarded statement.
    $write = $mysql->prepare("UPDATE $table AS settings "
        . "JOIN $table AS base ON base.option_name = 'woocommerce_currency' "
        . "JOIN $table AS home ON home.option_name = 'home' "
        . "SET settings.option_value = ? WHERE settings.option_name = '_wcml_settings' "
        . 'AND BINARY settings.option_value = BINARY ? AND BINARY base.option_value = BINARY ? '
        . 'AND BINARY home.option_value = BINARY ?');
    $write->execute([$encoded, $raw, $base, $values['home']]);
    if ($write->rowCount() !== 1) {
        throw new RuntimeException('Concurrent settings change detected; no update applied. Retry after checking the site.');
    }
    $saved = $mysql->query("SELECT option_value FROM $table WHERE option_name = '_wcml_settings'")->fetchColumn();
    if ($saved !== $encoded) {
        throw new RuntimeException('Write occurred, but read-back differs; another writer may have changed WCML settings.');
    }
    echo 'Saved and verified ' . count($changes) . " rate(s). Purge persistent WordPress options cache if enabled.\n";
}

function main(array $args): int {
    $apply = false;
    foreach (array_slice($args, 1) as $arg) {
        if ($arg === '--help' || $arg === '-h') {
            echo "Usage: php update_wcml_rates.php [--apply]\nDefaults to dry run. Config: PostgreSQL config_values, name=wcml_exchange_rates.\n";
            return 0;
        } elseif ($arg === '--apply') {
            $apply = true;
        } else {
            throw new RuntimeException('Unexpected argument. Use --help.');
        }
    }
    // Let libpq handle connection environment variables and .pgpass normally.
    foreach (['PGHOST' => '/tmp', 'PGDATABASE' => 'e2fax', 'PGUSER' => 'domains', 'PGCONNECT_TIMEOUT' => '10'] as $key => $default) {
        if (getenv($key) === false) {
            putenv("$key=$default");
        }
    }
    $pg = new PDO('pgsql:', null, null, [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
    $config = load_config($pg);
    run_sync($pg, $config, $apply);
    return 0;
}

if (PHP_SAPI === 'cli' && realpath($_SERVER['SCRIPT_FILENAME'] ?? '') === __FILE__) {
    try {
        exit(main($argv));
    } catch (PDOException $e) {
        // Avoid leaking connection details or passwords into cron mail/logs.
        fwrite(STDERR, 'Database operation failed (SQLSTATE ' . $e->getCode() . "). Check connectivity, grants and schema.\n");
        exit(1);
    } catch (Throwable $e) {
        fwrite(STDERR, $e->getMessage() . "\n");
        exit(1);
    }
}
