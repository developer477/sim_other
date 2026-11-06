# Customer account validation

This patch updates the customer email/phone lookup in both `tsim_integration.php` and `tsim_integration_v3.php`. Each script remains self-contained:

- Find candidates within the same domain using case-insensitive email or the last seven phone digits, ignoring phone formatting. Short/empty numbers do not establish a phone match.
- Require a case-insensitive email match and the existing address comparison before automatically selecting an account. A different phone does not disqualify an email-and-address match; phone alone never selects an account.
- Check every email-and-address match. Use a unique match, or prefer one non-numeric username when all other matches have usernames made entirely of digits. If multiple named accounts or multiple numeric-only accounts remain, request validation.
- When candidates exist without a unique match, send the existing validation link to helpdesk and `deven@staff.ownmail.com`, then stop automatic booking.
- Create a new customer only when no candidates exist.
- Log candidate counts and each validation email's mail-system acceptance result.

The address comparison retains the current order billing building/street/city/postcode versus the account's stored delivery fields. A missing building cannot establish an address match. A unique email-and-phone match no longer bypasses a failed address check. The ten-candidate threshold does not control this decision. V3 no longer resolves ambiguous matches by choosing the most recently used or first account. The existing explicit-username route and reseller account selection are unchanged. Customer validation preserves each script's existing recipients and also copies Deven; V3's reseller recipients remain unchanged.

The full integration files remain outside this public repository because they contain embedded credentials. This patch contains only the validation changes: the main script section uses its local pre-validation version from 2 November 2026 as a base, and V3 uses its local version before the 6 November matching amendment. It does not include earlier username-collision, apostrophe or payment-status fixes. V3 retains its local `register` route for new customers; the main script retains its existing follow-up request.

With both matching private PHP copies in the repository root:

```sh
git apply --check patches/8388-tsim-integration-validation.patch
git apply patches/8388-tsim-integration-validation.patch
php -l tsim_integration.php
php -l tsim_integration_v3.php
php tests/test_tsim_integration_validation.php
php tests/test_tsim_integration_validation.php tsim_integration_v3.php
```

For a server containing only one script, use `git apply --include=tsim_integration.php --check patches/8388-tsim-integration-validation.patch` (or `--include=tsim_integration_v3.php`), then repeat without `--check`. There is no shared runtime dependency.

The test also accepts an explicit PHP source path as its first argument. It extracts the account-selection branches and replaces database, HTTP, email and log operations with local fakes. It never includes or runs the integration endpoint.

Local checks cover split email/phone identities, different or missing phones with matching email/address, case-insensitive email, numeric-username preference, candidate ordering, address mismatches, ambiguity, empty attributes, more than ten candidates, each script's existing new-customer route, database failures, validation-link encoding and email recipients. The revised suite passes 33 scenarios per script: 203 assertions for the main script and 227 for V3 (430 total). Eleven additional checks of the actual parameterized SQL passed against temporary tables in local PostgreSQL, covering phone suffixes, email casing, domain isolation, address fields and apostrophes; both scripts use identical matching SQL. Mail acceptance does not prove delivery.

Read-only checks on 6 November 2026: order 1517672 has one phone/address candidate but a different email, so it still requires validation. Order 1517676 has one matching email/address with a different phone and would be selected. Order 1517636 has two email/phone candidates, but only the numeric username matches the street, so that account would be selected. These are current database snapshots, not proof of which historical route booked an order.

Deployment of the 6 November matching amendment remains a separate step. No booking was replayed and no production data was changed. Existing mapping and deduplication markers can still prevent an already-blocked order from reaching account selection; recovery of those orders remains separate.
