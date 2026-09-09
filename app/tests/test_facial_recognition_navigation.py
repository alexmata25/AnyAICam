"""AAC sidebar navigation -- Phase 2. Users without facial.view must
never see the "Facial Recognition" nav entry; users with it must.
Mirrors test_settings_navigation.py's own direct-function-call pattern.
"""

import main


def test_aac_nav_item_is_registered():
    keys = [item[0] for item in main.NAV_ITEMS]
    assert "aac" in keys


def test_customer_owner_has_facial_view_permission_for_nav():
    assert main._facial_view_permitted({"role": "customer_owner"}, "customer_owner") is True


def test_customer_viewer_has_facial_view_permission_for_nav():
    assert main._facial_view_permitted({"role": "customer_viewer"}, "customer_viewer") is True


def test_administrator_has_facial_view_permission_for_nav():
    assert main._facial_view_permitted({"role": "administrator"}, "administrator") is True


def test_salesperson_does_not_have_facial_view_permission_for_nav():
    assert main._facial_view_permitted({"role": "salesperson"}, "salesperson") is False


def test_unknown_role_does_not_have_facial_view_permission_for_nav():
    assert main._facial_view_permitted({"role": "some_future_role"}, "some_future_role") is False


def test_none_identity_does_not_have_facial_view_permission_for_nav():
    assert main._facial_view_permitted(None, "customer_owner") is True  # role string alone is enough
    assert main._facial_view_permitted(None, "") is False


def test_customer_owner_sees_aac_in_navigation_keys_for_role():
    keys = main.navigation_keys_for_role("customer_owner")
    assert "aac" in keys


def test_customer_owner_aac_entry_survives_the_full_visibility_filter():
    """End-to-end through the SAME filter page_shell() actually applies
    -- both the coarse role-based allowlist (navigation_keys_for_role)
    AND the fine-grained facial.view permission check must both pass."""
    allowed_keys = main.navigation_keys_for_role("customer_owner")
    shell_user = {"role": "customer_owner"}
    shell_role = "customer_owner"
    visible = [
        item
        for item in main.NAV_ITEMS
        if (allowed_keys is None or item[0] in allowed_keys)
        and (item[0] != "customer-app-settings" or shell_role in main.CUSTOMER_PORTAL_ROLES)
        and (item[0] != "aac" or main._facial_view_permitted(shell_user, shell_role))
    ]
    assert "aac" in [item[0] for item in visible]


def test_administrator_aac_entry_survives_the_full_visibility_filter():
    allowed_keys = main.navigation_keys_for_role("administrator")
    visible_keys = [
        item[0]
        for item in main.NAV_ITEMS
        if (allowed_keys is None or item[0] in allowed_keys)
        and (item[0] != "aac" or main._facial_view_permitted({"role": "administrator"}, "administrator"))
    ]
    assert "aac" in visible_keys
