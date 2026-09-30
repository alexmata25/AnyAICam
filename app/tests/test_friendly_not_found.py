"""Friendly not-found page for people (2026-09-30): a signed-in browser following an
old link got raw JSON ({"detail":"Not Found"}); API calls keep their JSON errors."""
import main
import partner_portal
from test_live_view_black_tile_fix import client, db_path, _owner_cookie  # noqa: F401  (shared fixtures)

BROWSER = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def _get(client, path, headers=None):
    return client.get(path, headers=headers or {}, cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}, follow_redirects=False)


def test_a_browser_gets_a_friendly_page_with_a_404_status(client):
    response = _get(client, "/this-page-does-not-exist", BROWSER)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Page not found" in response.text and '"detail"' not in response.text


def test_an_old_voice_call_link_says_the_call_is_gone(client):
    response = _get(client, "/aac/voice-call/not-a-real-call", BROWSER)
    assert response.status_code == 404 and "This Voice Call is no longer available" in response.text


def test_api_and_script_requests_keep_json_errors(client):
    api = _get(client, "/api/customer/aac/voice-call/events/not-a-real-call", BROWSER)
    assert api.status_code == 404 and api.headers["content-type"].startswith("application/json")
    plain = _get(client, "/this-page-does-not-exist")          # no HTML Accept header
    assert plain.status_code == 404 and plain.json() == {"detail": "Not Found"}
    static = _get(client, "/static/nope.js", BROWSER)
    assert static.status_code == 404 and "Page not found" not in static.text
