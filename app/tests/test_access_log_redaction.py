"""Password-reset tokens and similar query-string secrets never appear in
access-log lines (2026-09-28)."""
import logging

import access_log_redaction as redaction


def test_reset_token_is_redacted_and_the_rest_of_the_url_kept():
    line = redaction.redact_url("/customer-reset-password?token=abc123SECRET&lang=en")
    assert line == "/customer-reset-password?token=<redacted>&lang=en"


def test_other_sensitive_names_are_redacted_case_insensitively():
    assert redaction.redact_url("/cb?Code=xyz&state=ok") == "/cb?Code=<redacted>&state=ok"
    assert redaction.redact_url("/x?api_key=k1;signature=s2") == "/x?api_key=<redacted>;signature=<redacted>"


def test_non_sensitive_urls_are_unchanged():
    for url in ("/playback?camera=abc&event=e1&autoplay=event", "/health", "/customer-login.html?next=%2Fplayback"):
        assert redaction.redact_url(url) == url


def test_uvicorn_access_record_is_rewritten_before_formatting(caplog):
    redaction.install()
    logger = logging.getLogger("uvicorn.access")
    logger.propagate = True
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logger.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/reset-password?token=TOPSECRET", "1.1", 200)
    assert "TOPSECRET" not in caplog.text and "token=<redacted>" in caplog.text


def test_install_is_idempotent():
    redaction.install()
    redaction.install()
    assert logging.getLogger("uvicorn.access").filters.count(redaction._FILTER) == 1


def test_the_app_installs_the_filter_at_startup():
    import main  # noqa: F401
    assert redaction._FILTER in logging.getLogger("uvicorn.access").filters
