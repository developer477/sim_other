<?php
declare(strict_types=1);
namespace TsimValidationTest;

// Execute only the account-selection block. All database, HTTP, mail and log
// operations are replaced with local fakes; the integration endpoint is never included.
class RouteExit extends \RuntimeException {}
class Rows {
    public int $position = 0;
    public function __construct(public array $rows) {}
}
class HttpRequest { public array $options = []; }

function extract_block(string $source, string $marker): string {
    $start = strpos($source, $marker);
    if ($start === false) throw new \RuntimeException('Missing source block: '.$marker);
    $code = '';
    $depth = 0;
    $opened = false;
    foreach (token_get_all('<?php '.substr($source, $start)) as $token) {
        if (is_array($token)) {
            if ($token[0] === T_OPEN_TAG) continue;
            if ($token[0] === T_CURLY_OPEN || $token[0] === T_DOLLAR_OPEN_CURLY_BRACES) $depth++;
            $code .= $token[0] === T_EXIT ? 'throw new RouteExit()' : $token[1];
        } else {
            $code .= $token;
            if ($token === '{') { $opened = true; $depth++; }
            if ($token === '}' && --$depth === 0 && $opened) return $code;
        }
    }
    throw new \RuntimeException('Unclosed source block');
}

function pg_query($db, string $sql) {
    $state = &$GLOBALS['test_state'];
    $state['sql'][] = $sql;
    if (str_starts_with($sql, 'select * from users where username=')) return new Rows([]);
    throw new \RuntimeException('Unexpected database query: '.$sql);
}
function pg_query_params($db, string $sql, array $params) {
    $state = &$GLOBALS['test_state'];
    $state['sql'][] = $sql;
    $state['query_params'][] = compact('sql', 'params');
    if (str_starts_with($sql, 'select * from sim_user where domainname=')) {
        return $state['config']['candidate_failure'] ?? false ? false : new Rows($state['config']['candidates']);
    }
    if (str_contains($sql, 'trim(deliver_add_building)')) {
        return $state['config']['address_failure'] ?? false ? false : new Rows($state['config']['exact'] ?? []);
    }
    throw new \RuntimeException('Unexpected parameterized query');
}
function pg_num_rows(Rows $rows): int { return count($rows->rows); }
function pg_fetch_all(Rows $rows) {
    return !$rows->rows && ($GLOBALS['test_state']['config']['empty_fetch_false'] ?? false) ? false : $rows->rows;
}
function pg_result_seek(Rows $rows, int $position): bool { $rows->position = $position; return isset($rows->rows[$position]); }
function pg_fetch_assoc(Rows $rows) { return $rows->rows[$rows->position++] ?? false; }
function pg_last_error($db): string { return 'simulated lookup failure'; }
function file_put_contents($path, $message, $flags = 0): int {
    $GLOBALS['test_state']['logs'][] = $message;
    return strlen($message);
}
function mail($to, $subject, $message, $headers): bool {
    $GLOBALS['test_state']['mail'][] = compact('to', 'subject', 'message', 'headers');
    return !in_array($to, $GLOBALS['test_state']['config']['mail_failures'] ?? [], true);
}
function get_current_host_tsim_integration_url(): string { return 'https://integration.invalid/tsim_integration.php'; }
function curl_init(): HttpRequest { return new HttpRequest(); }
function curl_setopt(HttpRequest $request, $option, $value): bool { $request->options[$option] = $value; return true; }
function curl_exec(HttpRequest $request): string {
    parse_str($request->options[CURLOPT_POSTFIELDS], $payload);
    $GLOBALS['test_state']['requests'][] = $payload;
    if ($request->options[CURLOPT_SSL_VERIFYPEER] !== true || $request->options[CURLOPT_SSL_VERIFYHOST] !== 2) {
        throw new \RuntimeException('Existing-user request lost TLS verification');
    }
    return 'mock booking response';
}
function curl_getinfo($request, $option): int { return 200; }
function curl_error($request): string { return ''; }
function curl_close($request): void {}
function create_user($username, $phone): string {
    $GLOBALS['test_state']['new_user_calls']++;
    return 'unused-test-customer';
}
function submit_followup_booking_request($payload, $context, $log_file): string {
    $GLOBALS['test_state']['requests'][] = $payload;
    return 'mock booking response';
}

function run_case(string $block, array $config): array {
    $GLOBALS['test_state'] = ['config' => $config, 'sql' => [], 'query_params' => [], 'logs' => [], 'mail' => [], 'requests' => [], 'new_user_calls' => 0];
    $db = 'mock';
    $log_file = 'mock-log';
    $found_simuser = 0;
    $orderid = '1516798';
    $username = 'newuser';
    $action = '';
    $email = 'order@example.invalid';
    $orderEmail = $email;
    $phone = '919000000001';
    $orderMobile = '+'.$phone;
    $domainname = 'tsim.in';
    $role = 'customer';
    $services_email = 'services@tsim.in';
    $client_building = 'Example Building';
    $client_add_street = "Pragathi's Road & Lane";
    $client_city = 'Hyderabad';
    $client_postcode = '500090';
    $billing_address = "Example Building, Pragathi's Road & Lane";
    $shipping_address = 'Delivery + Address';
    $num_rows = 2; // For the separately extracted reseller validation block.
    extract($config['inputs'] ?? []);
    ob_start();
    try {
        eval('namespace TsimValidationTest; '.$block);
    } catch (RouteExit $exit) {
        $GLOBALS['test_state']['exited'] = true;
    } finally {
        ob_end_clean();
    }
    return $GLOBALS['test_state'] + compact('action', 'username', 'billing_address', 'shipping_address', 'orderMobile');
}

function check(bool $condition, string $message): void {
    if (!$condition) throw new \RuntimeException($message);
    $GLOBALS['checks']++;
}
function verify_route(array $state, string $expected, ?string $username = null, ?array $recipients = null): void {
    if ($expected === 'validation') {
        check(array_column($state['mail'], 'to') === ($recipients ?? $GLOBALS['customer_recipients']), 'Validation must reach each configured recipient once');
        check($state['requests'] === [] && $state['new_user_calls'] === 0 && $state['action'] === '', 'Uncertain accounts must not book or create a user');
        check($state['exited'] ?? false, 'Validation must stop automatic processing');
        foreach ($state['mail'] as $email) {
            check($email['subject'] === 'Validate User for Order no 1516798', 'Validation subject must identify the order');
        }
    } elseif ($expected === 'existing') {
        check($state['mail'] === [] && $state['new_user_calls'] === 0, 'Clear matches must not create users or request validation');
        check(count($state['requests']) === 1 && !isset($state['requests'][0]['new_user']), 'Send exactly one existing-user request');
        check($state['requests'][0]['set_username'] === $username, 'Select the matching username');
        check($state['requests'][0]['order_no'] === '1516798', 'Preserve order identity');
    } elseif ($expected === 'exact') {
        check($state['action'] === 'user_exists' && $state['username'] === $username, 'Select the expected email-and-address account');
        check($state['mail'] === [] && $state['requests'] === [] && $state['new_user_calls'] === 0, 'Exact match must not invoke another booking path');
    } elseif ($expected === 'new') {
        check($state['mail'] === [] && $state['new_user_calls'] === 1, 'No candidates must retain new-user creation');
        if ($GLOBALS['testing_v3']) {
            check($state['action'] === 'register' && $state['requests'] === [], 'V3 must retain its local new-user registration route');
        } else {
            check(count($state['requests']) === 1 && $state['requests'][0]['new_user'] === 'new_user', 'Submit one new-user booking request');
        }
    } elseif ($expected === 'error') {
        check(($state['exited'] ?? false) && $state['mail'] === [] && $state['requests'] === [] && $state['new_user_calls'] === 0, 'Database failure must not become a new or existing booking');
        check(str_contains(implode('', $state['logs']), 'lookup failed for order 1516798'), 'Lookup failure must identify the affected order');
    }
}

$GLOBALS['checks'] = 0;
set_error_handler(function ($level, $message, $file, $line) { throw new \ErrorException($message, 0, $level, $file, $line); });
$sourcePath = $argv[1] ?? dirname(__DIR__).'/tsim_integration.php';
$GLOBALS['testing_v3'] = str_contains(basename($sourcePath), '_v3');
$GLOBALS['customer_recipients'] = $GLOBALS['testing_v3']
    ? ['hoshang@staff.ownmail.com', 'aditya@staff.ownmail.com', 'binu@staff.ownmail.com', 'services@tsim.in', 'dhaval@staff.ownmail.com', 'deven@staff.ownmail.com']
    : ['alpesh@staff.ownmail.com', 'binu@staff.ownmail.com', 'services@tsim.in', 'deven@staff.ownmail.com'];
$source = \file_get_contents($sourcePath);
$block = extract_block($source, 'if ($found_simuser ==0){');
$full = ['username' => 'full-match', 'emailadd' => 'order@example.invalid', 'mobno' => '919000000001'];
$phoneOnly = ['username' => 'phone-only', 'emailadd' => 'old@example.invalid', 'mobno' => '919000000001'];
$emailOnly = ['username' => 'email-only', 'emailadd' => 'order@example.invalid', 'mobno' => '919000000002'];
$secondFull = array_replace($full, ['username' => 'second-full-match']);
$many = [];
for ($i = 0; $i < 10; $i++) $many[] = array_replace($phoneOnly, ['username' => 'shared-phone-'.$i]);
$numeric = array_replace($full, ['username' => '15550000001']);
$numeric2 = array_replace($full, ['username' => '15550000002']);
$mixedCase = array_replace($full, ['emailadd' => 'ORDER@Example.Invalid']);
$cases = [
    ['1516798 split identities without an address match', ['candidates' => [$phoneOnly, $emailOnly]], 'validation'],
    ['split identities select email-and-address match', ['candidates' => [$phoneOnly, $emailOnly], 'exact' => [$emailOnly]], 'exact', 'email-only'],
    ['reversed candidates select same email-and-address match', ['candidates' => [$emailOnly, $phoneOnly], 'exact' => [$emailOnly]], 'exact', 'email-only'],
    ['full match after partial candidate', ['candidates' => [$phoneOnly, $full], 'exact' => [$full]], 'exact', 'full-match'],
    ['full match before partial candidate', ['candidates' => [$full, $phoneOnly], 'exact' => [$full]], 'exact', 'full-match'],
    ['email and phone match but address differs', ['candidates' => [$full]], 'validation'],
    ['address disambiguates two matching emails', ['candidates' => [$full, $secondFull], 'exact' => [$full]], 'exact', 'full-match'],
    ['multiple named email-and-address matches', ['candidates' => [$full, $secondFull], 'exact' => [$full, $secondFull]], 'validation'],
    ['prefer named account over numeric account', ['candidates' => [$numeric, $full], 'exact' => [$numeric, $full]], 'exact', 'full-match'],
    ['named preference independent of query order', ['candidates' => [$full, $numeric], 'exact' => [$full, $numeric]], 'exact', 'full-match'],
    ['1517636 numeric account is the only address match', ['candidates' => [$numeric, $full], 'exact' => [$numeric]], 'exact', '15550000001'],
    ['one named account among several numeric matches', ['candidates' => [$numeric, $numeric2, $full], 'exact' => [$numeric2, $full, $numeric]], 'exact', 'full-match'],
    ['several numeric matches still need validation', ['candidates' => [$numeric, $numeric2], 'exact' => [$numeric, $numeric2]], 'validation'],
    ['several named matches plus numeric still need validation', ['candidates' => [$full, $numeric, $secondFull], 'exact' => [$numeric, $secondFull, $full]], 'validation'],
    ['case-insensitive email match', ['candidates' => [$mixedCase], 'exact' => [$mixedCase]], 'exact', 'full-match'],
    ['email whitespace ignored', ['candidates' => [array_replace($full, ['emailadd' => ' order@example.invalid '])], 'exact' => [$full]], 'exact', 'full-match'],
    ['1517672 phone and address match but different email', ['candidates' => [$phoneOnly], 'no_address_query' => true], 'validation'],
    ['1517676 email and address match with a different phone', ['candidates' => [$emailOnly], 'exact' => [$emailOnly]], 'exact', 'email-only'],
    ['missing order phone with matching email and address', ['candidates' => [$full], 'exact' => [$full], 'inputs' => ['phone' => '', 'orderMobile' => '']], 'exact', 'full-match'],
    ['missing account phone with matching email and address', ['candidates' => [array_replace($full, ['mobno' => null])], 'exact' => [array_replace($full, ['mobno' => null])]], 'exact', 'full-match'],
    ['missing phone cannot bypass address mismatch', ['candidates' => [$full], 'inputs' => ['phone' => '', 'orderMobile' => '']], 'validation'],
    ['no candidates, empty array result', ['candidates' => [], 'no_address_query' => true], 'new'],
    ['no candidates, false fetch result', ['candidates' => [], 'empty_fetch_false' => true, 'no_address_query' => true], 'new'],
    ['shared phone, ten candidates', ['candidates' => $many, 'no_address_query' => true], 'validation'],
    ['one email-and-address match among eleven candidates', ['candidates' => array_merge($many, [$full]), 'exact' => [$full]], 'exact', 'full-match'],
    ['empty email cannot establish identity', ['candidates' => [array_replace($full, ['emailadd' => ''])], 'inputs' => ['orderEmail' => '', 'email' => ''], 'no_address_query' => true], 'validation'],
    ['empty address cannot establish identity', ['candidates' => [$full], 'inputs' => ['client_building' => '   '], 'no_address_query' => true], 'validation'],
    ['mail refusal must not cause automatic booking', ['candidates' => [$phoneOnly, $emailOnly], 'mail_failures' => ['deven@staff.ownmail.com']], 'validation'],
    ['candidate query failure', ['candidates' => [], 'candidate_failure' => true], 'error'],
    ['address query failure', ['candidates' => [$full], 'address_failure' => true], 'error'],
    ['phone-only candidates need no address query', ['candidates' => [$phoneOnly], 'address_failure' => true, 'no_address_query' => true], 'validation'],
    ['new customer needs no address lookup', ['candidates' => [], 'address_failure' => true, 'no_address_query' => true], 'new'],
];
foreach ($cases as $case) {
    [$label, $config, $expected] = $case;
    $state = run_case($block, $config);
    verify_route($state, $expected, $case[3] ?? null);
    if ($config['no_address_query'] ?? false) check(count(array_filter($state['sql'], fn($sql) => str_contains($sql, 'trim(deliver_add_building)'))) === 0, 'Do not query the address without an email candidate or usable address');
    if ($expected === 'validation') {
        preg_match("/href='([^']+)'/", $state['mail'][0]['message'], $link);
        $url = html_entity_decode($link[1], ENT_QUOTES, 'UTF-8');
        parse_str(parse_url($url, PHP_URL_QUERY), $params);
        check($params['o'] === '1516798' && $params['r'] === 'customer', 'Validation link must retain order and role');
        check($params['b'] === $state['billing_address'] && $params['s'] === $state['shipping_address'], 'Quotes, ampersands and plus signs must survive the validation link');
        check($params['ordermobile'] === $state['orderMobile'], 'Raw phone plus sign must survive the validation link');
        check(count(array_filter($state['logs'], fn($log) => str_contains($log, 'Validation email for order 1516798'))) === count($GLOBALS['customer_recipients']), 'Log the result for every recipient');
    }
    if ($config['mail_failures'] ?? []) check(str_contains(implode('', $state['logs']), 'recipient=deven@staff.ownmail.com | accepted=no'), 'Log mail-system refusal');
    echo 'PASS: '.$label.PHP_EOL;
}
foreach ([
    ['+919000000001', '0000001'],
    ['919000000001', '0000001'],
    ['(900) 000-0001', '0000001'],
    ['0000001', '0000001'],
    ['123456', ''],
    ['', ''],
] as [$inputPhone, $expectedSuffix]) {
    $state = run_case($block, ['candidates' => [], 'inputs' => ['phone' => $inputPhone, 'orderMobile' => $inputPhone]]);
    check($state['query_params'][0]['params'][1] === $expectedSuffix, 'Candidate lookup must bind exactly seven trailing digits, or no suffix');
    check(!str_contains($state['query_params'][0]['sql'], 'order@example.invalid'), 'Customer attributes must be bound parameters');
}
$state = run_case($block, ['candidates' => [$full], 'exact' => [$full]]);
$lookupQueries = $state['query_params'];
check(count($lookupQueries) === 2, 'Use the candidate and address lookups only');
check($lookupQueries[1]['params'][2] === "%Pragathi's Road & Lane%", 'Address punctuation must survive parameter binding');
check(!str_contains($lookupQueries[1]['sql'], 'mobno'), 'Address match must not require the old phone');

$resellerBlock = extract_block($source, 'else if($num_rows>1)');
$resellerBlock = preg_replace('/^else if/', 'if', $resellerBlock, 1);
$state = run_case($resellerBlock, ['inputs' => ['role' => 'reseller_customer']]);
$resellerRecipients = $GLOBALS['testing_v3']
    ? ['hoshang@staff.ownmail.com', 'husain@staff.ownmail.com', 'binu@staff.ownmail.com', 'services@tsim.in']
    : $GLOBALS['customer_recipients'];
verify_route($state, 'validation', null, $resellerRecipients);
echo 'PASS: existing reseller validation recipients preserved'.PHP_EOL;
echo 'PASS: '.basename($sourcePath).': '.(count($cases) + 1).' scenarios, '.$GLOBALS['checks'].' assertions; no real database, HTTP or mail calls'.PHP_EOL;
