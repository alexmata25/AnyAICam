"""Launch checks for the public website source (website/). Read-only.

    python tools/website_launch_check.py

Fails on: superseded releases, installers and checksums; old, sandbox or
wrong AnyAiCam prices; a VMS licence not shown as one-time; "staging",
"sandbox", draft, candidate or internal roadmap wording; "free VMS" claims;
Windows VMS availability claims; download wording while the release is pending; release
blocks that disagree; broken local links and anchors.
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import website_release  # noqa: E402

WEBSITE = website_release.WEBSITE

APPLIANCES = {"Starter": "$1,249.99", "Professional": "$1,749.99", "Enterprise": "$2,249.99"}
RELAY = "$149.99"
PLAN_TABLES = {  # plans.html, in page order
    "AnyAiCam VMS licence": ["$49.99 one-time", "$79.99 one-time", "$129.99 one-time", "$199.99 one-time"],
}
# Billing-v2 (2026-10-05, locked): per-camera monthly plans, 1-64 cameras per account.
V2_PLANS = {"basic_local": ("Basic Local", "9.99"), "ai_local": ("AI Local", "14.99"), "hybrid": ("Hybrid", "24.99")}
V2_MIN_CAMERAS, V2_MAX_CAMERAS = 1, 64
# The old fixed-capacity subscriptions (8/16/32/64 cameras) are legacy-only: never offered on the website.
LEGACY_TIER_PRICE = re.compile(r"(?<!× )\$(?:14|24|39|69|99)\.99/month")  # "N × $14.99/month" is a per-camera estimate
# The one Windows installer still offered (signed 0.1.3); every other 0.1.3 or GitHub release reference is stale.
WINDOWS_ALLOWED = ("https://github.com/alexmata25/AnyAICam/releases/download/v0.1.3/AnyAiCam-VMS-Setup-0.1.3-ec5272f.exe",
                   "AnyAiCam VMS for Windows 0.1.3", "Windows 0.1.3 · Signed installer")
COMPANY_PHONE = "(346) 554-4699"
OLD_PHONE = re.compile(r"\(832\) 510-8240|\+?1?8325108240")
TIERS = ["Up to 8 cameras", "Up to 16 cameras", "Up to 32 cameras", "Up to 64 cameras"]
# Pages that sell AnyAiCam appliances (Videoloft pages have their own catalogue).
APPLIANCE_PAGES = {name: list(APPLIANCES) for name in ("index.html", "hardware.html", "build-your-system.html", "edge-appliance.html", "vms.html")}
APPLIANCE_PAGES["face-access.html"] = ["Enterprise"]  # sells only the Face Access appliance
RELAY_PAGES = ["hardware.html", "face-access.html", "build-your-system.html"]
RELEASE_PAGES = ["vms.html", "vms-linux.html", "index.html"]
LICENCE_PAGES = ["vms.html", "vms-linux.html"]  # tell a customer how to license their own PC

STALE = [
    (re.compile(r"(?<![\w.])(?:v)?(1\.2\.2|0\.1\.3)(?!\.?\w)"), "superseded release number"),
    (re.compile(r"github\.com/[^\s\"'<>]*/releases", re.I), "GitHub release download link"),
    (re.compile(r"\$(?:999|1,499|2,499)(?:\.00)?(?![\d,])"), "old appliance price"),
    (re.compile(r"\$0\.5[0-3](?!\d)"), "sandbox price"),
    (re.compile(r"\bstag(?:ing|ed)\b", re.I), "'staging' wording"),
    (re.compile(r"\bsandbox\b", re.I), "'sandbox' wording"),
] + [(re.compile(re.escape(p)), "superseded installer checksum") for p in website_release.SUPERSEDED_SHA256_PREFIXES] \
  + [(re.compile(re.escape(p)), "superseded installer build") for p in website_release.SUPERSEDED_FILENAME_PARTS if not p.startswith("-")]
DRAFT = re.compile(r"\b(draft|candidate|subject to (?:final )?(?:AnyAiCam )?validation|before launch|lorem ipsum|TBD|TODO"
                   r"|roadmap|is being structured|product direction|before (?:professional )?production (?:launch|sales)"
                   r"|server-authori[sz]ed|authoritative backend|should publish)\b", re.I)
# The AnyAiCam VMS is sold (with an appliance or as a one-time licence); the free product is the Videoloft Software Bridge.
FREE_VMS = re.compile(r"\bfree\s+(?:AnyAiCam\s+)?VMS\b|\bVMS\s+(?:software\s+)?(?:is\s+)?free\b|\bfree\s+AnyAiCam\s+(?:software|download)", re.I)
SERVED = ("*.html", "*.php", "*.js", "*.css", "*.txt", "*.md", "*.json", "*.webmanifest")
# Repository notes about the mirror itself, never uploaded (see website/README.md).
REPO_ONLY = {"README.md", "SOURCE_MANIFEST.json", "SERVER_CLEANUP.json"}


def visible_text(source: str) -> str:
    source = re.sub(r"<(script|style)\b.*?</\1>", " ", source, flags=re.S | re.I)
    source = re.sub(r"<!--.*?-->", " ", source, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", source)))


def without_release_blocks(source: str) -> str:
    return website_release.MARKER.sub(" ", source)


def served_files(website: Path):
    seen = set()
    for pattern in SERVED:
        for path in website.rglob(pattern):
            if "PHPMailer" in path.parts or path.name in REPO_ONLY or path in seen:
                continue
            seen.add(path)
            yield path


def check_stale(website: Path, fail):
    for path in sorted(served_files(website)):
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(website).as_posix()
        for allowed in WINDOWS_ALLOWED:  # same length, so line numbers stay right
            text = text.replace(allowed, "x" * len(allowed))
        if path.suffix == ".html":
            for match in OLD_PHONE.finditer(text):
                fail(f"{rel}:{text.count(chr(10), 0, match.start()) + 1}: old company phone number {match.group(0)!r} (use {COMPANY_PHONE})")
            for match in LEGACY_TIER_PRICE.finditer(visible_text(text)):
                fail(f"{rel}: legacy fixed-capacity subscription price {match.group(0)!r} offered")
        for pattern, label in STALE:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                fail(f"{rel}:{line}: {label}: {match.group(0)!r}")
        if path.suffix == ".html":
            for match in DRAFT.finditer(visible_text(text)):
                fail(f"{rel}: internal wording visible to customers: {match.group(0)!r}")
            for match in FREE_VMS.finditer(visible_text(text)):
                fail(f"{rel}: 'free VMS' claim: {match.group(0)!r}")
            for match in re.finditer(r'\balt="([^"]*)"', text):
                if DRAFT.search(match.group(1)):
                    fail(f"{rel}: internal wording in an image description: {match.group(1)!r}")


def check_prices(website: Path, fail):
    for name, tiers in APPLIANCE_PAGES.items():
        text = visible_text((website / name).read_text(encoding="utf-8"))
        for tier in tiers:
            price = APPLIANCES[tier]
            if price not in text:
                fail(f"{name}: AnyAiCam {tier} price {price} missing")
            for match in re.finditer(rf"\b{tier}\b[^$]{{0,70}}?\$([\d,]+\.\d\d)", text):
                if f"${match.group(1)}" not in APPLIANCES.values() and f"${match.group(1)}" != RELAY:
                    fail(f"{name}: {tier} next to an unexpected price ${match.group(1)}")
    for name in RELAY_PAGES:
        text = visible_text((website / name).read_text(encoding="utf-8"))
        if RELAY not in text:
            fail(f"{name}: Numato relay price {RELAY} missing")
        for match in re.finditer(r"(?:Numato|relay)[^$]{0,60}?\$([\d,]+\.\d\d)", text, re.I):
            if f"${match.group(1)}" != RELAY:
                fail(f"{name}: relay next to an unexpected price ${match.group(1)}")
    plans = (website / "plans.html").read_text(encoding="utf-8")
    for title, prices in PLAN_TABLES.items():
        card = re.search(rf"<h3>{re.escape(title)}</h3>.*?</table>", plans, re.S)
        if not card:
            fail(f"plans.html: '{title}' price table missing")
            continue
        rows = re.findall(r"<tr><th>([^<]+)</th><td>([^<]+)</td></tr>", card.group(0))
        if rows != list(zip(TIERS, prices)):
            fail(f"plans.html: '{title}' prices {rows} != authoritative {list(zip(TIERS, prices))}")
    check_v2_plans(plans, fail)
    for name in LICENCE_PAGES:
        text = visible_text((website / name).read_text(encoding="utf-8"))
        if "one-time licence" not in text:
            fail(f"{name}: the VMS licence for your own PC is not described as a one-time licence")
        for match in re.finditer(r"licen[cs]e\b[^$]{0,80}?\$([\d,]+\.\d\d)", text, re.I):
            if f"${match.group(1)} one-time" not in PLAN_TABLES["AnyAiCam VMS licence"]:
                fail(f"{name}: licence next to an unexpected price ${match.group(1)}")


def check_v2_plans(plans: str, fail):
    """plans.html shows each Billing-v2 plan at its locked per-camera price,
    the estimator uses the same prices and the 1-64 range, and the plan is
    chosen in My subscription (the website never names a Stripe Price)."""
    for key, (name, price) in V2_PLANS.items():
        card = re.search(rf'<article\b[^>]*data-plan="{key}"[^>]*>.*?</article>', plans, re.S)
        if not card:
            fail(f"plans.html: Billing-v2 plan card {name} missing")
            continue
        attr = re.search(r'data-plan-price="([^"]+)"', card.group(0))
        text = visible_text(card.group(0))
        if not attr or attr.group(1) != price or f"<h3>{name}</h3>" not in card.group(0) or f"${price} per camera / month" not in text:
            fail(f"plans.html: {name} must show ${price} per camera / month")
        option = re.search(rf'<option value="{key}" data-price="([^"]+)"', plans)
        if not option or option.group(1) != price:
            fail(f"plans.html: estimator price for {name} must be {price}")
    quantity = re.search(r'<input id="v2-cameras"[^>]*>', plans)
    if not quantity or f'min="{V2_MIN_CAMERAS}"' not in quantity.group(0) or f'max="{V2_MAX_CAMERAS}"' not in quantity.group(0):
        fail(f"plans.html: camera quantity must allow {V2_MIN_CAMERAS}-{V2_MAX_CAMERAS}")
    if "subscription-portal" not in plans or "customer-register" not in plans:
        fail("plans.html: plans must lead to Create account / Sign in -> My subscription")
    if re.search(r"price_[A-Za-z0-9]{8,}", plans):
        fail("plans.html: a Stripe Price ID appears in the page")


def check_release(website: Path, fail):
    state, values, problems = website_release.check(website)
    for problem in problems:
        fail(problem)
    for name in RELEASE_PAGES:
        source = (website / name).read_text(encoding="utf-8")
        outside = visible_text(without_release_blocks(source))
        for phrase in ("Sign in to download", "Available now", "download the installer", "SHA-256 "):
            if phrase in outside and not (name == "vms-linux.html" and phrase == "SHA-256 "):
                fail(f"{name}: release wording outside the release blocks: {phrase!r}")
        linux_only = re.sub(r'<article class="card" id="windows-download">.*?</article>', " ", source, flags=re.S)
        if state == "pending" and re.search(r"\b\d+\.\d+\.\d+\b", visible_text(linux_only)):
            fail(f"{name}: names a release version while the release is pending")
    for path in website.glob("*.html"):
        source = re.sub(r"<(script|style)\b.*?</\1>", " ", path.read_text(encoding="utf-8"), flags=re.S | re.I)
        # The signed Windows 0.1.3 installer is genuinely available (2026-10-05); only its card may say so.
        source = re.sub(r'<article class="card" id="windows-download">.*?</article>', " ", source, flags=re.S)
        # Each paragraph, list item or cell on its own: a nearby "Coming soon" label must not excuse a claim.
        blocks = [visible_text(m.group(2)) for m in re.finditer(r"<(p|li|td|small)\b[^>]*>(.*?)</\1>", source, re.S | re.I)]
        for sentence in (s for block in blocks for s in re.split(r"(?<=[.!?])\s", block)):
            if re.search(r"\bAnyAiCam\b[^.]{0,40}\bWindows\b|\bWindows\b[^.]{0,30}\bAnyAiCam\b", sentence) \
                    and not re.search(r"coming soon|Videoloft|Bridge", sentence, re.I):
                fail(f"{path.name}: Windows availability claim: {sentence.strip()[:120]!r}")
    return state, values


def check_links(website: Path, fail):
    ids = {}

    def anchors(path: Path):
        if path not in ids:
            text = path.read_text(encoding="utf-8", errors="replace")
            ids[path] = set(re.findall(r'\b(?:id|name)="([^"]+)"', text))
        return ids[path]

    for page in sorted(website.rglob("*.html")):
        text = page.read_text(encoding="utf-8", errors="replace")
        # Attributes, and script redirects such as location.href='x.html' or location.replace('x.html').
        for match in re.finditer(r'\b(?:href|src)="([^"]*)"|\blocation(?:\.href\s*=|\.replace\()\s*[\'"]([^\'"+]+\.html[^\'"+]*)[\'"]', text):
            url = html.unescape(match.group(1) if match.group(1) is not None else match.group(2)).strip()
            if not url or url.startswith(("http:", "https:", "mailto:", "tel:", "sms:", "javascript:", "data:", "//", "{", "$")):
                continue
            parts = urlsplit(url)
            target = page if not parts.path else (website / unquote(parts.path).lstrip("/") if parts.path.startswith("/")
                                                  else page.parent / unquote(parts.path))
            target = target.resolve()
            if target.is_dir():
                target = target / "index.html"
            rel = page.relative_to(website).as_posix()
            if not target.exists():
                fail(f"{rel}: broken link {url}")
            elif parts.fragment and target.suffix == ".html" and parts.fragment not in anchors(target):
                fail(f"{rel}: missing anchor {url}")


def run(website: Path = WEBSITE) -> tuple[list[str], str]:
    failures: list[str] = []
    check_stale(website, failures.append)
    check_prices(website, failures.append)
    state, values = check_release(website, failures.append)
    check_links(website, failures.append)
    summary = f"release blocks {state}" + (f" ({values['version']}, {values['filename']})" if values else "")
    return failures, summary


def main() -> int:
    failures, summary = run()
    for failure in failures:
        print("FAIL", failure)
    pages = len(list(WEBSITE.rglob("*.html")))
    print(f"{pages} pages checked; {summary}; {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
