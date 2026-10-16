"""Every analytic the Live View row lists (all ANALYTIC_KEYS, for every camera)
needs upgrade-card copy. The rule analytics (line_crossing, intrusion,
loitering; ae06971) had none, so /api/customer/cameras/{id}/analytics raised
KeyError -> 500 for every camera."""
from customer_analytics_panel import ANALYTIC_KEYS, UPGRADE_CARD_CONTENT


def test_every_listed_analytic_has_upgrade_card_copy():
    missing = [key for key in ANALYTIC_KEYS if key not in UPGRADE_CARD_CONTENT]
    assert missing == []
    for key in ANALYTIC_KEYS:
        card = UPGRADE_CARD_CONTENT[key]
        assert card["description"].strip() and card["benefits"]
