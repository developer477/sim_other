<?php

declare(strict_types=1);

date_default_timezone_set('Asia/Kolkata');

const DB_HOST = '127.0.0.1';
const DB_PORT = '5432';
const DB_NAME = 'e2fax';
const DB_USER = 'domains';
const DB_PASSWORD = 'your_password';

const BALANCE_COMMAND = '/usr/local/bin/php /usr/local/ownmail/bin/quad_call_with_return.php getbalance';
const PACKLIST_COMMAND = '/usr/local/bin/php /usr/local/ownmail/bin/quad_call_with_return.php qrypacklist';
const DELPACK_COMMAND = '/usr/local/bin/php /usr/local/ownmail/bin/quad_call_with_return.php delpack';
const REFILL_COMMAND = '/usr/local/bin/php /usr/local/ownmail/bin/tsim_quad_refill.php';
const ALERT_THRESHOLD_PERCENT = 3.0;
const LOG_FILE = '/tmp/tsim_quad_check_recharge.log';

function stderr(string $message): void
{
    fwrite(STDERR, $message . PHP_EOL);
}

function logMessage(string $context, string $status, string $message): void
{
    $timestamp = (new DateTimeImmutable('now'))->format('Y-m-d H:i:s');
    $line = sprintf("[%s] context=%s status=%s %s\n", $timestamp, $context, $status, $message);
    @file_put_contents(LOG_FILE, $line, FILE_APPEND);
}

function connectDatabase()
{
    $connString = sprintf(
        'host=%s port=%s dbname=%s user=%s password=%s',
        DB_HOST,
        DB_PORT,
        DB_NAME,
        DB_USER,
        DB_PASSWORD
    );

    $connection = @pg_connect($connString);

    if ($connection === false) {
        return false;
    }

    return $connection;
}

function parseRefillFilterArguments(array $arguments): array
{
    $filters = ['order_id' => null, 'transaction_id' => null];

    for ($index = 0; $index < count($arguments); $index++) {
        $argument = $arguments[$index];
        if (strpos($argument, '--') === 0) {
            $option = explode('=', substr($argument, 2), 2);
            $name = $option[0];
            if (!array_key_exists($name, $filters)) {
                throw new InvalidArgumentException('Usage: php tsim_quad_check_recharge.php [--order_id=ORDER_ID | ORDER_ID] [--transaction_id=TRANSACTION_ID]');
            }
            $value = $option[1] ?? ($arguments[++$index] ?? '');
        } else {
            $name = 'order_id';
            $value = $argument;
        }

        $value = trim($value);
        if ($value === '' || strpos($value, '-') === 0) {
            throw new InvalidArgumentException($name . ' must have a non-empty value.');
        }
        if ($filters[$name] !== null) {
            throw new InvalidArgumentException($name . ' may only be supplied once.');
        }
        $filters[$name] = $value;
    }

    return $filters;
}

function fetchPendingRefills($dbConnection, ?string $orderId = null, ?string $transactionId = null): array
{
    $parameters = [];
    $filters = [];
    if ($orderId !== null) {
        $parameters[] = $orderId;
        $filters[] = '  AND s.order_no = $' . count($parameters);
    }
    if ($transactionId !== null) {
        $parameters[] = $transactionId;
        $filters[] = '  AND r.transaction_id = $' . count($parameters);
    }
    $filterSql = implode(PHP_EOL, $filters);
    $sql = <<<SQL
SELECT
    r.username,
    r.domainname,
    r.tripid,
    r.amount,
    r.refill_sku,
    r.transaction_id,
    r.accept_time,
    r.refill_status,
    s.simno
FROM tsim_refill r
JOIN sim_stock_trip s ON s.tripid = r.tripid
WHERE r.refill_status = false
{$filterSql}
  AND EXISTS (
      SELECT 1
      FROM tsim_blank_sims bl
      WHERE bl.iccid = s.simno
        AND bl.vendor LIKE '454%'
  )
FOR UPDATE OF r SKIP LOCKED
SQL;

    $result = $parameters === []
        ? @pg_query($dbConnection, $sql)
        : @pg_query_params($dbConnection, $sql, $parameters);

    if ($result === false) {
        throw new RuntimeException('failed to query pending refill rows');
    }

    $rows = pg_fetch_all($result);

    return is_array($rows) ? $rows : [];
}

function addRefillFailure(array &$failures, string $tripid, string $iccid, string $reason): void
{
    $failures[] = [
        'tripid' => $tripid,
        'iccid' => $iccid,
        'reason' => $reason,
    ];
}

function sendRefillFailureEmail(array $failures): void
{
    global $send_to_these, $subj, $headers;

    if ($failures === [] || !isset($send_to_these, $subj, $headers) || trim((string) $send_to_these) === '') {
        return;
    }

    $lines = ['Refill failed for the following ICCIDs:'];

    foreach ($failures as $failure) {
        $lines[] = sprintf(
            'tripid=%s iccid=%s reason=%s',
            $failure['tripid'] !== '' ? $failure['tripid'] : 'unknown',
            $failure['iccid'] !== '' ? $failure['iccid'] : 'unknown',
            $failure['reason']
        );
    }

    $msg = implode(PHP_EOL, $lines);
    mail($send_to_these, $subj, $msg, $headers);
}

function fetchImsiByIccid($dbConnection, string $iccid): string
{
    $sql = 'SELECT imsi FROM tsim_blank_sims WHERE iccid = $1 LIMIT 1';
    $result = @pg_query_params($dbConnection, $sql, [$iccid]);

    if ($result === false) {
        throw new RuntimeException('failed to query IMSI for iccid=' . $iccid);
    }

    $row = pg_fetch_assoc($result);

    if (!is_array($row)) {
        throw new RuntimeException('no IMSI found for iccid=' . $iccid);
    }

    $imsi = trim((string) ($row['imsi'] ?? ''));

    if ($imsi === '' || $imsi === '0') {
        throw new RuntimeException('invalid IMSI for iccid=' . $iccid);
    }

    return $imsi;
}

function parseQuadDate(?string $value): ?DateTimeImmutable
{
    if ($value === null || $value === '') {
        return null;
    }

    $date = DateTimeImmutable::createFromFormat('YmdHis', $value, new DateTimeZone(date_default_timezone_get()));

    return $date ?: null;
}

function isUnlimitedPack(array $pack): bool
{
    return isset($pack['totalQuota'], $pack['remainingQuota'])
        && (int) $pack['totalQuota'] === (int) $pack['remainingQuota']
        && in_array((int) $pack['totalQuota'], [-1, -2], true);
}

function isExpired(array $pack, DateTimeImmutable $now): bool
{
    $expiry = parseQuadDate($pack['expTime'] ?? null);

    return $expiry !== null && $expiry < $now;
}

function shouldRefillForQuota(array $pack): bool
{
    if (isUnlimitedPack($pack)) {
        return false;
    }

    if (!isset($pack['totalQuota'], $pack['remainingQuota'])) {
        return false;
    }

    $total = (float) $pack['totalQuota'];
    $remaining = (float) $pack['remainingQuota'];

    if ($total <= 0) {
        return false;
    }

    return ($remaining / $total) * 100.0 < ALERT_THRESHOLD_PERCENT;
}

function buildPackSummary(array $pack): string
{
    $parts = [];
    $parts[] = 'packOrderSn=' . ($pack['packOrderSn'] ?? 'n/a');
    $parts[] = 'packCode=' . ($pack['packCode'] ?? 'n/a');
    $parts[] = 'totalQuota=' . ($pack['totalQuota'] ?? 'n/a');
    $parts[] = 'remainingQuota=' . ($pack['remainingQuota'] ?? 'n/a');
    $parts[] = 'expTime=' . ($pack['expTime'] ?? 'n/a');

    return implode(', ', $parts);
}

function selectLatestPack(array $packs): array
{
    $latestPack = null;
    $latestExpiry = null;

    foreach ($packs as $pack) {
        if (!is_array($pack)) {
            continue;
        }

        $expiry = parseQuadDate($pack['expTime'] ?? null);

        if ($latestPack === null) {
            $latestPack = $pack;
            $latestExpiry = $expiry;
            continue;
        }

        if ($expiry !== null && ($latestExpiry === null || $expiry > $latestExpiry)) {
            $latestPack = $pack;
            $latestExpiry = $expiry;
        }
    }

    if (!is_array($latestPack)) {
        throw new RuntimeException('invalid pack entry returned');
    }

    return $latestPack;
}

function areAllPacksInvalid(array $packs): bool
{
    if ($packs === []) {
        return false;
    }

    foreach ($packs as $pack) {
        if (!is_array($pack) || strcasecmp(trim((string) ($pack['state'] ?? '')), 'Invalid') !== 0) {
            return false;
        }
    }

    return true;
}

function fetchSinglePack(string $iccid): array
{
    $command = BALANCE_COMMAND . ' ' . escapeshellarg($iccid) . ' 2>&1';
    logMessage($iccid, 'balance', 'command=' . $command);
    $output = [];
    $exitCode = 0;
    exec($command, $output, $exitCode);

    logMessage($iccid, 'balance', 'raw_output=' . trim(implode(PHP_EOL, $output)));

    if ($exitCode !== 0) {
        throw new RuntimeException('provider call failed: ' . implode(' | ', $output));
    }

    $rawJson = sanitizeProviderCommandOutput($output);
    logMessage($iccid, 'balance', 'output=' . $rawJson);

    if ($rawJson === '0') {
        return ['_zeroBalance' => true];
    }

    $packs = json_decode($rawJson, true);

    if (!is_array($packs)) {
        throw new RuntimeException('invalid JSON returned: ' . $rawJson);
    }

    if ($packs === []) {
        return ['_noPacksReturned' => true];
    }

    $latestPack = selectLatestPack($packs);
    $latestPack['_allPacksInvalid'] = areAllPacksInvalid($packs);

    return $latestPack;
}

function runRefill(string $iccid, string $refillSku): string
{
    $command = REFILL_COMMAND . ' ' . escapeshellarg($iccid) . ' ' . escapeshellarg($refillSku) . ' 2>&1';
    logMessage($iccid, 'refill', 'command=' . $command);
    $output = [];
    $exitCode = 0;
    exec($command, $output, $exitCode);

    logMessage($iccid, 'refill', 'raw_output=' . trim(implode(PHP_EOL, $output)));
    $result = sanitizeRefillCommandOutput($output);
    logMessage($iccid, 'refill', 'output=' . $result);

    if ($exitCode !== 0 && !isSuccessfulRefillResult($result)) {
        throw new RuntimeException('refill command failed: ' . $result);
    }

    return $result;
}

function deletePack(string $imsi, string $packCode, string $context): string
{
    $command = DELPACK_COMMAND . ' ' . escapeshellarg($imsi) . ' ' . escapeshellarg($packCode) . ' 2>&1';
    logMessage($context, 'delpack', 'command=' . $command);
    $output = [];
    $exitCode = 0;
    exec($command, $output, $exitCode);

    logMessage($context, 'delpack', 'raw_output=' . trim(implode(PHP_EOL, $output)));
    $result = sanitizeProviderCommandOutput($output);
    logMessage($context, 'delpack', 'output=' . $result);

    if ($exitCode !== 0) {
        throw new RuntimeException('delpack command failed: ' . $result);
    }

    if (trim($result) !== '1') {
        throw new RuntimeException('delpack command did not return success status 1: ' . $result);
    }

    return $result;
}

function fetchPackList(string $imsi, string $context): array
{
    $command = PACKLIST_COMMAND . ' ' . escapeshellarg($imsi) . ' 2>&1';
    logMessage($context, 'packlist', 'command=' . $command);
    $output = [];
    $exitCode = 0;
    exec($command, $output, $exitCode);

    logMessage($context, 'packlist', 'raw_output=' . trim(implode(PHP_EOL, $output)));
    $result = sanitizeProviderCommandOutput($output);
    logMessage($context, 'packlist', 'output=' . $result);

    if ($exitCode !== 0) {
        throw new RuntimeException('packlist command failed: ' . $result);
    }

    $packs = json_decode($result, true);

    if (!is_array($packs)) {
        throw new RuntimeException('invalid packlist JSON returned: ' . $result);
    }

    return $packs;
}

function canProceedAfterDelpackFailure(string $imsi, string $context): bool
{
    $packs = fetchPackList($imsi, $context);

    if ($packs === []) {
        return false;
    }

    foreach ($packs as $pack) {
        if (!is_array($pack)) {
            return false;
        }

        if (strcasecmp(trim((string) ($pack['state'] ?? '')), 'Invalid') !== 0) {
            return false;
        }
    }

    logMessage($context, 'packlist', 'invalid packs returned; all pack states are Invalid');

    return true;
}

function sanitizeRefillCommandOutput(array $output): string
{
    return sanitizeProviderCommandOutput($output);
}

function sanitizeProviderCommandOutput(array $output): string
{
    $filtered = array_values(array_filter($output, static function ($line): bool {
        return strpos($line, 'PHP Notice:  Undefined offset: 12 in /usr/local/ownmail/bin/quad_call_with_return.php') === false;
    }));

    return trim(implode(PHP_EOL, $filtered));
}

function isSuccessfulRefillResult(string $result): bool
{
    foreach (preg_split('/\r\n|\r|\n/', trim($result)) as $line) {
        if (strcasecmp(trim($line), 'success') === 0) {
            return true;
        }
    }

    return false;
}

function markRefillStatus($dbConnection, string $tripid, ?string $transactionId = null): void
{
    $sql = 'UPDATE tsim_refill SET refill_status = true WHERE tripid = $1';
    $parameters = [$tripid];
    if ($transactionId !== null) {
        $sql .= ' AND transaction_id = $2';
        $parameters[] = $transactionId;
    }
    $result = @pg_query_params($dbConnection, $sql, $parameters);

    if ($result === false) {
        throw new RuntimeException('failed to update refill_status for tripid=' . $tripid);
    }
}

$dbConnection = null;
$transactionStarted = false;

try {
    $filters = parseRefillFilterArguments(array_slice($argv, 1));
    $orderId = $filters['order_id'];
    $transactionId = $filters['transaction_id'];
    $dbConnection = connectDatabase();

    if ($dbConnection === false) {
        logMessage('database', 'error', 'failed to connect to postgres');
        stderr('Failed to connect to postgres.');
        exit(2);
    }

    if (@pg_query($dbConnection, 'BEGIN') === false) {
        throw new RuntimeException('failed to begin refill row-lock transaction');
    }

    $transactionStarted = true;
    $pendingRows = fetchPendingRefills($dbConnection, $orderId, $transactionId);
    logMessage('database', 'ok', 'fetched ' . count($pendingRows) . ' pending refill rows'
        . ($orderId === null ? '' : ' for order_id=' . $orderId)
        . ($transactionId === null ? '' : ' for transaction_id=' . $transactionId));

    $now = new DateTimeImmutable('now');
    $rechargeCandidates = [];
    $refillErrors = [];
    $refillFailures = [];

    foreach ($pendingRows as $row) {
        $tripid = trim((string) ($row['tripid'] ?? ''));
        $simno = trim((string) ($row['simno'] ?? ''));
        $context = $tripid !== '' ? $tripid : 'unknown';

        if ($simno === '') {
            $message = 'missing simno for pending refill row';
            addRefillFailure($refillFailures, $tripid, $simno, $message);
            logMessage($context, 'error', $message);
            continue;
        }

        try {
            $pack = fetchSinglePack($simno);
        } catch (RuntimeException $exception) {
            addRefillFailure($refillFailures, $tripid, $simno, $exception->getMessage());
            logMessage($context, 'error', 'simno=' . $simno . ' ' . $exception->getMessage());
            continue;
        }

        $zeroBalance = ($pack['_zeroBalance'] ?? false) === true;
        $noPacksReturned = ($pack['_noPacksReturned'] ?? false) === true;
        $allPacksInvalid = ($pack['_allPacksInvalid'] ?? false) === true;
        unset($pack['_zeroBalance'], $pack['_noPacksReturned'], $pack['_allPacksInvalid']);
        $reasons = [];

        if ($zeroBalance) {
            $reasons[] = 'zero balance returned';
            logMessage($context, 'balance', 'zero balance returned; proceeding with refill');
        } elseif ($noPacksReturned) {
            $reasons[] = 'no packs returned';
            logMessage($context, 'balance', 'no packs returned; proceeding with refill');
        } elseif ($allPacksInvalid) {
            $reasons[] = 'invalid packs returned';
            logMessage($context, 'balance', 'invalid packs returned; all pack states are Invalid');
        } elseif (isExpired($pack, $now)) {
            $expiry = parseQuadDate($pack['expTime'] ?? null);
            $reasons[] = 'expired at ' . ($expiry ? $expiry->format('Y-m-d H:i:s') : ($pack['expTime'] ?? 'n/a'));
        }

        if (!$zeroBalance && !$noPacksReturned && shouldRefillForQuota($pack)) {
            $total = (float) $pack['totalQuota'];
            $remaining = (float) $pack['remainingQuota'];
            $percent = ($remaining / $total) * 100.0;
            $reasons[] = sprintf('remaining quota low at %.2f%% of total', $percent);
        }

        if ($reasons === []) {
            logMessage($context, 'ok', 'simno=' . $simno . ' no refill needed [' . buildPackSummary($pack) . ']');
            continue;
        }

        $refillSku = trim((string) ($row['refill_sku'] ?? ''));

        if ($refillSku === '') {
            $message = 'simno=' . $simno . ' missing refill_sku for recharge candidate';
            $refillErrors[] = $message;
            addRefillFailure($refillFailures, $tripid, $simno, $message);
            logMessage($context, 'error', $message);
            continue;
        }

        $packCode = trim((string) ($pack['packCode'] ?? ''));

        if (!$zeroBalance && !$noPacksReturned && $packCode === '') {
            $message = 'simno=' . $simno . ' missing packCode for recharge candidate';
            $refillErrors[] = $message;
            addRefillFailure($refillFailures, $tripid, $simno, $message);
            logMessage($context, 'error', $message);
            continue;
        }

        $candidate = [
            'username' => $row['username'] ?? null,
            'domainname' => $row['domainname'] ?? null,
            'tripid' => $tripid,
            'amount' => $row['amount'] ?? null,
            'refill_sku' => $refillSku,
            'transaction_id' => $row['transaction_id'] ?? null,
            'accept_time' => $row['accept_time'] ?? null,
            'simno' => $simno,
            'imsi' => null,
            'packCode' => $packCode,
            'reasons' => $reasons,
            'pack' => $pack,
        ];

        try {
            if ($zeroBalance) {
                $candidate['delpack_result'] = 'skipped because zero balance was returned';
                logMessage($context, 'delpack', 'skipped because zero balance was returned');
            } elseif ($noPacksReturned) {
                $candidate['delpack_result'] = 'skipped because no packs were returned';
                logMessage($context, 'delpack', 'skipped because no packs were returned');
            } elseif ($allPacksInvalid) {
                $candidate['delpack_result'] = 'skipped because all packs are Invalid';
                logMessage($context, 'delpack', 'skipped because all packs are Invalid');
            } else {
                $imsi = fetchImsiByIccid($dbConnection, $simno);
                $candidate['imsi'] = $imsi;

                try {
                    $candidate['delpack_result'] = deletePack($imsi, $packCode, $context);
                } catch (RuntimeException $exception) {
                    if (!canProceedAfterDelpackFailure($imsi, $context)) {
                        throw $exception;
                    }

                    $candidate['delpack_result'] = 'skipped after packlist confirmed all packs Invalid';
                    logMessage($context, 'delpack', 'proceeding with refill because all packs are Invalid');
                }

                sleep(3);
            }

            $refillResult = runRefill($simno, $refillSku);
            $candidate['refill_result'] = $refillResult;

            if ($tripid !== '' && isSuccessfulRefillResult($refillResult)) {
                markRefillStatus($dbConnection, $tripid, $transactionId);
            }

            $rechargeCandidates[] = $candidate;
            logMessage($context, 'refill', 'simno=' . $simno . ' sku=' . $refillSku . ' ' . implode('; ', $reasons) . ' [' . buildPackSummary($pack) . '] result=' . $refillResult);
        } catch (RuntimeException $exception) {
            $message = 'simno=' . $simno . ' sku=' . $refillSku . ' ' . $exception->getMessage();
            $refillErrors[] = $message;
            addRefillFailure($refillFailures, $tripid, $simno, $message);
            logMessage($context, 'error', $message);
        }
    }

    echo json_encode($rechargeCandidates, JSON_UNESCAPED_SLASHES) . PHP_EOL;

    sendRefillFailureEmail($refillFailures);

    if (@pg_query($dbConnection, 'COMMIT') === false) {
        throw new RuntimeException('failed to commit refill row-lock transaction');
    }

    $transactionStarted = false;

    if ($refillErrors !== []) {
        stderr(implode(PHP_EOL, $refillErrors));
        exit(2);
    }

    exit($rechargeCandidates === [] ? 0 : 1);
} catch (Throwable $exception) {
    if ($transactionStarted && $dbConnection !== null) {
        @pg_query($dbConnection, 'ROLLBACK');
    }

    logMessage('script', 'error', $exception->getMessage());
    stderr($exception->getMessage());
    exit(2);
}
