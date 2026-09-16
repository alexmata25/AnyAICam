"""Target: PPE (personal protective equipment detection). Same shape as
LPR (see test_lpr.py) -- an entitlement-gated analytic
(app/customer_analytics_panel.py: ANALYTIC_LABELS['ppe'], via
GET /api/customer/cameras/{camera_id}/analytics/ppe/summary), not a
standalone page. Not currently a Playback marker/filter category
(filterCategory() has no 'ppe' case -- see test_playback.py's own
module docstring) -- worth confirming that's still true on first
authenticated run, not assuming it stays that way forever.
"""
import pytest


@pytest.mark.e2e
def test_ppe_pill_appears_only_for_an_entitled_camera(page, base_url, e2e_credentials):
    pytest.skip("needs a real camera_id known to have the ppe entitlement -- not yet confirmed which one qualifies")


@pytest.mark.e2e
def test_ppe_summary_reflects_a_real_recent_detection(page, base_url, e2e_credentials):
    pytest.skip("needs a real, recent ppe detection event to exist -- not yet confirmed on this test tenant")
