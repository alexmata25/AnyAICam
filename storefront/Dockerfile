# STAGING/TEST STOREFRONT ONLY -- serves website-pricing-review/ (the local
# review copy that has never been deployed anywhere before this task).
# Smallest reliable PHP/Apache base: the official php:8.2-apache image plus
# the curl extension (not bundled by default), which checkout-session.php/
# stripe-checkout.php both require for their Stripe API calls.
FROM php:8.2-apache

RUN apt-get update \
    && apt-get install -y --no-install-recommends libcurl4-openssl-dev \
    && docker-php-ext-install curl \
    && rm -rf /var/lib/apt/lists/*

# The snapshot already contains everything under website-pricing-review/
# (top-level pages + staging/ subfolder) -- copied verbatim, not rebuilt
# from a live working tree, so what's deployed matches exactly what
# staging/tests/test-checkout-catalog.php verified (88/88) before this
# build ran (see .snapshot-sha256.txt at the copy's root for the exact
# tarball checksum this image was built from).
COPY . /var/www/html/

# A tiny convenience redirect so the bare hostname lands on the actual
# customer-facing entry point instead of Apache's directory listing.
RUN printf '<!doctype html><meta http-equiv="refresh" content="0; url=/staging/build-your-system.html">' > /var/www/html/index.html

EXPOSE 80
