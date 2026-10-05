"""Bluehost upload package (tools/website_publish_package.py), offline.

The live site is replaced by a fake fetcher, so these tests never touch anyaicam.com."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import website_publish_package as package  # noqa: E402

WEBSITE = ROOT / "website"
BEACON = ('<script type="module" src="https://static.cloudflareinsights.com/beacon.min.js/v1" '
          'data-cf-beacon=\'{"token":"x"}\' crossorigin="anonymous"></script>\n')


def cloudflare(data: bytes) -> bytes:
    """What Cloudflare serves for a page: beacon injected, e-mail addresses obfuscated."""
    text = data.decode("utf-8")
    text = text.replace("mailto:amata@anyaicam.com", "/cdn-cgi/l/email-protection#0e6f636f")
    text = text.replace("amata@anyaicam.com", '<span class="__cf_email__" data-cfemail="0e6f">[email&#160;protected]</span>')
    return text.replace("</body>", BEACON + "</body>").encode("utf-8")


def test_runtime_files_exclude_repository_notes_and_example_config():
    files = package.runtime_files()
    assert "index.html" in files and ".htaccess" in files and "PHPMailer/PHPMailer.php" in files
    for name in ("README.md", "SOURCE_MANIFEST.json", "SERVER_CLEANUP.json", ".gitignore", "stripe-config.example.php"):
        assert name not in files
    assert not [f for f in files if f.endswith(".md")]


def test_cloudflare_rewriting_alone_is_not_a_change():
    page = (WEBSITE / "contact.html").read_bytes()
    assert package.normalize(cloudflare(page), "contact.html") == package.normalize(page, "contact.html")
    edited = page.replace(b"</body>", b"<p>New</p></body>")
    assert package.normalize(cloudflare(page), "contact.html") != package.normalize(edited, "contact.html")


def test_php_is_never_requested():
    def fetcher(rel):
        raise AssertionError(f"requested {rel}")
    entry = package.classify("stripe-webhook.php", (WEBSITE / "stripe-webhook.php").read_bytes(), fetcher, lambda rel: [])
    assert entry["status"].startswith("on host")
    changed = package.classify("stripe-webhook.php", b"<?php // edited", fetcher, lambda rel: [])
    assert changed["upload"] and "server-side" in changed["special"]


def test_changed_page_uploads_the_source_and_rolls_back_to_the_host_source_not_the_cloudflare_bytes():
    host_source = b"<html><body><p>Old</p><a href=\"mailto:amata@anyaicam.com\">amata@anyaicam.com</a></body></html>"
    entry = package.classify("vms.html", b"<html><body><p>New</p></body></html>",
                             lambda rel: (200, cloudflare(host_source)), lambda rel: [b"<p>other</p>", host_source])
    assert entry["upload"] and entry["rollback"] == host_source and "special" not in entry


def test_new_page_and_unmatched_live_page_are_reported():
    assert package.classify("vms-linux.html", b"x", lambda rel: (404, b""), lambda rel: []) == {"status": "new", "upload": True}
    entry = package.classify("vms.html", b"<p>New</p>", lambda rel: (200, b"<p>Edited on the host</p>"), lambda rel: [])
    assert entry["rollback"] == b"<p>Edited on the host</p>" and "matches no repository version" in entry["special"]


def test_build_refuses_a_pending_release_unless_preparing(tmp_path):
    import shutil
    import website_release
    copy = tmp_path / "website"
    shutil.copytree(WEBSITE, copy)
    website_release.apply(None, copy)  # back to "release finalizing"
    with pytest.raises(SystemExit, match="release blocks are still pending"):
        package.build(tmp_path / "pkg", website=copy, fetcher=lambda rel: (200, b""), history=lambda rel: [])


def test_preparation_build_lists_uploads_checksums_and_host_cleanup(tmp_path):
    live = {"index.html": cloudflare((WEBSITE / "index.html").read_bytes()), "anyaicam-test-checkout.html": b"test"}

    def fetcher(rel):
        if rel == "vms-linux.html":
            return 404, b""
        if rel in live:
            return 200, live[rel]
        if rel == "vms.html":
            return 200, b"<p>older vms page</p>"
        if (WEBSITE / rel).is_file():
            return 200, (WEBSITE / rel).read_bytes()
        return 404, b""

    out = tmp_path / "pkg"
    manifest = package.build(out, allow_pending=True, fetcher=fetcher, history=lambda rel: [b"<p>older vms page</p>"])
    uploads = sorted(rel for rel, r in manifest["files"].items() if r.get("upload"))
    assert uploads == ["vms-linux.html", "vms.html"]
    assert (out / "upload" / "vms.html").read_bytes() == (WEBSITE / "vms.html").read_bytes()
    assert (out / "rollback-original" / "vms.html").read_bytes() == b"<p>older vms page</p>"
    assert not (out / "rollback-original" / "vms-linux.html").exists()
    sums = (out / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert f"{manifest['files']['vms.html']['sha256']}  vms.html" in sums
    assert "anyaicam-test-checkout.html" in manifest["delete_from_host"]
    assert "anyaicam-test-webhook.php" in manifest["delete_from_host"]
    assert "CMHT1722-28LS, 2MP HD-TVI.html" not in manifest["delete_from_host"]
    publish = (out / "PUBLISH.md").read_text(encoding="utf-8")
    # The committed site names the published 1.2.3 release, so this is a real (uploadable) build.
    assert "preparation build" not in publish and "release blocks published (1.2.3" in publish and "`vms-linux.html` (new)" in publish
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8"))["files"]["index.html"]["status"] == "same as live"
