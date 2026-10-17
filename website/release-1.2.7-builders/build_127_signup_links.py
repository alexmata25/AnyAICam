"""plans.html and the prepared hardware-first build-your-system.html: 'Create an account' goes to the self-service
sign-up (/customer-signup), not the installer-request page (/customer-register). DRAFT.

plans.html is prepared from the copy anyaicam.com SERVES (Cloudflare rewrites one email link in it), so before
publishing re-run this on the SERVER original fetched through cPanel: python build_127_signup_links.py <server plans.html>.
"""
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).parent
OLD, NEW = 'href="https://app.anyaicam.com/customer-register"', 'href="https://app.anyaicam.com/customer-signup"'
# Owner wording (2026-10-16), shown in the order summary before checkout: why the licence is or isn't charged.
TOTALS_OLD = ("box.innerHTML='<strong>One-time cost</strong><ul>'+(k.oneTime.length?k.oneTime.map(i=>'<li>'+esc(i.label)+': '"
              "+money(i.amount)+'</li>').join(''):'<li>None</li>')+'</ul><p><strong>One-time total: '+money(k.oneTimeTotal)+'</strong></p>'")
TOTALS_NEW = ("const tierNow=val('appliance_tier'),licIncluded=APPLIANCE[tierNow]?'<li id=\"licenceIncluded\">AnyAiCam VMS Software License "
              "— FREE, included with your PC: $0.00</li>':'',licNote=tierNow==='own_pc'?'<p class=\"inline-note\" id=\"licenceRequired\">"
              "A one-time AnyAiCam VMS software license is required when using your own computer.</p>':'';\n"
              "  box.innerHTML='<strong>One-time cost</strong><ul>'+(k.oneTime.length?k.oneTime.map(i=>'<li>'+esc(i.label)+': '"
              "+money(i.amount)+'</li>').join(''):'<li>None</li>')+licIncluded+'</ul>'+licNote+'<p><strong>One-time total: '+money(k.oneTimeTotal)+'</strong></p>'")


STEP1 = '<section class="step-pane" data-step="1">'
INSTALLED_NOTICE = ('<p class="inline-note" id="installedNotice" hidden style="background:#e8f7f1;border:1px solid #b9e4d3;'
                    'border-radius:10px;padding:.7rem .9rem;margin:0 0 1rem">You already have AnyAiCam installed on your computer. '
                    'Choose your cameras and monthly plan here; after checkout, go back to AnyAiCam on that computer. '
                    'There is nothing to download or reinstall.</p>')
# Activate Your System hand-off from the installed VMS (owner decision 2026-10-16): ?source=installed preselects the
# customer's own PC. No identifier is read from the address; the cloud knows the linked PC from the signed-in account.
INSTALLED_SCRIPT = ("<script>(function(){try{if(new URLSearchParams(location.search).get('source')!=='installed')return;"
                    "var n=document.getElementById('installedNotice');if(n)n.hidden=false;"
                    "var r=document.querySelector('input[name=\"appliance_tier\"][value=\"own_pc\"]');"
                    "if(r){r.checked=true;r.dispatchEvent(new Event('change',{bubbles:true}));}}catch(e){}})();</script>")


# Activation-first (2026-10-16): My subscription is locked until the first purchase, so an existing customer who signs in
# from Build Your System goes to the order summary (the same place a new account goes), never to My subscription.
NEXT_OLD = "const next='/subscription-portal?'+handoffQuery();"
NEXT_NEW = "const next='/order-summary?'+handoffQuery();"
SUMMARY_OLD = ("document.getElementById('handoffSummary').textContent='In My subscription, confirm '+(labels[k.plan]||k.plan)+' for '"
               "+k.cameras+' camera'+(k.cameras===1?'':'s')+' ('+k.cameras+' × '+money(k.perCamera)+' = '+money(k.monthly)+'/month).';")
SUMMARY_NEW = ("document.getElementById('handoffSummary').textContent=tier==='residential'?'The AnyAiCam Residential VMS \u2014 Ryzen 5 "
               "can’t be ordered online yet.':'Next: review your order and pay in secure checkout \u2014 '"
               "+(labels[k.plan]||k.plan)+' for '+k.cameras+' camera'+(k.cameras===1?'':'s')+' ('+k.cameras+' × '+money(k.perCamera)+' = '"
               "+money(k.monthly)+'/month)'+(k.oneTime.length?' plus the one-time '+k.oneTime[0].label+' ('+money(k.oneTimeTotal)+')':'')+'.';")
# The Residential appliance can't be ordered online yet: no sign-in/checkout buttons that would start a different order.
RESIDENTIAL_OLD = "hwButton.hidden=!hardware;signIn.hidden=!!hardware;register.hidden=!!hardware;"
RESIDENTIAL_NEW = ("hwButton.hidden=!hardware;signIn.hidden=!!hardware||tier==='residential';"
                   "register.hidden=!!hardware||tier==='residential';")


# Activation-first, step 4 (owner decision 2026-10-16; Codex acceptance contract): the first checkout is cameras + plan
# (+ the own-PC licence) or hardware only. Optional analytics, AAC Face Access and door relays are chosen after
# activation in My subscription, so step 4 explains them instead of collecting quantities.
ADDONS_STEP = ('<section class="step-pane" data-step="3"><h2 tabindex="-1">Optional add-ons, after activation</h2>'
               '<p>Your first order covers your cameras, your monthly plan and, on your own computer, the one-time AnyAiCam VMS '
               'software license. Optional add-ons are chosen after activation in <strong>My subscription</strong>, once your '
               'system is running, so you only pay for what you use.</p><div class="feature-list">'
               '<div class="feature-row"><label>People Counting, License Plate Recognition and PPE detection<small>Analytics '
               'packages for suitable cameras, priced per camera per month.</small></label></div>'
               '<div class="feature-row"><label>AAC Face Access<small>Local face matching and access policy for entrance cameras, '
               'priced per door per month. Homes: the AnyAiCam Residential VMS \u2014 Ryzen 5 covers one front-door camera and up '
               'to 5 household members; commercial sites use the AnyAiCam Enterprise appliance.</small></label></div>'
               '<div class="feature-row"><label>Door control relay (Numato 3-channel, $149.99)<small>For AAC Face Access door '
               'control or local door and gate triggering.</small></label></div></div>'
               '<p class="inline-note">Smart Motion and person and vehicle detection are already included in AI Local and Hybrid.</p>'
               '<div class="constraint" id="faceAccessRequirement" hidden></div></section>')
ACTIVATION_TEXT = (
    ("<button type=\"button\" data-goto=\"3\">4 Features</button>", "<button type=\"button\" data-goto=\"3\">4 Add-ons</button>"),
    ("including front-door facial recognition for up to 5 household members", "including front-door AAC Face Access for up to 5 household members"),
    ("for commercial facial recognition and AAC Face Access", "for commercial AAC Face Access"),
    ("Facial recognition runs on the AnyAiCam Residential VMS", "AAC Face Access runs on the AnyAiCam Residential VMS"),
    ("face_access:'AAC Face Access',", ""),
    ("function faceQty(){return featureQuantities().face_access||0}", "function faceQty(){return 0}  /* Face Access is chosen after activation */"),
    ("const accessParts=[];if(access!=='none')", "const accessParts=[];if(access&&access!=='none')"),
    ("document.getElementById('sumAnalytics').textContent=parts.length?parts.join(', '):'None';",
     "document.getElementById('sumAnalytics').textContent=parts.length?parts.join(', '):'Added after activation';"),
    ("feedback.textContent='Standalone relay modules must be a whole number from 0 to 16.';",
     "feedback.textContent='Door relays must be a whole number from 0 to 16.';"),
    ("const relays=Number(val('standalone_relay_modules'))||0;if(relays>0)q.set('relays',String(relays));return q.toString()}",
     "const relays=Number(val('standalone_relay_modules'))||0;if(relays>0)q.set('relays',String(relays));\n"
     "  if(new URLSearchParams(location.search).get('source')==='installed')q.set('source','installed');return q.toString()}"),
)


def activation_first(html: str) -> str:
    import re
    found = re.findall(r'<section class="step-pane" data-step="3">.*?</section>', html, re.S)
    assert len(found) == 1, "features step not found exactly once"
    html = html.replace(found[0], ADDONS_STEP)
    for old, new in ACTIVATION_TEXT:
        assert html.count(old) == 1, old[:70]
        html = html.replace(old, new)
    lowered = html.lower()
    assert "face_access" not in lowered and "facial recognition" not in lowered and "relay modules" not in lowered, "left over"
    assert "data-feature=" not in html and "after activation" in lowered
    return html


def licence_explanation(html: str) -> str:
    assert html.count(TOTALS_OLD) == 1, "order-summary renderer not found exactly once"
    html = html.replace(TOTALS_OLD, TOTALS_NEW)
    for old, new in ((NEXT_OLD, NEXT_NEW), (SUMMARY_OLD, SUMMARY_NEW), (RESIDENTIAL_OLD, RESIDENTIAL_NEW)):
        assert html.count(old) == 1, old[:60]
        html = html.replace(old, new)
    assert html.count(STEP1) == 1 and html.count("</body>") == 1
    html = html.replace(STEP1, INSTALLED_NOTICE + STEP1)
    html = activation_first(html)
    return html.replace("</body>", INSTALLED_SCRIPT + "</body>")


plans_src = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "live-snapshot" / "plans.html"
bys_src = HERE.parent / "hw-checkout-20261015" / "website" / "prepared" / "public_html" / "build-your-system.html"
out = HERE / "prepared" / "public_html"
for name, src in (("plans.html", plans_src), ("build-your-system.html", bys_src)):
    html = src.read_text(encoding="utf-8")
    assert html.count(OLD) == 1, f"{name}: expected one register link, found {html.count(OLD)}"
    html = html.replace(OLD, NEW)
    if name == "build-your-system.html":
        html = licence_explanation(html)
    if name == "plans.html":
        PLANS_OLD = ('You choose your plan and number of cameras in <strong>My subscription</strong> after signing in. '
                     'The amount you pay is calculated by AnyAiCam and confirmed at checkout.')
        PLANS_NEW = ('You choose your cameras and plan in <a href="build-your-system.html">Build Your System</a> (on your own computer '
                     'the one-time AnyAiCam VMS software license is added to the same checkout). After that, change your plan or '
                     'add optional analytics anytime in <strong>My subscription</strong>. The amount you pay is calculated by '
                     'AnyAiCam and confirmed at checkout.')
        assert html.count(PLANS_OLD) == 1
        html = html.replace(PLANS_OLD, PLANS_NEW)
    (out / name).write_bytes(html.encode("utf-8"))
    print(name, "from", src.name, hashlib.sha256(html.encode()).hexdigest())
