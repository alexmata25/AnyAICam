<?php
declare(strict_types=1);

/**
 * STAGING/REVIEW COPY -- NOT the live Bluehost file.
 *
 * Regression coverage for the storefront->customer-registration handoff
 * fix (2026-09-12): before this fix, checkout-continue.html's final
 * "purchase complete" state had no path onward at all -- a brand-new
 * customer who had just paid had no way to learn where to create the
 * AnyAiCam account their purchase is waiting to be attached to. This is
 * a content-contract test (no test framework is installed anywhere in
 * this repo, matching test-checkout-catalog.php's own established
 * plain-PHP assertion style): it asserts the expected markup/behavior
 * is present in the actual shipped file, not a rendered DOM (there is
 * no headless browser available in this environment either).
 *
 * Run with: php staging/tests/test-checkout-continue-handoff.php
 */

$failures = 0;
$passed = 0;

function check(bool $condition, string $description): void {
    global $failures, $passed;
    if ($condition) {
        $passed++;
    } else {
        $failures++;
        echo "FAIL: {$description}\n";
    }
}

$source = file_get_contents(__DIR__ . '/../checkout-continue.html');
if ($source === false) {
    echo "FAIL: could not read checkout-continue.html\n";
    exit(1);
}

// ------------------------------------------------- the handoff itself

check(
    str_contains($source, "const PORTAL_REGISTER_URL = 'https://portal-staging.anyaicam.com/customer-register'"),
    'a PORTAL_REGISTER_URL constant points at the real staging portal registration page'
);
check(
    str_contains($source, 'Create your AnyAiCam account'),
    'the done state offers an explicit "Create your AnyAiCam account" call to action'
);
check(
    str_contains($source, '${registerUrl}'),
    'the registration link is built from the PORTAL_REGISTER_URL constant, not hardcoded a second time'
);
check(
    str_contains($source, 'A partner/administrator still needs to approve the new account'),
    'the handoff sets correct expectations: registration is not the same as an active/approved account'
);

// ---------------------------------------- existing behavior preserved

check(
    str_contains($source, 'Sandbox test purchase complete'),
    'the existing purchase-complete confirmation heading is unchanged'
);
check(
    str_contains($source, 'Build another test order'),
    'the pre-existing "build another test order" action still exists'
);
check(
    str_contains($source, "remainingLegs.length === 0"),
    'the done state is still reached the same way -- no change to the multi-leg continuation logic'
);

// ---------------------------------------------------- never a regression
// in the multi-leg continuation path itself (this fix only touches the
// done state, never the "continue to the next leg" branch).

check(
    str_contains($source, "await fetch('checkout-session.php'"),
    'the next-leg continuation call to checkout-session.php is untouched'
);

if ($failures > 0) {
    echo "\n{$failures} failed, {$passed} passed\n";
    exit(1);
}
echo "ALL PASSED ({$passed} checks)\n";
