"""Home Screen app (PWA) manifest and icons (2026-09-30).

Found on staging: /manifest.webmanifest redirected to the sign-in page, so
browsers reported "Manifest: Line: 1, column: 1, Syntax error" and a phone
could not read the app's name, icon or standalone display -- which iPhone
web push depends on. The offline page (pre-cached by the service worker)
redirected too, and the declared icons were a 1280x906 image labelled
192x192/512x512.
"""
import json
import struct
from pathlib import Path

from fastapi.testclient import TestClient

STATIC = Path(__file__).resolve().parents[1] / "static"


def _png_size(path):
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", path.name
    return struct.unpack(">II", header[16:24])


def _client():
    import main
    return TestClient(main.app, follow_redirects=False)


def test_manifest_is_public_valid_json_with_standalone_display():
    response = _client().get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = json.loads(response.text)
    assert manifest["display"] == "standalone"
    assert manifest["name"] and manifest["short_name"] and manifest["start_url"]


def test_manifest_icons_exist_and_match_their_declared_sizes():
    manifest = _client().get("/manifest.webmanifest").json()
    icons = manifest["icons"] + [icon for shortcut in manifest.get("shortcuts", []) for icon in shortcut.get("icons", [])]
    assert {i.get("purpose", "any") for i in manifest["icons"]} >= {"any", "maskable"}
    for icon in icons:
        path = STATIC / icon["src"].removeprefix("/static/")
        width, height = _png_size(path)
        declared = tuple(int(v) for v in icon["sizes"].split("x"))
        assert (width, height) == declared, (icon["src"], (width, height), declared)


def test_offline_page_is_public_and_uses_customer_wording():
    response = _client().get("/offline")
    assert response.status_code == 200
    assert "Samsung" not in response.text


def test_portal_pages_link_a_square_iphone_icon_and_the_manifest():
    import main
    source = Path(main.__file__).read_text(encoding="utf-8")
    assert '<link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">' in source
    assert '<link rel="manifest" href="/manifest.webmanifest">' in source
    assert _png_size(STATIC / "apple-touch-icon.png") == (180, 180)


def test_only_the_static_app_files_became_public():
    client = _client()
    for path in ("/customer-portal", "/dashboard", "/api/mobile/push/devices"):
        assert client.get(path).status_code in (303, 307, 401, 403), path
