"""Regression coverage for conftest.py's production/staging safety rail
(2026-09-16). Trigger: an independent review reproduced that the
original implementation -- `PRODUCTION_DOMAIN in BASE_URL` -- is a
case-sensitive substring check, so `https://APP.ANYAICAM.COM` (or any
differently-cased variant) sailed straight through it, never treated as
production at all. Reproduced locally before fixing.

Fixed by switching the whole model from a production blocklist to an
explicit staging ALLOWLIST (ALLOWED_STAGING_HOSTS), decided from the
real parsed hostname (urlparse(...).hostname, which Python itself
already lowercases) rather than a raw substring match anywhere in the
URL. This is strictly safer in both directions: capitalization no
longer matters, AND a host that merely *contains* an allowed/blocked
name as a substring (a lookalike/subdomain trick) is no longer
confused with the real thing either way.

Imports conftest.py directly (it's this directory's own module,
already on sys.path via pytest's own conftest-collection mechanism) to
unit-test its pure functions without spawning a real pytest subprocess
or a browser.
"""
import conftest


# ---------------------------------------------------------------------------
# extract_hostname(): case and shape normalization
# ---------------------------------------------------------------------------

def test_extract_hostname_lowercases_uppercase_production_host():
    # The exact case Codex's independent review reproduced.
    assert conftest.extract_hostname("https://APP.ANYAICAM.COM") == "app.anyaicam.com"


def test_extract_hostname_lowercases_mixed_case_staging_host():
    assert conftest.extract_hostname("https://Portal-Staging.AnyAiCam.Com") == "portal-staging.anyaicam.com"


def test_extract_hostname_ignores_scheme_port_path_query():
    assert conftest.extract_hostname("http://portal-staging.anyaicam.com:8080/playback?x=1") == "portal-staging.anyaicam.com"


def test_extract_hostname_none_for_an_unparseable_value():
    assert conftest.extract_hostname("not a url at all") is None


# ---------------------------------------------------------------------------
# is_host_allowed(): the actual decision, exercised directly and by every
# capitalization/variant a real ANYAICAM_E2E_BASE_URL value could take
# ---------------------------------------------------------------------------

def test_staging_host_is_allowed_regardless_of_case():
    for host in ("portal-staging.anyaicam.com", "PORTAL-STAGING.ANYAICAM.COM", "Portal-Staging.AnyAiCam.Com"):
        assert conftest.is_host_allowed(conftest.extract_hostname(f"https://{host}"), allow_production=False) is True


def test_production_is_blocked_by_default_lowercase():
    assert conftest.is_host_allowed("app.anyaicam.com", allow_production=False) is False


def test_production_is_blocked_even_uppercase_the_exact_bypass_codex_found():
    # Before the fix: `PRODUCTION_DOMAIN in BASE_URL` on the raw URL
    # string -- "app.anyaicam.com" in "https://APP.ANYAICAM.COM" is
    # False, so this exact case sailed through as if it were allowed.
    hostname = conftest.extract_hostname("https://APP.ANYAICAM.COM")
    assert conftest.is_host_allowed(hostname, allow_production=False) is False


def test_production_is_blocked_for_reasonable_case_variants():
    for host in ("app.anyaicam.com", "APP.ANYAICAM.COM", "App.AnyAiCam.Com", "aPP.anyaicam.COM"):
        hostname = conftest.extract_hostname(f"https://{host}")
        assert conftest.is_host_allowed(hostname, allow_production=False) is False, host


def test_production_is_allowed_only_with_explicit_opt_in():
    assert conftest.is_host_allowed("app.anyaicam.com", allow_production=True) is True


def test_production_opt_in_still_respects_case_normalization():
    hostname = conftest.extract_hostname("https://APP.ANYAICAM.COM")
    assert conftest.is_host_allowed(hostname, allow_production=True) is True


def test_an_unrecognized_host_is_blocked_not_just_production():
    # The whole point of an allowlist over a blocklist: a typo or a
    # brand-new environment is rejected by default too, not silently
    # treated as "fine, it's not the one name I know to block."
    for hostname in ("staging.anyaicam.com", "portal.anyaicam.com", "localhost", "example.com"):
        assert conftest.is_host_allowed(hostname, allow_production=False) is False
        assert conftest.is_host_allowed(hostname, allow_production=True) is False


def test_lookalike_hosts_that_merely_contain_the_staging_name_are_rejected():
    # A raw substring check on the full URL would have let these
    # through too (they all *contain* "portal-staging.anyaicam.com" as
    # text) -- the hostname-only, exact-match comparison does not.
    for host in (
        "portal-staging.anyaicam.com.evil.com",
        "evil-portal-staging.anyaicam.com",
        "notportal-staging.anyaicam.com",
    ):
        hostname = conftest.extract_hostname(f"https://{host}")
        assert conftest.is_host_allowed(hostname, allow_production=False) is False, host


def test_lookalike_hosts_that_merely_contain_the_production_name_are_not_treated_as_production():
    # These must be rejected too (they're not on the allowlist), but for
    # a different reason than real production -- worth its own case so
    # a future change can't accidentally start treating "contains
    # app.anyaicam.com" as equivalent to "is app.anyaicam.com".
    for host in ("app.anyaicam.com.evil.com", "notapp.anyaicam.com"):
        hostname = conftest.extract_hostname(f"https://{host}")
        assert hostname != conftest.PRODUCTION_DOMAIN
        assert conftest.is_host_allowed(hostname, allow_production=True) is False, host


def test_none_hostname_is_never_allowed():
    assert conftest.is_host_allowed(None, allow_production=False) is False
    assert conftest.is_host_allowed(None, allow_production=True) is False


def test_the_default_base_url_constant_itself_is_on_the_allowlist():
    """Guards against the allowlist and the default drifting apart --
    if DEFAULT_STAGING_BASE_URL is ever repointed, this fails loudly
    instead of every test silently starting to refuse to run."""
    assert conftest.extract_hostname(conftest.DEFAULT_STAGING_BASE_URL) in conftest.ALLOWED_STAGING_HOSTS
