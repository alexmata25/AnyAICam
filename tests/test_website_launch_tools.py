"""Public website launch tools (tools/website_release.py, tools/website_launch_check.py).

The website names the AnyAiCam installer only through marked release blocks,
so the final release is one command; the launch check keeps superseded
installers, wrong prices and internal wording off customer pages."""
import hashlib
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import website_launch_check as launch  # noqa: E402
import website_release as release  # noqa: E402

FINAL = {"version": "1.2.3", "filename": "anyaicam-appliance-installer-1.2.3-vms-0123456789ab.tar.gz",
         "sha256": "ab" * 32, "url": "https://app.anyaicam.com/customer-login.html"}


@pytest.fixture()
def site(tmp_path):
    copy = tmp_path / "website"
    shutil.copytree(ROOT / "website", copy, ignore=shutil.ignore_patterns("*.png", "*.jpg", "*.jpeg", "*.webp", "*.mp4", "PHPMailer"))
    for image in (ROOT / "website").rglob("*"):  # links to images only need the files to exist
        if image.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".mp4"} and "PHPMailer" not in image.parts:
            target = copy / image.relative_to(ROOT / "website")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
    return copy


def read(site, name):
    return (site / name).read_text(encoding="utf-8")


# The approved Linux release (2026-10-05): VMS 1.2.3 built from 38a9a7a, a
# direct download from the website, verified against the real file.
LAUNCHED = {"version": "1.2.3", "filename": "anyaicam-appliance-installer-1.2.3-vms-38a9a7a2592d.tar.gz",
            "sha256": "e32c4898f74b5135ecfe6191299cfad826951af1b94b5bf9a1fff98be2ebfaed",
            "url": "https://anyaicam.com/downloads/anyaicam-appliance-installer-1.2.3-vms-38a9a7a2592d.tar.gz",
            "size": "35258116"}


def test_the_committed_website_passes_the_launch_check_with_the_release_published():
    failures, summary = launch.run()
    assert failures == []
    assert summary == f"release blocks published ({LAUNCHED['version']}, {LAUNCHED['filename']})"
    assert release.check()[:2] == ("published", LAUNCHED)


def test_the_published_pages_name_exactly_the_approved_linux_release():
    vms = launch.visible_text((ROOT / "website" / "vms.html").read_text(encoding="utf-8"))
    linux = launch.visible_text((ROOT / "website" / "vms-linux.html").read_text(encoding="utf-8"))
    assert "AnyAiCam 1.2.3 for Linux" in vms and LAUNCHED["sha256"] in vms and "35.3 MB" in vms
    assert f"sha256sum {LAUNCHED['filename']}" in linux and "My subscription and select Download installer" not in linux
    source = (ROOT / "website" / "vms.html").read_text(encoding="utf-8")
    assert f'href="{LAUNCHED["url"]}" download>Download for Linux' in source
    # The tooling inside the package reports its own version; the website names only the VMS release.
    assert "1.1.0" not in vms and "1.1.0" not in linux


def test_the_signed_windows_installer_stays_downloadable():
    source = (ROOT / "website" / "vms.html").read_text(encoding="utf-8")
    assert 'href="https://github.com/alexmata25/AnyAICam/releases/download/v0.1.3/AnyAiCam-VMS-Setup-0.1.3-ec5272f.exe">Download for Windows' in source
    assert "anyaicam-vms_0.1.3" not in source  # the old 0.1.3 Linux package is replaced by 1.2.3


def test_publish_names_the_final_installer_everywhere_and_pending_restores_the_page(site):
    release.apply(None, site)
    before = {name: (site / name).read_bytes() for name in release.BLOCKS}
    assert release.apply(release.validate_values(dict(FINAL)), site) == list(release.BLOCKS)
    assert launch.run(site)[0] == []
    state, values, problems = release.check(site)
    assert (state, values, problems) == ("published", FINAL, [])
    linux = launch.visible_text(read(site, "vms-linux.html"))
    assert f"sha256sum {FINAL['filename']}" in linux and f"tar -xzf {FINAL['filename']}" in linux
    assert FINAL["sha256"] in linux and "AnyAiCam 1.2.3" in linux and "Release finalizing" not in linux
    vms = launch.visible_text(read(site, "vms.html"))
    assert "AnyAiCam 1.2.3 for Linux" in vms and f"SHA-256 {FINAL['sha256']}" in vms and "Download for Linux" in vms
    release.apply(None, site)
    assert {name: (site / name).read_bytes() for name in release.BLOCKS} == before  # byte-identical, line endings kept


@pytest.mark.parametrize("change, reason", [
    ({"sha256": "5729f5ded4331b01e7cbe3b4f86afc55051fa68b04e02e6c04d8d4c6b57ce29e"}, "superseded"),
    ({"sha256": "667ac892" + "0" * 56}, "superseded"),
    ({"sha256": "0486feb7" + "0" * 56}, "superseded"),
    ({"filename": "anyaicam-appliance-installer-1.2.3-vms-9f59f11561d9.tar.gz"}, "superseded"),
    ({"version": "1.2.2", "filename": "anyaicam-appliance-installer-1.2.2-vms-0123456789ab.tar.gz"}, "superseded"),
    ({"filename": "anyaicam-appliance-installer-1.1.1-vms-0123456789ab.tar.gz"}, "does not carry version"),
    ({"sha256": "abc"}, "64 hex"),
    ({"version": "latest"}, "1.2.3"),
    ({"filename": "installer.zip"}, re.escape("anyaicam-*.tar.gz")),
    ({"url": "http://example.com/x"}, "https"),
    ({"url": "https://app.anyaicam.com/a b"}, "https"),
])
def test_publish_refuses_superseded_or_malformed_release_values(change, reason):
    with pytest.raises(release.ReleaseError, match=reason):
        release.validate_values({**FINAL, **change})


def test_publish_with_the_real_installer_checks_its_name_and_checksum(site, tmp_path):
    installer = tmp_path / FINAL["filename"]
    installer.write_bytes(b"installer bytes")
    actual = hashlib.sha256(b"installer bytes").hexdigest()
    args = ["publish", "--version", "1.2.3", "--filename", FINAL["filename"], "--installer", str(installer), "--website", str(site)]
    assert release.main(args + ["--sha256", "cd" * 32]) == 2
    assert release.check(site)[:2] == ("published", LAUNCHED)  # nothing written on refusal
    assert release.main(args + ["--sha256", actual]) == 0
    assert release.check(site)[1]["sha256"] == actual


def test_a_hand_edited_or_missing_release_block_fails_the_check(site):
    page = site / "vms.html"
    page.write_text(read(site, "vms.html").replace(">Download for Linux<", ">Download now<", 1), encoding="utf-8")
    assert any("edited by hand" in p for p in release.check(site)[2])
    page.write_text(read(site, "vms.html").replace("<!--/release:vms-card-status-->", ""), encoding="utf-8")
    assert any("vms-card-status missing" in p for p in release.check(site)[2])


def test_mixed_pending_and_published_blocks_fail_the_check(site):
    release.apply(release.validate_values(dict(FINAL)), site)
    index = site / "index.html"
    published = read(site, "index.html")
    start = published.index("<!--release:home-linux-note")
    end = published.index("<!--/release:home-linux-note-->") + len("<!--/release:home-linux-note-->")
    pending = release.render("home-linux-note", release.BLOCKS["index.html"]["home-linux-note"], None)
    index.write_text(published[:start] + pending + published[end:], encoding="utf-8")
    assert any("disagree" in p for p in release.check(site)[2])


@pytest.mark.parametrize("page, old, new, expected", [
    ("vms.html", "Ubuntu 24.04.", "Ubuntu 24.04. Version 1.2.2.", "superseded release number"),
    ("hardware.html", "$1,249.99", "$999", "old appliance price"),
    ("sales-calculator.html", "No adapter</option>", "No adapter</option><option>Test - $0.53</option>", "sandbox price"),
    ("plans.html", "Simple per-camera pricing.", "Staging per-camera pricing.", "'staging' wording"),
    ("hardware.html", "Premium appliance", "Draft appliance", "internal wording"),
    ("vms.html", "Ubuntu 24.04.", "Ubuntu 24.04. 5729f5ded433", "superseded installer checksum"),
    ("vms.html", 'href="vms-linux.html"', 'href="vms-linux-old.html"', "broken link"),
    ("vms.html", 'href="edge-appliance.html#appliances"', 'href="edge-appliance.html#gone"', "missing anchor"),
    ("plans.html", 'data-plan-price="14.99"', 'data-plan-price="13.99"', "AI Local must show $14.99"),
    ("plans.html", "<p class=\"v2-price\">$24.99 <small>", "<p class=\"v2-price\">$29.99 <small>", "Hybrid must show $24.99"),
    ("plans.html", 'data-price="9.99">Basic Local', 'data-price="8.99">Basic Local', "estimator price for Basic Local"),
    ("plans.html", 'max="64"', 'max="100"', "1-64"),
    ("plans.html", "<h2 style=\"margin-top:0\">How plans work</h2>", "<p>Up to 8 cameras $14.99/month</p><h2 style=\"margin-top:0\">How plans work</h2>", "legacy fixed-capacity"),
    ("plans.html", "customer-login.html?next=/subscription-portal", "customer-login.html", "My subscription"),
    ("plans.html", "<h2 style=\"margin-top:0\">How plans work</h2>", "<!-- price_1AbCdEfGhIjKl --><h2 style=\"margin-top:0\">How plans work</h2>", "Stripe Price ID"),
    ("contact.html", "(346) 554-4699", "(832) 510-8240", "old company phone"),
    ("vms.html", "Download for Windows</a>", "Download for Windows</a> <a href=\"https://github.com/x/y/releases/download/v0.1.3/anyaicam-vms_0.1.3.deb\">Linux</a>", "GitHub release download link"),
    ("hardware.html", "$149.99", "$129.99", "relay price"),
    ("vms.html", "Professional $1,749.99", "Professional $1,799.99", "Professional"),
    ("vms-linux.html", "Windows version coming soon.", "AnyAiCam for Windows is ready.", "Windows availability"),
    ("vms.html", "An AnyAiCam Ryzen appliance arrives", "Sign in to download. An AnyAiCam Ryzen appliance arrives", "outside the release blocks"),
    ("plans.html", "<td>$49.99 one-time</td>", "<td>$49.99</td>", "prices"),
    ("vms.html", "buy a one-time licence for your own PC", "buy a licence for your own PC", "one-time licence"),
    ("vms-linux.html", "from $49.99 for 8 cameras", "from $39.99 for 8 cameras", "licence next to an unexpected price"),
    ("plans.html", "Simple per-camera pricing.", "Sandbox per-camera pricing.", "'sandbox' wording"),
    ("analytics.html", "Coming soon as a premium analytics module.", "Future premium roadmap category.", "internal wording"),
    ("face-access.html", "Contact AnyAiCam to confirm", "AnyAiCam should publish a list. Contact AnyAiCam to confirm", "internal wording"),
    ("vms.html", "AnyAiCam 1.2.3 for Windows is coming soon.", "Download the Free AnyAiCam VMS. AnyAiCam 1.2.3 for Windows is coming soon.", "free VMS"),
    ("videoloft-partner-referral-wizard.html", "location.replace('sales-partner-login.html')",
     "location.href='referral-entry.html'", "broken link referral-entry.html"),
    ("build-your-system.html", "AI Local — $14.99 per camera / month", "AI Local — $15.99 per camera / month", "plan AI Local"),
    ("build-your-system.html", "hybrid:24.99}", "hybrid:29.99}", "monthly calculation prices"),
    ("build-your-system.html", "price:1749.99}", "price:1799.99}", "Professional appliance price"),
    ("build-your-system.html", "RELAY_PRICE=149.99", "RELAY_PRICE=129.99", "relay price"),
    ("build-your-system.html", "[16,79.99]", "[16,69.99]", "VMS licence prices"),
    ("build-your-system.html", 'min="1" max="64"', 'min="1" max="128"', "1-64"),
    ("build-your-system.html", 'id="sumOneTime"', 'id="sumOnce"', "separately"),
    ("build-your-system.html", ">Continue to Checkout</button>", ">Finish System Plan</button>", "Continue to Checkout"),
    ("build-your-system.html", "customer-register", "customer-signup", "Sign in / Create account"),
    ("build-your-system.html", "const PLAN_PRICE=", "/* price_1AbCdEfGhIjKl */const PLAN_PRICE=", "Stripe Price ID"),
    ("vms-features.html", "<h2>How camera licenses work</h2>", "<h2>Entitlements follow the camera slot</h2>", "per-slot entitlement"),
    ("vms.html", "One plan covers every licensed camera", "Features can be licensed per slot. One plan covers every licensed camera", "per-slot entitlement"),
    ("index.html", "AnyAiCam Residential VMS — Ryzen 5 · <strong>$949.99</strong>", "AnyAiCam Residential VMS — Ryzen 5 · <strong>$899.99</strong>", "must show $949.99"),
    ("plans.html", "AnyAiCam Residential VMS — Ryzen 5 $949.99", "AnyAiCam Residential VMS — Ryzen 5 $999.99", "must show $949.99"),
    ("build-your-system.html", "name:'AnyAiCam Residential VMS — Ryzen 5',price:949.99}", "name:'AnyAiCam Residential VMS — Ryzen 5',price:899.99}", "Ryzen 5 price must be $949.99"),
    ("vms.html", "github.com", "github.com", None),
])
def test_the_launch_check_catches_each_kind_of_launch_defect(site, page, old, new, expected):
    text = read(site, page)
    if expected is None:  # control row: an untouched copy passes
        assert launch.run(site)[0] == []
        return
    assert old in text, old
    (site / page).write_text(text.replace(old, new, 1), encoding="utf-8")
    failures = launch.run(site)[0]
    assert any(expected in failure for failure in failures), failures


def test_the_homepage_shows_the_real_vms_live_view_not_a_mock_panel():
    index = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
    assert 'src="app-screens/vms-live-camera.webp"' in index
    assert (ROOT / "website" / "app-screens" / "vms-live-camera.webp").is_file()
    for mock in ("Your cameras. Your local system.", "Local system online", "Search with licensed AI"):
        assert mock not in index


def test_the_aac_feature_tour_pairs_each_feature_with_its_real_screenshot():
    import re
    page = (ROOT / "website" / "aac-features.html").read_text(encoding="utf-8")
    rows = re.findall(r'<div class="feature-row[^"]*"><div class="feature-copy">.*?<h3>(.*?)</h3>.*?<img src="app-screens/([^"?]+)[^"]*"', page)
    assert rows == [("Live view", "vms-live-camera.webp"), ("Playback", "vms-playback-timeline.webp"), ("Events", "vms-events.webp"),
                    ("Smart Alerts", "vms-smart-alerts.webp"), ("License Plate Recognition", "vms-license-plates.webp"),
                    ("People Counting", "vms-people-counting.webp"), ("AACO — AnyAiCam Operator", "aaco-person-events.webp"),
                    ("AAC VC — Visitor Call", "visitor-call-email-phone.webp"), ("AAC Secure Edge", "secure-edge-arm-disarm.webp")]
    for _, image in rows:
        assert (ROOT / "website" / "app-screens" / image).is_file()
    assert page.count("vms-smart-alerts.webp") == 2  # one feature row: the image and its full-size link


def test_the_homepage_sells_anyaicam_and_videoloft_has_its_own_page():
    index = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
    videoloft = (ROOT / "website" / "videoloft.html").read_text(encoding="utf-8")
    for section in ('id="videoloft-demo"', 'id="software-bridge"', 'id="buy-cloud-storage"', 'id="adapter-product"',
                    'id="ai-video-showcase"', 'id="service-scope"', "Videoloft: IP Camera CCTV App."):
        assert section not in index and section in videoloft
    assert '<a href="videoloft.html">Videoloft Cloud</a>' in index
    assert 'id="anyaicam-features"' in index and index.count('class="aic-feature-card"') == 6
    assert 'href="build-your-system.html">Build Your System</a>' in index and 'href="aac-features.html"' in index
    # Old "Cloud Account Setup" links (index.html#services) still land on the Videoloft content.
    assert 'id="services"' in index and "location.replace('videoloft.html#services')" in index
    assert 'id="services"' in videoloft
    # The page keeps the Videoloft side's own navigation.
    assert '<a href="how-it-works.html">How It Works</a>' in videoloft and '<a href="cloud-setup-wizard.html">Get Started</a>' in videoloft


def test_camera_readiness_is_an_anyaicam_pre_purchase_check_and_keeps_the_videoloft_check():
    page = (ROOT / "website" / "camera-compatibility-check.html").read_text(encoding="utf-8")
    assert "Already have cameras? Check whether your current system is ready for AnyAiCam." in page
    assert 'id="anyaicam-readiness"' in page and 'id="videoloft-check"' in page
    # No unbenchmarked appliance-capacity claims.
    assert not re.search(r"64 cameras with (?:all|every)|all analytics|every analytic", page, re.I)
    # The existing interactive check and its Videoloft order path are unchanged.
    assert '<script src="/camera-compatibility-check.js"' in page and 'href="/cloud-setup-wizard.html" id="continueOrderButton"' in page
    index = (ROOT / "website" / "index.html").read_text(encoding="utf-8")
    assert "Check whether your current system is ready for AnyAiCam." in index and 'href="/camera-compatibility-check.html"' in index
