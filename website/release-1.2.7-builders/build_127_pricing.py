"""1.2.7 website pricing (owner approvals 2026-10-17), applied to prepared/public_html after the other builders.

* Hybrid $19.99 per camera / month (was $24.99). Basic Local $9.99 and AI Local $14.99 are unchanged.
* One-time VMS software licenses: 8 / 16 / 32 / 64 cameras $24.99 / $37.49 / $62.49 / $99.99
  (were $49.99 / $79.99 / $129.99 / $199.99).
* "Free remote technical support by phone, Monday through Sunday, during business hours" on all three plans.
* Plan comparison in everyday language (owner request 2026-10-17): remote viewing is included with ALL three plans;
  "Remote cloud services" becomes "Cloud Video Backup" (Hybrid only: event video clips, never continuous
  recording); how long recordings are kept is shown; the AI Local description is clarified.

Every replacement names the exact old text and its exact count, so nothing that merely contains "24.99" (the
Advanced Analytics add-on, hardware, the new 8-camera license) is touched by accident; a page that does not match
stops the build. Hardware, relay and appliance prices are never edited. Re-running is safe (already applied = skip).
--dry-run transforms in memory and reports, writing nothing.
"""
import sys
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "prepared" / "public_html"
SUPPORT = "Free remote technical support by phone, Monday through Sunday, during business hours"
LICENSES_OLD = ("$49.99", "$79.99", "$129.99", "$199.99")
LICENSES_NEW = ("$24.99", "$37.49", "$62.49", "$99.99")
REMOTE = "Watch your cameras from anywhere using your phone or computer."
REMOTE_DETAIL = REMOTE + " It works over the internet, so your AnyAiCam system needs an internet connection."
BACKUP = "Keep an extra copy of your camera's event video clips safely stored online for 14 days."
# Owner decisions 2026-10-17: advertise local retention as "up to", and say why.
STORAGE_NOTE = "How long local recordings are kept depends on the storage available on your AnyAiCam system."
RETENTION = {"basic": "Up to 2 days of local recording", "ai": "Up to 7 days of local recording",
             "hybrid": "Up to 7 days of local recording + 14 days of cloud event-clip backup"}
AI_LOCAL = "Smart AI camera features, local recording, and remote viewing — without paying for cloud video storage."
KEPT = "How long recordings are kept"
DESC_CSS = (".aic-compare-desc{display:block;font-weight:400;font-size:.86em;line-height:1.35;color:#52627a;margin-top:.15rem}"
            ".aic-compare-kept{font-weight:600}\n")
MOBILE_CSS = "@media (max-width:760px){\n .aic-compare-table col.aic-col-feature{width:34%}"


def _desc(label, text):
    """A comparison-table row heading: a short label, with the everyday explanation on its own smaller line."""
    return f'<th scope="row">{label}<span class="aic-compare-desc">{text}</span></th>'


class Mismatch(Exception):
    pass


def replace(html, old, new, count=1, *, page):
    found = html.count(old)
    if found == count:
        return html.replace(old, new)
    if found == 0 and html.count(new) >= count:
        return html  # already applied
    raise Mismatch(f"{page}: expected {count} x {old[:80]!r}, found {found}")


def insert_in_card(html, card_marker, before, addition, *, page):
    """Insert `addition` right before the first `before` that follows `card_marker` (one card)."""
    if html.count(card_marker) != 1:
        raise Mismatch(f"{page}: card marker {card_marker[:60]!r} found {html.count(card_marker)} times")
    start = html.index(card_marker)
    at = html.index(before, start)
    if addition in html[start:at + len(before) + len(addition) + 200]:
        return html  # already applied
    return html[:at] + addition + html[at:]


def plans(html, page="plans.html"):
    html = replace(html, 'data-plan="hybrid" data-plan-price="24.99"', 'data-plan="hybrid" data-plan-price="19.99"', page=page)
    html = replace(html, '<h3>Hybrid</h3>\n<p class="v2-price">$24.99 <small>', '<h3>Hybrid</h3>\n<p class="v2-price">$19.99 <small>', page=page)
    html = replace(html, '<option value="hybrid" data-price="24.99">Hybrid — $24.99 per camera</option>',
                   '<option value="hybrid" data-price="19.99">Hybrid — $19.99 per camera</option>', page=page)
    for capacity, old, new in zip((8, 16, 32, 64), LICENSES_OLD, LICENSES_NEW):
        html = replace(html, f"<tr><th>Up to {capacity} cameras</th><td>{old} one-time</td></tr>",
                       f"<tr><th>Up to {capacity} cameras</th><td>{new} one-time</td></tr>", page=page)
    html = replace(html, "<li>Remote viewing where supported</li>", f"<li>{REMOTE[:-1]}</li>", page=page)
    # One Cloud Video Backup line replaces the two overlapping Hybrid cloud lines.
    html = replace(html, "<li>Cloud event protection: events kept in the cloud for 14 days</li>\n", "", page=page)
    html = replace(html, "<li>Remote cloud services</li>",
                   "<li>Cloud Video Backup: an extra copy of your event video clips, kept online for 14 days</li>", page=page)
    for old, key in (("2-day local recording", "basic"), ("7-day local recording", "ai"),
                     ("7-day local recording + 14-day cloud event retention", "hybrid")):
        html = replace(html, f'<p class="v2-retention">{old}</p>', f'<p class="v2-retention">{RETENTION[key]}</p>', page=page)
    html = insert_in_card(html, 'id="plan-ai-local"', "<ul>", f'<p class="v2-tagline">{AI_LOCAL}</p>\n', page=page)
    if STORAGE_NOTE not in html:
        html = replace(html, '<article class="card" id="plan-estimator"',
                       f'<p class="v2-storage-note">{STORAGE_NOTE}</p>\n\n<article class="card" id="plan-estimator"', page=page)
    for card in ('id="plan-basic-local"', 'id="plan-ai-local"', 'id="plan-hybrid"'):
        html = insert_in_card(html, card, "</ul>", f'<li class="v2-support">{SUPPORT}</li>\n', page=page)
    return html


def build_your_system(html, page="build-your-system.html"):
    html = replace(html, "<strong>Hybrid — $24.99 per camera / month</strong>", "<strong>Hybrid — $19.99 per camera / month</strong>", page=page)
    html = replace(html, "const PLAN_PRICE={basic_local:9.99,ai_local:14.99,hybrid:24.99};",
                   "const PLAN_PRICE={basic_local:9.99,ai_local:14.99,hybrid:19.99};", page=page)
    html = replace(html, "up to 8 cameras $49.99, 16 $79.99, 32 $129.99, 64 $199.99.",
                   "up to 8 cameras $24.99, 16 $37.49, 32 $62.49, 64 $99.99.", page=page)
    html = replace(html, "LICENCE=[[8,49.99],[16,79.99],[32,129.99],[64,199.99]]",
                   "LICENCE=[[8,24.99],[16,37.49],[32,62.49],[64,99.99]]", page=page)
    html = replace(html, "2-day local recording, basic motion and events, remote viewing where supported. No cloud event storage.",
                   "Up to 2 days of local recording, basic motion and events. Watch your cameras from anywhere. "
                   "No cloud video backup.", page=page)
    html = replace(html, "7-day local recording, person and vehicle detection, Smart Motion, AI search, Talk Down on supported "
                         "cameras. No cloud event storage.",
                   "Up to 7 days of local recording, person and vehicle detection, Smart Motion, AI search, Talk Down on "
                   "supported cameras. Watch your cameras from anywhere. No cloud video backup.", page=page)
    if STORAGE_NOTE not in html:
        html = replace(html, '</label></div><div class="field-grid" style="margin-top:1rem">',
                       f'</label></div><p class="inline-note">{STORAGE_NOTE}</p><div class="field-grid" style="margin-top:1rem">',
                       page=page)
    html = replace(html, "Everything in AI Local, plus 14-day cloud event retention. Not continuous cloud recording.",
                   "Everything in AI Local, plus Cloud Video Backup: an extra copy of your event video clips kept online "
                   "for 14 days. Not continuous cloud recording.", page=page)
    for plan in ("basic_local", "ai_local", "hybrid"):
        html = insert_in_card(html, f'name="subscription_plan" value="{plan}"', "</small>", f" {SUPPORT}.", page=page)
    return html


def home(html, page="index.html"):
    html = replace(html, '<h3>Hybrid</h3><div class="aic-price-value">$24.99</div>', '<h3>Hybrid</h3><div class="aic-price-value">$19.99</div>', page=page)
    html = replace(html, '<span class="aic-compare-plan">Hybrid</span><span class="aic-compare-price">$24.99</span>',
                   '<span class="aic-compare-plan">Hybrid</span><span class="aic-compare-price">$19.99</span>', page=page)
    html = replace(html, "AnyAiCam VMS Software · $49.99 one-time", "AnyAiCam VMS Software · $24.99 one-time", page=page)
    html = replace(html, '<h3>Software Only · Up to 8 Cameras</h3><div class="aic-price-value">$49.99</div>',
                   '<h3>Software Only · Up to 8 Cameras</h3><div class="aic-price-value">$24.99</div>', page=page)
    html = replace(html, "<p>Local VMS with AI-powered detection and smart alerts. Additional analytics modules may be separately licensed.</p>",
                   f"<p>{AI_LOCAL} Additional analytics modules may be separately licensed.</p>", page=page)
    for plan in ("Basic Local", "AI Local", "Hybrid"):
        html = insert_in_card(html, f'<div class="aic-price-body"><h3>{plan}</h3>', '<a class="button',
                              f'<p class="aic-price-support">{SUPPORT}</p>', page=page)
    # Comparison table, in everyday language: a label plus an explanation line (mobile-friendly, same columns).
    if ".aic-compare-desc{" not in html:
        html = replace(html, MOBILE_CSS, DESC_CSS + MOBILE_CSS, page=page)
    html = replace(html, "AnyAiCam VMS monthly plans, priced per camera. See exactly what each plan includes.",
                   "AnyAiCam VMS monthly plans, priced per camera. Every plan lets you watch your cameras from anywhere; "
                   "Hybrid also keeps a backup copy of your event video online.", page=page)
    html = replace(html, '<th scope="row">Remote viewing where supported</th>', _desc("Remote viewing", REMOTE_DETAIL), page=page)
    html = replace(html, '<th scope="row">Remote cloud services</th>', _desc("Cloud Video Backup", BACKUP), page=page)
    # One clear Cloud Video Backup row: the two overlapping Hybrid-only rows go (owner decision 2026-10-17).
    for label in ("Hybrid cloud services", "Cloud event protection"):
        marker = f'<tr><th scope="row">{label}</th>'
        if marker in html:
            start = html.index(marker)
            row = html[start:html.index("</tr>", start) + len("</tr>")]
            if row.count('class="aic-yes"') != 1:
                raise Mismatch(f"{page}: '{label}' is not a Hybrid-only row; refusing to remove it")
            html = html.replace(row, "", 1)
    if KEPT not in html:
        anchor = html.index('<tr><th scope="row">Local recording</th>')
        anchor_end = html.index("</tr>", anchor) + len("</tr>")
        kept = ("<tr>" + _desc(KEPT, STORAGE_NOTE + " Hybrid also keeps event video clips online for 14 days.")
                + '<td class="aic-compare-kept">Up to 2 days</td><td class="aic-compare-kept">Up to 7 days</td>'
                  '<td class="aic-col-recommended aic-compare-kept">Up to 7 days + 14 days online</td></tr>')
        html = html[:anchor_end] + kept + html[anchor_end:]
    # Comparison table: one row, included with all three plans (a copy of the all-included "Local VMS" row).
    if 'class="aic-compare-desc">' + SUPPORT not in html:
        start = html.index('<tr><th scope="row">Local VMS</th>')
        row = html[start:html.index("</tr>", start) + len("</tr>")]
        if row.count('class="aic-yes"') != 3:
            raise Mismatch(f"{page}: the Local VMS comparison row is not included in all three plans")
        anchor = html.index('<tr><th scope="row">Cloud Video Backup<span class="aic-compare-desc">')
        anchor_end = html.index("</tr>", anchor) + len("</tr>")
        html = html[:anchor_end] + row.replace('<th scope="row">Local VMS</th>', _desc("Free phone support", SUPPORT)) + html[anchor_end:]
    return html


def vms_page(html, page):
    return replace(html, "from $49.99 for 8 cameras", "from $24.99 for 8 cameras", page=page)


PAGES = {"plans.html": plans, "build-your-system.html": build_your_system, "index.html": home,
         "vms.html": lambda h: vms_page(h, "vms.html"), "vms-linux.html": lambda h: vms_page(h, "vms-linux.html")}


def main(argv) -> int:
    dry = "--dry-run" in argv
    results = {}
    for name, transform in PAGES.items():
        before = (OUT / name).read_bytes().decode("utf-8")
        after = transform(before)
        results[name] = after
        print(f"{name}: {'changed' if after != before else 'already applied'}; support lines "
              f"{after.count(SUPPORT)}; '$24.99' left {after.count('$24.99')} (8-camera license / add-ons only)")
    if dry:
        print("DRY RUN: nothing written")
        return 0
    for name, html in results.items():
        (OUT / name).write_bytes(html.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
