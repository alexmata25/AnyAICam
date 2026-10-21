"""Owner Dell VM acceptance (2026-10-19, 1.2.6 -> 1.2.7 upgrade, appliance
linked, 0 cameras) -- MASTER-CHECKLIST-1.2.7.md section 1a, reproduced with
the real app (TestClient(main.app)), a signed customer_owner cookie and a
throwaway sqlite DB.

1. "Live View redirects to onboarding Step 6": /customer-live with no camera
   -> /customer-account -> /customer/setup is INTENDED for an activated
   appliance with no camera plan and no camera (see
   test_customer_account_install_state.py::
   test_truly_unprovisioned_customer_still_goes_to_setup). Setup reopened on
   the saved draft's step (6) and never said why; it now does, with the way
   forward (live_view_waits_for_camera_plan()).
2. "Step 7 confirmation blank; progress indicator inconsistent": the tabs and
   "Step N of 7" were rendered for the saved step but the panels were
   hard-coded with step 1 visible (Welcome under "Step 7 of 7" until/unless
   the page script ran); Step 7 listed nothing, and for an account with no
   camera plan its "Confirm and open dashboard" landed back on Step 7.
3. "One-click secure appliance linking primary, manual claim code fallback":
   the appliance's own AnyAiCam Setup page and the installer download page
   already lead with "Link this appliance" (no codes); /customer/setup's
   Add appliance step led with the claim code. It now leads with the
   one-click link and keeps the claim code (and Cloud ID + token) as fallbacks.
"""

import re
import sqlite3

import pytest

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_onboarding_owner_vm_127.db"


@pytest.fixture()
def http_client(db_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.delenv("ANYAICAM_RUNTIME_ROLE", raising=False)  # the cloud portal
    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _seed_owner_with_linked_appliance(db_path, *, draft_step=None):
    """The owner's VM state: one activated, enrolled and checked-in appliance,
    no camera, no camera plan."""
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) "
                 "VALUES('cust-1','partner-1','Owner VM','owner@example.test','active','2026-01-01')")
    conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main Site','2026-01-01')")
    conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,online_status,last_check_in,created_at) "
                 "VALUES('appl-1','cust-1','site-1','AIC-OWNERVM1','activated','online','2026-10-19T10:00:00','2026-01-01')")
    conn.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at,created_by,last_used_at) "
                 "VALUES('cred-1','appl-1','x','2026-01-01','activation','2026-10-19T10:00:00')")
    if draft_step is not None:
        conn.execute("INSERT INTO customer_setup_drafts(customer_id,current_step,data_json,updated_at) VALUES(?,?,?,?)",
                     ("cust-1", draft_step, '{"appliance_id":"appl-1"}', "2026-10-19"))
    conn.commit()
    conn.close()


def _seed_camera_plan(db_path, slots=8):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,stripe_checkout_session_id,created_at,updated_at) "
        "VALUES('ent-1','cust-1','camera_slots_local',?,'active','cs_test_owner','2026-01-01','2026-01-01')", (slots,))
    conn.commit()
    conn.close()


def _cookies():
    return {partner_portal.SESSION_COOKIE: partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None)}


def _visible_panels(html):
    panels = re.findall(r'<div class="customer-setup-step" data-step="(\d)"( hidden)?>', html)
    assert len(panels) == 7
    return [step for step, hidden in panels if not hidden]


# ----------------------------------------------------------------- defect 1

def test_live_view_without_camera_plan_still_goes_to_setup_and_setup_says_why(http_client, db_path):
    _seed_owner_with_linked_appliance(db_path, draft_step=6)

    live = http_client.get("/customer-live", cookies=_cookies())
    assert live.status_code == 303 and live.headers["location"] == "/customer-account"
    account = http_client.get("/customer-account", cookies=_cookies())
    assert account.status_code == 303 and account.headers["location"] == "/customer/setup"  # intended, unchanged

    setup = http_client.get("/customer/setup", cookies=_cookies())
    assert setup.status_code == 200
    assert 'id="live-view-needs-plan"' in setup.text
    assert "Live View opens once you choose a camera plan." in setup.text
    assert 'id="live-view-needs-plan-link" href="/subscription-portal"' in setup.text
    # The explanation sits above the steps, whichever step the draft reopens.
    assert setup.text.index('id="live-view-needs-plan"') < setup.text.index('id="customer-setup-tabs"')


def test_no_live_view_notice_once_the_account_has_a_camera_plan(http_client, db_path):
    _seed_owner_with_linked_appliance(db_path, draft_step=6)
    _seed_camera_plan(db_path)

    assert http_client.get("/customer-live", cookies=_cookies()).headers["location"] == "/customer-account"
    assert http_client.get("/customer-account", cookies=_cookies()).status_code == 200  # dashboard, not setup
    setup = http_client.get("/customer/setup", cookies=_cookies())
    assert 'id="live-view-needs-plan"' not in setup.text


def test_no_live_view_notice_before_the_appliance_is_activated(http_client, db_path):
    """A customer still linking belongs in setup for that reason; the notice
    is only for the activated-but-no-plan bounce."""
    _seed_owner_with_linked_appliance(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE appliance_credentials SET revoked_at='2026-10-19' WHERE id='cred-1'")
    conn.commit()
    conn.close()
    setup = http_client.get("/customer/setup", cookies=_cookies())
    assert 'id="live-view-needs-plan"' not in setup.text


# ----------------------------------------------------------------- defect 2

@pytest.mark.parametrize("step", range(1, 8))
def test_progress_indicator_and_visible_panel_agree_on_every_step(http_client, db_path, step):
    _seed_owner_with_linked_appliance(db_path, draft_step=step)
    html = http_client.get("/customer/setup", cookies=_cookies()).text

    assert _visible_panels(html) == [str(step)]
    assert f"Step <strong>{step}</strong> of 7" in html
    active = re.findall(r'class="workspace-tab active" data-step="(\d)" aria-current="step"', html)
    assert active == [str(step)]
    assert re.search(r'<script>let setupStep=(\d+),', html).group(1) == str(step)
    back_hidden = '<button class="ghost-button" id="customer-setup-back" hidden>' in html
    next_hidden = '<button class="action-button" id="customer-setup-next" hidden>' in html
    assert back_hidden == (step == 1)
    assert next_hidden == (step == 7)


def test_held_at_status_step_shows_the_status_panel_not_welcome(http_client, db_path):
    """Not yet enrolled: a saved step 6 is held at 3 -- the indicator AND the
    panel both say 3."""
    _seed_owner_with_linked_appliance(db_path, draft_step=6)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE appliance_credentials SET last_used_at=NULL")
    conn.commit()
    conn.close()
    html = http_client.get("/customer/setup", cookies=_cookies()).text
    assert _visible_panels(html) == ["3"]
    assert "Step <strong>3</strong> of 7" in html


def _step7(html):
    return html[html.index('<div class="customer-setup-step" data-step="7"'):html.index('class="dialog-actions"')]


def test_step7_lists_what_is_confirmed_and_where_it_leads_without_a_plan(http_client, db_path):
    _seed_owner_with_linked_appliance(db_path, draft_step=7)
    step7 = _step7(http_client.get("/customer/setup", cookies=_cookies()).text)
    assert 'id="confirm-setup-summary"' in step7
    assert "Appliance: AnyAiCam appliance (Online)" in step7
    assert "Cameras: 0 cameras added" in step7
    assert "Camera plan: No camera plan purchased yet" in step7
    assert "Analytics: None purchased" in step7
    assert "Local recording: " in step7
    assert 'id="confirm-setup-next-note"' in step7
    assert ">Confirm and choose a camera plan</button>" in step7


def test_step7_with_a_camera_plan_opens_the_dashboard(http_client, db_path):
    _seed_owner_with_linked_appliance(db_path, draft_step=7)
    _seed_camera_plan(db_path, slots=8)
    step7 = _step7(http_client.get("/customer/setup", cookies=_cookies()).text)
    assert "Cameras: 0 of 8 licensed cameras added" in step7
    assert "Camera plan: Local &middot; 8 licensed cameras" in step7
    assert 'id="confirm-setup-next-note"' not in step7
    assert ">Confirm and open dashboard</button>" in step7


def test_confirm_without_a_plan_goes_to_my_subscription_not_back_to_step7(http_client, db_path):
    _seed_owner_with_linked_appliance(db_path, draft_step=7)
    response = http_client.post("/api/customer/setup/confirm", json={"appliance_id": "appl-1"}, cookies=_cookies())
    assert response.status_code == 200
    assert response.json()["redirect"] == "/subscription-portal"
    # The old destination is still the setup bounce for this account.
    assert http_client.get("/customer-account", cookies=_cookies()).headers["location"] == "/customer/setup"


def test_confirm_with_a_plan_still_opens_the_dashboard(http_client, db_path):
    _seed_owner_with_linked_appliance(db_path, draft_step=7)
    _seed_camera_plan(db_path)
    response = http_client.post("/api/customer/setup/confirm", json={"appliance_id": "appl-1"}, cookies=_cookies())
    assert response.status_code == 200
    assert response.json() == {"message": "Setup confirmed and saved.", "redirect": "/customer-account"}
    assert http_client.get("/customer-account", cookies=_cookies()).status_code == 200


# ----------------------------------------------------------------- item 3

def _step2(html):
    return html[html.index('<div class="customer-setup-step" data-step="2"'):html.index('<div class="customer-setup-step" data-step="3"')]


def test_add_appliance_leads_with_one_click_link_and_keeps_claim_code_fallback(http_client, db_path):
    _seed_owner_with_linked_appliance(db_path, draft_step=2)
    step2 = _step2(http_client.get("/customer/setup", cookies=_cookies()).text)
    one_click = step2.index('id="one-click-link-panel"')
    assert one_click < step2.index('id="claim-appliance-panel"') < step2.index('id="customer-cloud-id"')
    assert "Link from the appliance (recommended)" in step2
    assert "<strong>Link this appliance</strong>" in step2  # link_server.py's button
    assert "Link this computer to AnyAiCam" in step2  # Windows Start menu entry (AnyAiCam-VMS.iss)
    assert "No codes to copy" in step2
    # The manual claim code stays available, as the fallback.
    assert "Or claim your appliance with its code" in step2
    assert 'href="/customer/claim-appliance?return=setup"' in step2


def test_appliance_setup_page_offers_only_the_one_click_link():
    """The appliance's own AnyAiCam Setup page (link_server.py): one button,
    no code shown -- the manual code exists only via `anyaicam-setup --claim`."""
    import logging
    from types import SimpleNamespace
    from anyaicam_agent.link_server import LinkServer

    server = LinkServer(SimpleNamespace(portal_url="https://portal.example"), logging.getLogger("test"),
                        headless=lambda: object(), linked=lambda: False, port=0)  # bound, never started
    try:
        page = server.page()
    finally:
        server.httpd.server_close()
    assert '<button class="button" type="submit">Link this appliance</button>' in page
    assert "there is nothing to copy or type" in page
    assert "claim code" not in page.lower()
