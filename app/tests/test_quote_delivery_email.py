"""The quote email carries the quote and a sign-in link, and never the partner's confidential numbers (Green 2026-10-17).

It was the bare sentence "Your AnyAiCam quote is ready for review." with no amounts and no link. The stored quote
(totals_json, from pricing_config.calculate_partner_quote) also holds partner cost, wholesale price, profit, margin
and legacy retention terms; only cameras, price per camera and the monthly total may reach the customer.
"""
import json

from cloud_features import quote_delivery_email

TOTALS = {
    "quantity": 6, "selling_price_per_camera": 21.5, "monthly_customer_revenue": 129.0,
    "partner_price_per_camera": 11.11, "partner_cost_per_camera": 7.77, "partner_profit_per_camera": 10.39,
    "partner_margin_percent": 48.33, "monthly_partner_charge": 66.66, "monthly_recurring_profit": 62.34,
    "installation_profit": 333.33, "hardware_profit": 444.44, "first_year_profit": 1525.85,
    "retention_days": 14, "volume_tier": "Gold", "partner_confidential": True,
}
QUOTE = {"id": "q-123abc", "totals_json": json.dumps(TOTALS)}


def test_the_quote_email_shows_the_customer_quote_and_a_sign_in_link(monkeypatch):
    monkeypatch.setenv("ANYAICAM_PUBLIC_URL", "https://app.example.test/")
    subject, text, html = quote_delivery_email(QUOTE)
    assert subject == "Your AnyAiCam quote"
    for expected in ("Quote reference: q-123abc", "Cameras: 6", "Price per camera: $21.50 per month",
                     "Monthly total: $129.00 per month", "https://app.example.test/customer-login.html"):
        assert expected in text
        assert expected.replace("&", "&amp;") in html or expected in html


def test_partner_confidential_numbers_and_retention_never_reach_the_customer():
    subject, text, html = quote_delivery_email(QUOTE)
    body = subject + text + html
    for secret in ("11.11", "7.77", "10.39", "48.33", "66.66", "62.34", "333.33", "444.44", "1,525.85", "1525.85",
                   "Gold", "margin", "profit", "wholesale", "14 days", "retention"):
        assert secret not in body, secret


def test_a_quote_with_missing_or_broken_totals_still_sends_a_sane_email():
    for quote in ({"id": "q-1", "totals_json": "not json"}, {"id": "q-2"}, {"id": "q-3", "totals_json": "{}"}):
        subject, text, html = quote_delivery_email(quote)
        assert "Quote reference: " + quote["id"] in text and "None" not in text and "$" not in text
