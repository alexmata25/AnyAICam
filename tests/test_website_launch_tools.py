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


def test_the_committed_website_passes_the_launch_check_with_the_release_pending():
    failures, summary = launch.run()
    assert failures == []
    assert summary == "release blocks pending"


def test_pending_pages_name_no_installer_version_or_checksum():
    for name in launch.RELEASE_PAGES:
        text = launch.visible_text((ROOT / "website" / name).read_text(encoding="utf-8"))
        assert not re.search(r"\b\d+\.\d+\.\d+\b", text) and not re.search(r"\b[0-9a-f]{64}\b", text)
        assert ".tar.gz" not in text.replace("anyaicam-appliance-installer-*.tar.gz", "")
        assert "Sign in to download" not in text and "Available now" not in text


def test_publish_names_the_final_installer_everywhere_and_pending_restores_the_page(site):
    before = {name: (site / name).read_bytes() for name in release.BLOCKS}
    assert release.apply(release.validate_values(dict(FINAL)), site) == list(release.BLOCKS)
    assert launch.run(site)[0] == []
    state, values, problems = release.check(site)
    assert (state, values, problems) == ("published", FINAL, [])
    linux = launch.visible_text(read(site, "vms-linux.html"))
    assert f"sha256sum {FINAL['filename']}" in linux and f"tar -xzf {FINAL['filename']}" in linux
    assert FINAL["sha256"] in linux and "AnyAiCam 1.2.3" in linux and "Release finalizing" not in linux
    vms = launch.visible_text(read(site, "vms.html"))
    assert "AnyAiCam 1.2.3 for Linux" in vms and f"SHA-256 {FINAL['sha256']}" in vms and "Sign in to download" in vms
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
    assert release.check(site)[0] == "pending"  # nothing written on refusal
    assert release.main(args + ["--sha256", actual]) == 0
    assert release.check(site)[1]["sha256"] == actual


def test_a_hand_edited_or_missing_release_block_fails_the_check(site):
    page = site / "vms.html"
    page.write_text(read(site, "vms.html").replace("Release finalizing", "Available now"), encoding="utf-8")
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
    ("index.html", "$1,249.99", "$999", "old appliance price"),
    ("sales-calculator.html", "No adapter</option>", "No adapter</option><option>Test - $0.53</option>", "sandbox price"),
    ("plans.html", "Plans are priced", "Staging plans are priced", "'staging' wording"),
    ("hardware.html", "Premium appliance", "Draft appliance", "internal wording"),
    ("vms.html", "Ubuntu 24.04.", "Ubuntu 24.04. 5729f5ded433", "superseded installer checksum"),
    ("vms.html", 'href="vms-linux.html"', 'href="vms-linux-old.html"', "broken link"),
    ("vms.html", 'href="edge-appliance.html#appliances"', 'href="edge-appliance.html#gone"', "missing anchor"),
    ("plans.html", "$69.99/month</td></tr>\n</tbody>", "$59.99/month</td></tr>\n</tbody>", "prices"),
    ("hardware.html", "$149.99", "$129.99", "relay price"),
    ("vms.html", "Professional $1,749.99", "Professional $1,799.99", "Professional"),
    ("vms.html", "A Windows version is coming soon.", "AnyAiCam for Windows is ready.", "Windows availability"),
    ("vms.html", "An AnyAiCam Ryzen appliance arrives", "Sign in to download. An AnyAiCam Ryzen appliance arrives", "outside the release blocks"),
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
