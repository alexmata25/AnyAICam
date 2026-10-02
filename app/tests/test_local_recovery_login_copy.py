"""admin@local is bootstrap/emergency-recovery access only, never the
normal day-to-day Administrator identity -- see this session's Samsung
identity-architecture audit. Nothing in the product links to /login any
more (the real Administrator/Partner/Technician entry point is
partner.html's Portal login, with its own role selector), but anyone
who navigates here directly must immediately understand this is
recovery access, not their normal sign-in, and be pointed at the real
one instead of guessing.
"""
import main


def test_login_page_reads_as_local_emergency_recovery_not_normal_sign_in():
    html = main.login_page_html()
    assert "Local emergency recovery sign-in" in html
    assert "<title>Local emergency recovery sign-in · AnyAiCam</title>" in html


def test_login_page_points_operators_at_the_real_portal_login():
    html = main.login_page_html()
    assert 'href="/partner.html"' in html


def test_login_page_no_longer_reads_as_a_generic_sign_in_page():
    html = main.login_page_html()
    assert "<h1>Sign in</h1>" not in html


def test_login_form_structure_is_unchanged_by_the_copy_update():
    # The relabel must not touch the CSRF-carrying form contract.
    html = main.login_page_html()
    assert 'action="/login" id="login-form"' in html
    assert '<input type="hidden" name="csrf_token" value="">' in html
