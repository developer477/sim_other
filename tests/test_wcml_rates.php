<?php
declare(strict_types=1);
require __DIR__ . '/../update_wcml_rates.php';

$checks = 0;
function check(bool $result, string $message): void {
    global $checks;
    if (!$result) {
        throw new RuntimeException($message);
    }
    $checks++;
}
function rejects(callable $action, string $message): void {
    try {
        $action();
    } catch (RuntimeException $e) {
        check(true, $message);
        return;
    }
    throw new RuntimeException($message);
}

$source = ['usd_inr' => '95.9630274', 'usd_aud' => '1.426534', 'usd_eur' => '0.87907'];
$settings = [
    'currency_options' => [
        'USD' => ['rate' => 1, 'position' => 'left'],
        'INR' => ['rate' => '90', 'rounding' => 'up'],
        'EUR' => ['rate' => '0.8', 'num_decimals' => 2],
    ],
    'multi_currency' => ['exchange_rates' => ['automatic' => 0, 'lifting_charge' => 0]],
    'unrelated' => ['label' => '₹ café €', 'enabled' => true],
];
[$updated, $changes] = plan_rates(decode_settings(serialize($settings)), 'USD', $source);
check($updated['currency_options']['INR']['rate'] === 95.963027, 'USD to INR quote');
check($updated['currency_options']['EUR']['rate'] === 0.879070, 'USD to EUR quote');
check($updated['currency_options']['INR']['previous_rate'] === '90', 'Preserve previous rate');
check($updated['currency_options']['USD'] === $settings['currency_options']['USD'], 'Preserve base currency');
check($updated['unrelated'] === $settings['unrelated'], 'Preserve unrelated serialized settings');
check($updated['currency_options']['INR']['rounding'] === 'up', 'Preserve rounding');
check(!isset($updated['currency_options']['AUD']), 'Do not enable source-only currencies');
check(decode_settings(serialize($updated)) === $updated, 'PHP serialization round trip');
[$again, $repeat] = plan_rates($updated, 'USD', $source);
check($repeat === [] && $again === $updated, 'Identical rerun must preserve previous_rate and avoid writes');
[$inr] = plan_rates($settings, 'INR', $source);
check($inr['currency_options']['USD']['rate'] === 0.010421, 'INR base to USD reciprocal');
check($inr['currency_options']['EUR']['rate'] === 0.009161, 'INR base to EUR cross rate');
$charged = $settings;
$charged['multi_currency']['exchange_rates']['lifting_charge'] = '2';
[$withCharge] = plan_rates($charged, 'USD', $source);
check($withCharge['currency_options']['EUR']['rate'] === 0.896651, 'Honor WCML lifting charge');
foreach ([null, 0, -1, 'NaN', INF, 'garbage'] as $badRate) {
    rejects(fn() => plan_rates($settings, 'USD', array_merge($source, ['usd_eur' => $badRate])), 'Reject invalid needed quote');
}
rejects(fn() => plan_rates($settings, 'JPY', $source), 'Reject missing base quote');
$unsupported = $settings;
$unsupported['currency_options']['CHF'] = ['rate' => 1];
rejects(fn() => plan_rates($unsupported, 'USD', $source), 'Reject missing configured currency rather than partially update');
$automatic = $settings;
$automatic['multi_currency']['exchange_rates']['automatic'] = 1;
rejects(fn() => plan_rates($automatic, 'USD', $source), 'Reject competing API updater');
rejects(fn() => decode_settings('bad data'), 'Reject malformed serialization');
$objects = $settings;
$objects['unrelated'] = new stdClass();
rejects(fn() => decode_settings(serialize($objects)), 'Reject serialized objects');

// Exercise source-date validation with fixture rows; no production database access.
class Rows extends PDOStatement {
    private array $rows;
    public function __construct(array $rows) { $this->rows = $rows; }
    public function fetchAll(int $mode = PDO::FETCH_DEFAULT, mixed ...$args): array { return $this->rows; }
}
class Source extends PDO {
    private array $rows;
    public function __construct(array $rows) { $this->rows = $rows; }
    public function query(string $query, ?int $fetchMode = null, mixed ...$args): PDOStatement|false { return new Rows($this->rows); }
}
$dated = ['date' => '2026-09-25', 'source_age_days' => 0] + $source;
check(latest_rates(new Source([$dated]), 3) === $dated, 'Accept current source');
foreach ([[], [array_replace($dated, ['source_age_days' => 4])], [array_replace($dated, ['source_age_days' => -1])], [$dated, $dated]] as $rows) {
    rejects(fn() => latest_rates(new Source($rows), 3), 'Reject empty, stale, future or ambiguous source');
}
echo "Passed $checks checks.\n";
