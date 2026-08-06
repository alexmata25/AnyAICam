"""Server-rendered customer experience pages."""

from __future__ import annotations

from html import escape


def _value(value, fallback="—") -> str:
    return escape(str(value if value not in (None, "") else fallback))


def _bytes(value) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"


def dashboard_page(model: dict, display_name: str) -> str:
    counts = model["camera_counts"]
    appliances = model["appliances"]
    healthy = sum(str(item.get("state") or item.get("online_status") or "").lower() == "online" for item in appliances)
    storage = model["storage"]
    events = "".join(
        f'<tr><td>{_value(item.get("event_timestamp"))}</td><td>{_value(item.get("event_type"))}</td><td>{_value(item.get("camera_id"))}</td></tr>'
        for item in model["events"]
    ) or '<tr><td colspan="3">No AI events yet.</td></tr>'
    alerts = "".join(
        f'<article class="setting-link"><div><strong>{_value(item.get("title"), "Alert")}</strong><div class="health-detail">{_value(item.get("message"), item.get("severity"))}</div></div><span>{_value(item.get("timestamp"))}</span></article>'
        for item in model["alerts"]
    ) or '<div class="empty">No recent alerts.</div>'
    camera_cards = "".join(
        f'<article class="feature-card"><div class="feature-icon">●</div><h2>{_value(item.get("name"), "Camera")}</h2><p>{_value(item.get("status"), "offline")} · {_value(item.get("resolution"), "Resolution pending")}</p><a class="download" href="/live">Open live view</a></article>'
        for item in model["cameras"]
    ) or '<div class="empty">No cameras have been discovered yet.</div>'
    return f'''<header class="topbar"><div><p class="eyebrow">{_value(model["tenant"].get("name"))}</p><h1>Welcome, {_value(display_name, "Customer")}</h1></div><span class="pill">Tenant isolated</span></header>
<section class="launch-summary">
  <article class="launch-stat"><span>Cameras online</span><strong>{counts["online"]} / {counts["total"]}</strong></article>
  <article class="launch-stat"><span>Recent AI events</span><strong>{len(model["events"])}</strong></article>
  <article class="launch-stat"><span>Healthy appliances</span><strong>{healthy} / {len(appliances)}</strong></article>
  <article class="launch-stat"><span>Recording storage</span><strong>{_bytes(storage.get("used_bytes"))}</strong></article>
</section>
<section class="panel" style="margin-top:18px"><div class="panel-head"><div><h2>Camera status</h2><div class="health-detail">{counts["offline"]} camera(s) offline or awaiting first connection.</div></div><a class="ghost-button" href="/customer-admin/cameras">Manage cameras</a></div><div class="feature-grid">{camera_cards}</div></section>
<div class="notification-grid" style="margin-top:18px"><section class="panel"><div class="panel-head"><div><h2>Recent AI events</h2><div class="health-detail">Latest tenant-owned event metadata.</div></div></div><div style="overflow:auto"><table class="data-table"><thead><tr><th>Time</th><th>Event</th><th>Camera</th></tr></thead><tbody>{events}</tbody></table></div></section><section class="panel"><div class="panel-head"><div><h2>Recent alerts</h2><div class="health-detail">Unread and recent customer notifications.</div></div></div><div class="settings-list">{alerts}</div></section></div>
<section class="panel" style="margin-top:18px"><div class="panel-head"><div><h2>Customer administration</h2><div class="health-detail">Manage only the users, sites, cameras, and sharing rules in this organization.</div></div></div><div class="settings-list"><a class="setting-link" href="/customer-admin/users"><div><strong>Users</strong><div class="health-detail">Customer administrators, managers, viewers, and guards.</div></div><span>Open →</span></a><a class="setting-link" href="/customer-admin/sites"><div><strong>Sites</strong><div class="health-detail">Locations and assigned Edge appliances.</div></div><span>Open →</span></a><a class="setting-link" href="/customer-admin/cameras"><div><strong>Cameras</strong><div class="health-detail">Camera status and appliance assignment.</div></div><span>Open →</span></a><a class="setting-link" href="/customer-admin/permissions"><div><strong>Permissions</strong><div class="health-detail">Per-user, per-camera access.</div></div><span>Open →</span></a></div></section>'''


def table_page(title: str, description: str, headers: list[str], rows: list[list[object]], action: str = "") -> str:
    headings = "".join(f"<th>{escape(item)}</th>" for item in headers)
    body = "".join("<tr>" + "".join(f"<td>{_value(value)}</td>" for value in row) + "</tr>" for row in rows)
    if not body:
        body = f'<tr><td colspan="{len(headers)}">No records yet.</td></tr>'
    return f'''<header class="topbar"><div><p class="eyebrow">Customer administration</p><h1>{escape(title)}</h1></div>{action}</header><section class="panel"><div class="panel-head"><div><h2>{escape(title)}</h2><div class="health-detail">{escape(description)}</div></div></div><div style="overflow:auto"><table class="data-table"><thead><tr>{headings}</tr></thead><tbody>{body}</tbody></table></div></section>'''


def onboarding_wizard_page(appliances: list[dict]) -> tuple[str, str]:
    appliance_options = '<option value="">Create a new Edge assignment</option>' + "".join(
        f'<option value="{escape(str(row["id"]), quote=True)}" data-cloud="{escape(str(row.get("cloud_id") or ""), quote=True)}">{_value(row.get("cloud_id"), row["id"])}</option>'
        for row in appliances
    )
    labels = ["Company", "Administrator", "First site", "Edge", "Discovery", "Subscription", "Invitation", "Complete"]
    progress = "".join(
        f'<button type="button" class="workspace-tab wizard-tab" data-step="{index}">{index}. {escape(label)}</button>'
        for index, label in enumerate(labels, 1)
    )
    content = f'''<header class="topbar"><div><p class="eyebrow">Platform administration</p><h1>New Customer</h1></div><span class="pill">Guided onboarding</span></header>
<section class="panel"><div class="workspace-tabs" id="customer-wizard-tabs">{progress}</div><form id="tenant-onboarding" class="rule-form">
<fieldset class="customer-wizard-step" data-step="1"><legend>Company information</legend><label>Company name<input name="tenant_name" required minlength="2" maxlength="120"></label><label>Company email<input name="company_email" type="email" required></label><label>Company phone<input name="company_phone" autocomplete="tel"></label></fieldset>
<fieldset class="customer-wizard-step" data-step="2" hidden><legend>Primary administrator</legend><label>Full name<input name="admin_name" required></label><label>Login email<input name="admin_email" type="email" required></label><p class="health-detail">This person becomes Customer Admin and must replace the temporary password at first login.</p></fieldset>
<fieldset class="customer-wizard-step" data-step="3" hidden><legend>First site</legend><label>Site name<input name="site_name" value="Primary Site" required></label><label>Address<input name="site_address" autocomplete="street-address"></label></fieldset>
<fieldset class="customer-wizard-step" data-step="4" hidden><legend>Edge appliance assignment</legend><label>Available appliance<select name="appliance_id" id="wizard-appliance">{appliance_options}</select></label><label>New appliance Cloud ID<input name="cloud_id" id="wizard-cloud-id" placeholder="Generated automatically when blank"></label></fieldset>
<fieldset class="customer-wizard-step" data-step="5" hidden><legend>Camera discovery</legend><div class="mock-banner"><strong>Discovery starts on the Edge Appliance.</strong> After the customer is created, the completion screen links to the tenant-safe discovery workflow. AWS will not connect directly to private camera addresses.</div></fieldset>
<fieldset class="customer-wizard-step" data-step="6" hidden><legend>Subscription selection</legend><label>Plan<select name="plan_code"><option value="trial">Trial</option><option value="starter">Starter</option><option value="professional">Professional</option><option value="enterprise">Enterprise</option></select></label><label>Camera limit<input name="camera_limit" type="number" min="1" max="512" value="4"></label></fieldset>
<fieldset class="customer-wizard-step" data-step="7" hidden><legend>Invitation email</legend><div class="health-detail">A pending invitation is prepared for the primary administrator. Delivery uses the configured email provider when enabled.</div><div id="wizard-review" class="mock-banner"></div></fieldset>
<fieldset class="customer-wizard-step" data-step="8" hidden><legend>Completion summary</legend><div id="tenant-result" class="settings-list"><div class="empty">Submit the reviewed customer to see the completion summary.</div></div></fieldset>
<div class="library-toolbar"><button class="ghost-button" id="wizard-back" type="button">Back</button><button class="action-button" id="wizard-next" type="button">Continue</button><button class="action-button" id="wizard-submit" type="submit" hidden>Create customer</button></div></form></section>'''
    scripts = '''<script>
(()=>{let step=1;const form=document.getElementById('tenant-onboarding'),panels=[...document.querySelectorAll('.customer-wizard-step')],tabs=[...document.querySelectorAll('.wizard-tab')],back=document.getElementById('wizard-back'),next=document.getElementById('wizard-next'),submit=document.getElementById('wizard-submit');
function values(){const data=Object.fromEntries(new FormData(form));data.camera_limit=Number(data.camera_limit);return data}
function show(){panels.forEach(panel=>panel.hidden=Number(panel.dataset.step)!==step);tabs.forEach(tab=>tab.classList.toggle('active',Number(tab.dataset.step)===step));back.hidden=step===1||step===8;next.hidden=step>=7;submit.hidden=step!==7;if(step===7){const d=values();document.getElementById('wizard-review').textContent=`${d.tenant_name||'Company'} · ${d.admin_email||'administrator pending'} · ${d.site_name||'Primary Site'} · ${d.plan_code||'trial'} · ${d.camera_limit||0} cameras`;}}
function valid(){const panel=panels.find(item=>Number(item.dataset.step)===step);return [...panel.querySelectorAll('input,select')].every(input=>input.reportValidity())}
next.onclick=()=>{if(valid()){step++;show()}};back.onclick=()=>{step--;show()};tabs.forEach(tab=>tab.onclick=()=>{const target=Number(tab.dataset.step);if(target<step){step=target;show()}});
document.getElementById('wizard-appliance').onchange=event=>{const option=event.target.selectedOptions[0];if(option&&option.dataset.cloud)document.getElementById('wizard-cloud-id').value=option.dataset.cloud};
form.onsubmit=async event=>{event.preventDefault();if(!valid())return;submit.disabled=true;try{const response=await fetch('/api/tenants/onboard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values())}),result=await response.json();if(!response.ok)throw new Error(result.detail||'Customer could not be created.');step=8;show();document.getElementById('tenant-result').innerHTML=`<article class="setting-link"><div><strong>${result.tenant.name}</strong><div class="health-detail">Tenant ${result.tenant.id}</div></div><span>Created</span></article><article class="setting-link"><div><strong>${result.primary_administrator.email}</strong><div class="health-detail">Invitation delivery: ${result.invitation.delivery_status}; expires ${result.invitation.expires_at}</div></div><span>Customer Admin</span></article><article class="setting-link"><div><strong>${result.appliance.cloud_id}</strong><div class="health-detail">Edge assignment ready for local discovery.</div></div><span>${result.appliance.status}</span></article><a class="setting-link" href="${result.next_steps.camera_discovery}"><div><strong>Continue to camera discovery</strong><div class="health-detail">Run discovery from the assigned Edge Appliance.</div></div><span>Open →</span></a>`;showToast('Customer tenant created.')}catch(error){showToast(error.message)}finally{submit.disabled=false}};show()})();
</script>'''
    return content, scripts
