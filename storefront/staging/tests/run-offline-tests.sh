#!/bin/sh
# Offline storefront tests (2026-10-04): no network and no Stripe key needed.
# Run from the repository root inside a PHP CLI container with curl, e.g.
#   docker run --rm -v "$PWD":/repo -w /repo php:8.2-cli sh storefront/staging/tests/run-offline-tests.sh
set -eu
ROOT=$(pwd)
STATE=$(mktemp -d)
export FAKE_STRIPE_STATE="$STATE"
export FAKE_STRIPE_PRICES="$ROOT/deploy/staging-stripe-test-prices.json"
php -S 127.0.0.1:8098 "$ROOT/storefront/staging/tests/fake-stripe.php" >"$STATE/fake.log" 2>&1 &
FAKE=$!
STRIPE_SECRET_KEY=sk_test_offline ANYAICAM_STOREFRONT_TEST_STRIPE_BASE=http://127.0.0.1:8098 \
  ANYAICAM_STOREFRONT_BASE_URL=https://store.example.test \
  php -S 127.0.0.1:8099 -t "$ROOT/storefront" >"$STATE/site.log" 2>&1 &
SITE=$!
trap 'kill $FAKE $SITE 2>/dev/null || true; rm -rf "$STATE"' EXIT
sleep 1
status=0
echo "== test-checkout-catalog.php"; php "$ROOT/storefront/staging/tests/test-checkout-catalog.php" | tail -2 || status=1
echo "== test-checkout-session-offline.php"; php "$ROOT/storefront/staging/tests/test-checkout-session-offline.php" || status=1
exit $status
