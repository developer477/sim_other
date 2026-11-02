# Customer account validation

This patch updates the customer email/phone lookup in `tsim_integration.php`:

- Check every candidate before selecting an account.
- Continue automatic booking only when exactly one candidate matches both email and phone.
- When candidates exist without a unique match, send the existing validation link to helpdesk and `deven@staff.ownmail.com`, then stop automatic booking.
- Create a new customer only when no candidates exist.
- Log candidate counts and each validation email's mail-system acceptance result.

Multiple email/phone matches require validation even if the address lookup could select one of them. The ten-candidate threshold no longer controls this decision. The existing explicit-username route is unchanged. Existing reseller validation emails also copy Deven; reseller account selection is unchanged.

The full integration files remain outside this public repository because they contain embedded credentials. This patch contains only the validation change, based on the local pre-change version from 2 November 2026. It does not include the earlier username-collision or apostrophe fixes. `tsim_integration_v3.php` is unchanged.

With the matching private copy of `tsim_integration.php` in the repository root:

```sh
git apply --check patches/8388-tsim-integration-validation.patch
git apply patches/8388-tsim-integration-validation.patch
php -l tsim_integration.php
php tests/test_tsim_integration_validation.php
```

The test also accepts an explicit PHP source path as its first argument. It extracts the account-selection branches and replaces database, HTTP, email and log operations with local fakes. It never includes or runs the integration endpoint.

Local checks cover the split email/phone case for order 1516798, candidate ordering, unique and ambiguous matches, exact-address handling, empty attributes, more than ten candidates, new customers, database failures, validation-link encoding and email recipients. Mail acceptance does not prove delivery.

This change has not been deployed or tested against production. Existing mapping and deduplication markers can still prevent an already-blocked order from reaching account selection; recovery of those orders remains separate.
