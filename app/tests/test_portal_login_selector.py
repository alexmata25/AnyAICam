"""Regression coverage for resolve_portal_login() -- the pure decision
behind POST /api/portal-login, the blue Portal login page's
Administrator/Partner/Technician selector.

Root cause this replaces: the blue portal login page (partner.html)
only ever posted to /api/partner-login with partner_only:true, which
checks partner_db exclusively and never the legacy Admin Portal's own
users.json store. An email with a real account in *both* systems (a
common, legitimate case -- an operator who is both a legacy platform
administrator and a Partner Portal administrator/owner) always landed
on the Partner Portal, silently, with no way to reach the Admin
Portal from that page at all. resolve_portal_login() fixes this by
deciding between BOTH systems' validated results instead of only ever
consulting one.
"""
import main


def test_selects_legacy_administrator_when_only_legacy_validates():
    decision = main.resolve_portal_login(selected_portal=None, legacy_role="administrator", partner_role=None)
    assert decision["system"] == "legacy"
    assert decision["destination"] == "/admin-portal"
    assert decision["available"] == ["administrator"]


def test_selects_partner_administrator_when_only_partner_validates():
    decision = main.resolve_portal_login(selected_portal=None, legacy_role=None, partner_role="administrator")
    assert decision["system"] == "partner"
    assert decision["destination"] == "/partner?tab=customers"


def test_never_guesses_between_legacy_and_partner_administrator_without_a_selection():
    decision = main.resolve_portal_login(selected_portal=None, legacy_role="administrator", partner_role="administrator")
    assert decision["system"] is None
    assert decision["destination"] is None
    assert decision["available"] == ["administrator"]


def test_same_email_both_identities_selecting_administrator_routes_to_legacy_admin_portal():
    # This is the exact case reported: one email, both a legacy Admin
    # Portal account and a Partner Portal 'administrator' account.
    # Selecting Administrator must deterministically reach the Admin
    # Portal, never the Partner Portal, and never require a guess.
    decision = main.resolve_portal_login(selected_portal="administrator", legacy_role="administrator", partner_role="administrator")
    assert decision["system"] == "legacy"
    assert decision["destination"] == "/admin-portal"


def test_same_email_both_identities_selecting_partner_is_rejected_when_only_administrator_available_on_partner_side():
    # partner_role='administrator' is not a member of the 'partner'
    # bucket (partner_owner/salesperson) -- selecting Partner for an
    # account whose only partner_db role is 'administrator' must not
    # silently reinterpret the selection.
    decision = main.resolve_portal_login(selected_portal="partner", legacy_role="administrator", partner_role="administrator")
    assert decision["system"] is None
    assert decision["available"] == ["administrator"]


def test_selects_partner_owner_bucket():
    decision = main.resolve_portal_login(selected_portal="partner", legacy_role=None, partner_role="partner_owner")
    assert decision["system"] == "partner"
    assert decision["role"] == "partner_owner"
    assert decision["destination"] == "/partner?tab=customers"


def test_selects_salesperson_under_partner_bucket():
    decision = main.resolve_portal_login(selected_portal="partner", legacy_role=None, partner_role="salesperson")
    assert decision["system"] == "partner"
    assert decision["role"] == "salesperson"
    assert decision["destination"] == "/partner-quotes"


def test_selects_technician_bucket():
    decision = main.resolve_portal_login(selected_portal="technician", legacy_role=None, partner_role="technician")
    assert decision["system"] == "partner"
    assert decision["destination"] == "/partner/appliance-dashboard"


def test_selecting_technician_for_a_partner_owner_account_is_rejected_not_reinterpreted():
    decision = main.resolve_portal_login(selected_portal="technician", legacy_role=None, partner_role="partner_owner")
    assert decision["system"] is None
    assert decision["available"] == ["partner"]


def test_selecting_administrator_for_an_account_with_neither_identity_is_rejected():
    decision = main.resolve_portal_login(selected_portal="administrator", legacy_role=None, partner_role=None)
    assert decision["system"] is None
    assert decision["available"] == []


def test_single_identity_auto_selects_without_a_selection_backward_compatible():
    decision = main.resolve_portal_login(selected_portal=None, legacy_role=None, partner_role="technician")
    assert decision["system"] == "partner"
    assert decision["role"] == "technician"


def test_unknown_selected_portal_value_is_treated_as_no_selection():
    decision = main.resolve_portal_login(selected_portal="root", legacy_role="administrator", partner_role=None)
    assert decision["system"] == "legacy"  # single available identity, falls back to auto-select


def test_available_lists_every_bucket_this_account_could_have_chosen():
    decision = main.resolve_portal_login(selected_portal="technician", legacy_role="administrator", partner_role="salesperson")
    assert decision["system"] is None
    assert set(decision["available"]) == {"administrator", "partner"}
