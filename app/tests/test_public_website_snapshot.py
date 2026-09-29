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
