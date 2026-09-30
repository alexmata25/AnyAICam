"""Public website (website/, the version-controlled copy of anyaicam.com,
2026-09-29): guards against the launch problems found in the live site.

- no customer-visible staging / test-mode / test-pricing wording, in pages
  or in scripts that inject markup (the live footer said "Staging only:
  Stripe Test Mode, $1/year test pricing");
- every local link and asset a page references exists in the copy (a
  footer linked the missing cart.html);
- every inline script and .js file is valid JavaScript (a multi-line
  string broke the whole support diagnostic tool);
- no expired third-party widget snippets (LiveChat) and the website's
  customer login never points at the portal's /login recovery page.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parents[2] / "website"
pytestmark = pytest.mark.skipif(not SITE.is_dir(), reason="website/ copy not present")

PAGES = sorted(SITE.glob("*.html"))
SCRIPTS = sorted(SITE.glob("*.js"))
STAGING_WORDS = re.compile(r"staging only|test mode|test pricing|test cart|\$1/year|mock provisioning|staging-footer", re.I)
LOCAL_REF = re.compile(r"""(?:href|src)\s*=\s*["']([^"'#?]+?\.(?:html|css|js|png|jpe?g|webp|svg|ico|webmanifest))(?:[?#][^"']*)?["']""", re.I)
# Pages that are only reached from JavaScript still count as references.
KNOWN_MISSING_OWNER_DECISION = {"referral-entry.html"}  # see website/README.md follow-ups


def _text(path):
    return path.read_bytes().decode("utf-8", "replace")


@pytest.mark.parametrize("path", PAGES + SCRIPTS, ids=lambda p: p.name)
def test_no_staging_or_test_wording(path):
    assert not STAGING_WORDS.search(_text(path)), f"{path.name} contains staging/test wording"


@pytest.mark.parametrize("path", PAGES + SCRIPTS, ids=lambda p: p.name)
def test_local_references_exist(path):
    missing = []
    for ref in LOCAL_REF.findall(_text(path)):
        if ref.startswith(("http:", "https:", "//", "mailto:", "tel:", "data:")) or "${" in ref or "/cdn-cgi/" in ref:
            continue
        name = ref.lstrip("/")
        if name in KNOWN_MISSING_OWNER_DECISION:
            continue
        if not (SITE / name).exists():
            missing.append(ref)
    assert not missing, f"{path.name} references missing files: {missing}"


NODE = shutil.which("node")


def _node_check(code):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(code)
        name = handle.name
    try:
        return subprocess.run([NODE, "--check", name], capture_output=True, text=True, timeout=60)
    finally:
        Path(name).unlink(missing_ok=True)


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("path", PAGES, ids=lambda p: p.name)
def test_inline_scripts_are_valid_javascript(path):
    blocks = re.findall(r"<script\b(?![^>]*\bsrc=)(?![^>]*type=[\"']application/ld\+json)[^>]*>(.*?)</script>", _text(path), re.S | re.I)
    for index, code in enumerate(block for block in blocks if block.strip()):
        done = _node_check(code)
        assert done.returncode == 0, f"{path.name} inline script #{index}: {done.stderr.strip()[:300]}"


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_script_files_are_valid_javascript(path):
    done = _node_check(_text(path))
    assert done.returncode == 0, f"{path.name}: {done.stderr.strip()[:300]}"


def test_no_expired_livechat_widget():
    assert not [p.name for p in PAGES if "livechatinc" in _text(p).lower()]


def test_website_customer_login_opens_the_customer_sign_in():
    html = _text(SITE / "customer-login.html")
    assert "app.anyaicam.com/login" not in html and "https://app.anyaicam.com/customer-login.html" in html


def test_config_php_is_never_committed():
    assert "config.php" in _text(SITE / ".gitignore") and not (SITE / "config.php").exists()


# ---------------------------------------------------------------- AAC product pages (2026-09-29)

PRODUCT_PAGES = ["aac-features.html", "aaco.html", "visitor-call.html", "secure-edge.html"]


@pytest.mark.parametrize("name", PRODUCT_PAGES)
def test_product_page_exists_with_title_description_and_canonical(name):
    html = _text(SITE / name)
    assert re.search(r"<title>[^<]{10,}</title>", html)
    assert re.search(r'<meta name="description" content="[^"]{40,}"', html)
    assert f'<link rel="canonical" href="https://anyaicam.com/{name}"' in html
    assert 'href="aac-features.html" aria-current="page"' in html  # nav shows where you are


@pytest.mark.parametrize("name", PRODUCT_PAGES)
def test_product_page_screenshots_exist_are_described_and_sized(name):
    html = _text(SITE / name)
    images = re.findall(r'<img src="(app-screens/[^"]+)" alt="([^"]*)" width="(\d+)" height="(\d+)"', html)
    assert images, "each product page shows at least one real app screenshot"
    for src, alt, width, height in images:
        assert (SITE / src).exists(), src
        assert len(alt) > 20 and int(width) > 0 and int(height) > 0


def test_product_pages_are_reachable_from_nav_footer_vms_page_and_sitemap():
    nav_pages = [p for p in PAGES if 'class="stage-nav"' in _text(p)]
    assert nav_pages and all('href="aac-features.html"' in _text(p) for p in nav_pages)
    footers = [p for p in PAGES if "<h3>AI Access" in _text(p)]
    assert footers and all(all(f'href="{x}"' in _text(p) for x in ("aaco.html", "visitor-call.html", "secure-edge.html")) for p in footers)
    vms = _text(SITE / "vms.html")
    assert all(f'href="{x}"' in vms for x in ("aaco.html", "visitor-call.html", "secure-edge.html", "aac-features.html"))
    sitemap = _text(SITE / "sitemap.xml")
    assert all(f"https://anyaicam.com/{x}" in sitemap for x in PRODUCT_PAGES)


@pytest.mark.parametrize("name", PRODUCT_PAGES)
def test_product_pages_make_no_pricing_certification_or_monitoring_claims(name):
    text = re.sub(r"<[^>]+>", " ", _text(SITE / name))
    assert not re.search(r"\$\s?\d", text), "no prices on product pages"
    assert not re.search(r"(?i)\b(certified|certification|UL[- ]listed|24/7 monitor|guarantee|warranty|police dispatch)\b", text)
    assert "Available with AnyAiCam VMS" in text or "In final testing" in text or "Coming soon" in text


def test_secure_edge_says_it_is_not_a_monitoring_service_and_never_calls_911():
    text = _text(SITE / "secure-edge.html")
    assert "does not call 911" in text and "not a professionally monitored alarm service" in text


def test_screenshots_contain_no_staging_markers_in_file_names():
    assert not [p.name for p in (SITE / "app-screens").iterdir() if re.search(r"(?i)stag|test|debug", p.name)]


def test_vms_footer_pages_use_the_styled_site_footer():
    """plans.html and analytics.html used an unstyled script-built footer whose
    stylesheet never existed on the server: a 1280px logo made the page scroll
    sideways (906px on a phone). Every page now uses the site's unified footer."""
    for path in PAGES:
        text = path.read_text(encoding="utf-8", errors="replace")
        assert 'class="stage-footer"' not in text, path.name
        if "aic-unified-footer" in text:
            assert ".aic-unified-footer" in text or "vms-product.css" in text, path.name


def test_vms_page_shows_live_and_playback_screenshots():
    text = (SITE / "vms.html").read_text(encoding="utf-8", errors="replace")
    for name in ("vms-live-camera.webp", "vms-playback-timeline.webp"):
        tag = re.search(r'<img src="app-screens/' + re.escape(name) + r'"[^>]*>', text)
        assert tag, name
        assert re.search(r'alt="[^"]{20,}"', tag.group(0)) and 'width="1600"' in tag.group(0), name
        assert (SITE / "app-screens" / name).stat().st_size < 400_000, name
    assert ".app-shot-pair" in text
