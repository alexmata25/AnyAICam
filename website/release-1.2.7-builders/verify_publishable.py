"""Refuses to call the prepared 1.2.7 pages publishable while any value is still pending, or if the base pages
no longer match the server copy they were built from. Exit 0 = publishable."""
import sys
from pathlib import Path

HERE = Path(__file__).parent
problems = []
for page in ("vms.html", "vms-linux.html"):
    html = (HERE / "prepared" / "public_html" / page).read_text(encoding="utf-8")
    if "PENDING-SHA256" in html or "PENDING-SIZE" in html:
        problems.append(f"{page}: installer SHA-256/size still pending")
    if "PENDING-DOWNLOAD-HOST" in html:
        problems.append(f"{page}: installer host not set (S3/CloudFront setup awaiting owner approval)")
    if "https://anyaicam.com/downloads/linux/1.2.7/" not in html:
        problems.append(f"{page}: the installer link is not on the approved Option A path /downloads/linux/1.2.7/")
    if "state=pending" in html or "Release pending" in html:
        problems.append(f"{page}: marked as a pending release (not published until the approved host serves the installer)")
    if "state=published" not in html:
        problems.append(f"{page}: release markers are not in the published state")
    if "about 900 MB" in html:
        problems.append(f"{page}: size is an estimate, not the verified byte count")
# Pricing (owner approvals 2026-10-17): Hybrid $19.99 per camera/month; licenses $24.99/$37.49/$62.49/$99.99;
# free phone support on all three plans (build_127_pricing.py). An old price or missing support line blocks publication.
import build_127_pricing as pricing
_pages = {name: (HERE / "prepared" / "public_html" / name).read_text(encoding="utf-8") for name in pricing.PAGES}
for name, html in _pages.items():
    for old in ("hybrid:24.99", 'data-price="24.99"', 'data-plan-price="24.99"', "Hybrid — $24.99", '"aic-compare-price">$24.99',
                "$49.99", "$79.99", "$129.99", "$199.99", "49.99]", "79.99]", "129.99]", "199.99]"):
        if old in html:
            problems.append(f"{name}: still shows an old price ({old})")
for name, expected in (("plans.html", 3), ("build-your-system.html", 3), ("index.html", 4)):
    if _pages[name].count(pricing.SUPPORT) != expected:
        problems.append(f"{name}: free phone support shown {_pages[name].count(pricing.SUPPORT)} times, expected {expected}")
# Plan comparison wording (owner request 2026-10-17): remote viewing on all three plans, Cloud Video Backup on Hybrid only.
for name, html in _pages.items():
    for jargon in ("Remote viewing where supported", "remote viewing where supported", "Remote cloud services"):
        if jargon in html:
            problems.append(f"{name}: still uses the old wording ({jargon})")
_home = _pages["index.html"]
for label, marks in (("Remote viewing", 3), ("Cloud Video Backup", 1)):
    _at = _home.find(f'<th scope="row">{label}<span class="aic-compare-desc">')
    if _at < 0 or _home[_at:_home.index("</tr>", _at)].count('class="aic-yes"') != marks:
        problems.append(f"index.html: the comparison row '{label}' must be included in exactly {marks} plan(s)")
# Retention wording (owner decisions 2026-10-17): "up to", the storage explanation, one Cloud Video Backup row.
for name in ("plans.html", "build-your-system.html", "index.html"):
    if pricing.STORAGE_NOTE not in _pages[name]:
        problems.append(f"{name}: missing the note that local retention depends on available storage")
for row in ("Hybrid cloud services", "Cloud event protection"):
    if f'<th scope="row">{row}</th>' in _home:
        problems.append(f"index.html: the overlapping Hybrid row '{row}' is still in the comparison")
for name in ("plans.html", "build-your-system.html"):
    for unqualified in ('"v2-retention">2-day', '"v2-retention">7-day', "<small>2-day local recording", "<small>7-day local recording"):
        if unqualified in _pages[name]:
            problems.append(f"{name}: retention is not advertised as 'up to' ({unqualified})")
# The 14-day cloud event-clip claim is published only once the S3 expiration rule was verified (read-only).
_s3 = HERE / "S3-LIFECYCLE-VERIFIED.md"
if not _s3.exists() or "RESULT: 14-day expiration confirmed" not in _s3.read_text(encoding="utf-8"):
    problems.append("the 14-day cloud event-clip retention is not yet verified against the S3 lifecycle rule "
                    "(record the read-only check in S3-LIFECYCLE-VERIFIED.md)")
if pricing.KEPT not in _home:
    problems.append("index.html: the comparison table does not show how long recordings are kept")
if 'data-plan="hybrid" data-plan-price="19.99"' not in _pages["plans.html"] or "hybrid:19.99" not in _pages["build-your-system.html"]:
    problems.append("Hybrid is not shown at $19.99 per camera/month")
if not list((HERE / "prepared" / "public_html").glob("*.tar.gz.sha256")):
    problems.append("no published .sha256 file prepared (needs the verified SHA-256)")
print("\n".join(problems) or "values complete (publication still needs owner approval, the hosting decision and a server re-check)")
sys.exit(1 if problems else 0)
