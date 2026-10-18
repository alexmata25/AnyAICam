"""The support address in customer emails is configuration (Green 2026-10-17; Red: env, with the owner's address as the
default). A malformed value never reaches an email."""
import pytest

import purchase_notifications


def test_the_default_is_the_owner_address(monkeypatch):
    monkeypatch.delenv("ANYAICAM_SUPPORT_EMAIL", raising=False)
    assert purchase_notifications._support_email() == "amata@anyaicam.com"


def test_the_environment_sets_it(monkeypatch):
    monkeypatch.setenv("ANYAICAM_SUPPORT_EMAIL", "  support@anyaicam.com ")
    assert purchase_notifications._support_email() == "support@anyaicam.com"


@pytest.mark.parametrize("bad", ["", "support", "support@localhost", "a b@anyaicam.com", '"x"@anyaicam.com',
                                 "<support@anyaicam.com>", "x" * 250 + "@anyaicam.com"])
def test_a_malformed_value_falls_back_to_the_default(monkeypatch, bad):
    monkeypatch.setenv("ANYAICAM_SUPPORT_EMAIL", bad)
    assert purchase_notifications._support_email() == "amata@anyaicam.com"


def test_the_footer_uses_the_configured_address():
    assert purchase_notifications.SUPPORT_EMAIL in purchase_notifications._SUPPORT_FOOTER_TEXT
    assert f'mailto:{purchase_notifications.SUPPORT_EMAIL}' in purchase_notifications._SUPPORT_FOOTER_HTML
