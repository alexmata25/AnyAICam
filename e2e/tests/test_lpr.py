"""Target: LPR (license plate recognition). Not a standalone page -- it's
an entitlement-gated analytic surfaced two ways (confirmed from source):
(1) the focused Live View's per-camera analytics pill row
(app/customer_analytics_panel.py: ANALYTIC_LABELS['lpr'], via
GET /api/customer/cameras/{camera_id}/analytics/lpr/summary), and
(2) as event markers/filters on /playback (filterCategory() maps
'plate'/'lpr' event_type -> the 'lpr' category, color #3dbfae -- see
test_playback.py). Real per-camera entitlement and DOM selectors are TODO.
"""
import pytest


@pytest.mark.e2e
def test_lpr_pill_appears_only_for_an_entitled_camera(page, base_url, e2e_credentials):
    pytest.skip("needs a real camera_id known to have the lpr entitlement -- not yet confirmed which one qualifies")


@pytest.mark.e2e
def test_lpr_summary_shows_a_real_recent_plate(page, base_url, e2e_credentials):
    pytest.skip("needs a real, recent lpr detection event to exist -- not yet confirmed on this test tenant")
