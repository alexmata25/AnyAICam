import json
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from pricing_config import calculate_quote, load_pricing, public_pricing, save_pricing

ACCOUNT_FILE = Path('/app/recordings/account_management.json')
QUOTES_FILE = Path('/app/recordings/customer_quotes.json')


def _read(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temporary.replace(path)


def _money(value) -> str:
    return f'${float(value):,.2f}'


def _calculator_form(prefix='calc', include_customer=False) -> str:
    customer = '<label>Customer<input id="customer" placeholder="Customer name" required></label><label>Site<input id="site" placeholder="Site name" required></label>' if include_customer else ''
    return f'''<form class="panel rule-form pricing-form" id="{prefix}-form">{customer}<label>Resolution<select id="resolution"><option value="2mp">2MP / 1080p</option><option value="4mp">4MP</option><option value="8mp">8MP / 4K</option></select></label><label>Recording mode<select id="recording"><option value="motion">Motion</option><option value="continuous">Continuous</option></select></label><label>Retention<select id="retention"><option value="2">2 days</option><option value="7">7 days</option><option value="14">14 days</option><option value="30">30 days</option></select></label><label>Camera quantity<input id="quantity" type="number" min="1" max="128" value="4" required></label><fieldset style="border:1px solid var(--line);border-radius:9px;padding:13px"><legend>Analytics per camera</legend><label><input class="addon" type="checkbox" value="smart_motion"> Smart Motion</label><label><input class="addon" type="checkbox" value="people_counting"> People Counting</label><label><input class="addon" type="checkbox" value="lpr"> License Plate Recognition</label><label><input class="addon" type="checkbox" value="ppe"> Construction PPE Monitoring</label></fieldset><button class="action-button" type="submit">Calculate estimate</button></form>'''


def _calculator_script(prefix='calc', save_quote=False) -> str:
    save = "payload.customer=document.getElementById('customer').value;payload.site=document.getElementById('site').value;await fetch('/api/quotes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});" if save_quote else ''
    return f'''<script>const form=document.getElementById('{prefix}-form'),summary=document.getElementById('{prefix}-summary');form.addEventListener('submit',async event=>{{event.preventDefault();const payload={{resolution:form.querySelector('#resolution').value,recording:form.querySelector('#recording').value,retention:Number(form.querySelector('#retention').value),quantity:Number(form.querySelector('#quantity').value),addons:[...form.querySelectorAll('.addon:checked')].map(item=>item.value)}};const response=await fetch('/api/pricing/calculate',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}}),result=await response.json();if(!response.ok){{summary.innerHTML=`<div class="mock-banner">${{result.detail}}</div>`;showToast(result.detail);return}}{save}summary.innerHTML=`<h2>Quote summary</h2><div class="health-row"><span>Cloud recording subtotal</span><strong>${{money(result.cloud_subtotal)}}</strong></div><div class="health-row"><span>Analytics subtotal</span><strong>${{money(result.analytics_subtotal)}}</strong></div><div class="health-row"><span>Per-camera total</span><strong>${{money(result.per_camera_total)}}</strong></div><div class="health-row"><span>Estimated monthly total</span><strong>${{money(result.monthly_total)}}</strong></div><div class="health-row"><span>Estimated annual total (${{result.annual_discount_percent}}% discount)</span><strong>${{money(result.annual_total)}}</strong></div><p class="health-detail">${{result.trial_days}}-day free cloud-storage trial for new activations. Estimate only until the order is confirmed.</p>`;showToast('Pricing estimate calculated.')}});function money(value){{return new Intl.NumberFormat('en-US',{{style:'currency',currency:'USD'}}).format(value)}}</script>'''


def register_pricing_routes(app: FastAPI, shell: Callable) -> None:
    @app.get('/api/pricing-config')
    def pricing_config() -> dict:
        return public_pricing()

    @app.post('/api/pricing/calculate')
    def pricing_calculate(payload: dict) -> dict:
        try:
            return calculate_quote(payload)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.put('/api/pricing-config-legacy', include_in_schema=False)
    def pricing_update(payload: dict) -> dict:
        config = load_pricing()
        if 'trial_days' in payload:
            config['trial_days'] = max(0, min(365, int(payload['trial_days'])))
        if 'annual_discount_percent' in payload:
            config['annual_discount_percent'] = max(0, min(100, float(payload['annual_discount_percent'])))
        for key, value in payload.get('addons', {}).items():
            if key in config['addons']:
                config['addons'][key]['price'] = max(0, float(value))
        for key, value in payload.get('prices', {}).items():
            resolution, recording, retention = key.split('.')
            config['plans'][resolution][recording][retention] = max(0, float(value))
        for key, value in payload.get('conflict_resolutions', {}).items():
            if key in config['conflicts'] and float(value) in config['conflicts'][key]['options']:
                resolution, recording, retention = key.split('.')
                config['plans'][resolution][recording][retention] = float(value)
                config['conflicts'][key]['confirmed'] = True
                config['conflicts'][key]['confirmed_value'] = float(value)
        save_pricing(config)
        return {'status': 'complete', 'message': 'Pricing configuration saved.', 'config': config}

    @app.post('/api/quotes')
    def save_quote(payload: dict) -> dict:
        try:
            totals = calculate_quote(payload)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        quotes = _read(QUOTES_FILE, [])
        quote = {'id': uuid4().hex[:10], 'customer': payload.get('customer', ''), 'site': payload.get('site', ''), 'created_at': datetime.now().isoformat(), 'selection': payload, 'totals': totals, 'status': 'estimate'}
        quotes.append(quote); _write(QUOTES_FILE, quotes)
        return {'status': 'complete', 'quote': quote, 'message': 'Quote estimate saved.'}

    @app.get('/pricing', response_class=HTMLResponse)
    def pricing_page(request: Request) -> str:
        # /pricing, /quotes, /setup, and /subscription hand out and
        # display real customer/quote data (including a newly-created
        # customer's temporary password on /setup) and were reachable by
        # anyone, unauthenticated -- register_pricing_routes() never
        # required a partner identity. Lazily importing from main avoids a
        # circular import at module load time (main imports this module);
        # see page_shell()'s identical pattern for partner_identity.
        from main import partner_page_authorization_response
        authorization_response = partner_page_authorization_response(request)
        if authorization_response is not None:
            return authorization_response
        config = load_pricing(); trial = config['trial_days']
        content = f'''<header class="topbar"><div><p class="eyebrow">Reusable pricing configuration</p><h1>Subscription calculator</h1></div><a class="ghost-button" href="/quotes">Create customer quote</a></header><div class="mock-banner">Estimate only until the order is confirmed. New activations include a configurable {trial}-day free cloud-storage trial.</div><section class="health-grid">{_calculator_form()}<div class="panel" id="calc-summary"><h2>Subscription summary</h2><p class="health-detail">Choose a supported plan to see monthly and discounted annual totals.</p></div></section>'''
        return shell('Pricing', 'pricing', content, _calculator_script())

    @app.get('/quotes', response_class=HTMLResponse)
    def quotes_page(request: Request) -> str:
        # /pricing, /quotes, /setup, and /subscription hand out and
        # display real customer/quote data (including a newly-created
        # customer's temporary password on /setup) and were reachable by
        # anyone, unauthenticated -- register_pricing_routes() never
        # required a partner identity. Lazily importing from main avoids a
        # circular import at module load time (main imports this module);
        # see page_shell()'s identical pattern for partner_identity.
        from main import partner_page_authorization_response
        authorization_response = partner_page_authorization_response(request)
        if authorization_response is not None:
            return authorization_response
        quotes = list(reversed(_read(QUOTES_FILE, [])))
        rows = ''.join(f'<tr><td>{q["customer"]}</td><td>{q["site"]}</td><td>{q["totals"]["quantity"]}</td><td>{_money(q["totals"]["monthly_total"])}</td><td>{_money(q["totals"]["annual_total"])}</td><td>Estimate</td></tr>' for q in quotes) or '<tr><td colspan="6">No quotes saved yet.</td></tr>'
        content = f'''<header class="topbar"><div><p class="eyebrow">Partner quoting</p><h1>Customer quotes</h1></div></header><div class="mock-banner">All quotes are estimates until the order is confirmed.</div><section class="health-grid">{_calculator_form('quote', True)}<div class="panel" id="quote-summary"><h2>Quote summary</h2><p class="health-detail">Select a customer, site, and subscription options.</p></div></section><section class="panel"><div class="panel-head"><h2>Saved estimates</h2></div><div style="overflow:auto"><table class="data-table"><thead><tr><th>Customer</th><th>Site</th><th>Cameras</th><th>Monthly</th><th>Annual</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></div></section>'''
        return shell('Quotes', 'quotes', content, _calculator_script('quote', True))

    @app.get('/setup', response_class=HTMLResponse)
    def setup_page(request: Request) -> str:
        # /pricing, /quotes, /setup, and /subscription hand out and
        # display real customer/quote data (including a newly-created
        # customer's temporary password on /setup) and were reachable by
        # anyone, unauthenticated -- register_pricing_routes() never
        # required a partner identity. Lazily importing from main avoids a
        # circular import at module load time (main imports this module);
        # see page_shell()'s identical pattern for partner_identity.
        from main import partner_page_authorization_response
        authorization_response = partner_page_authorization_response(request)
        if authorization_response is not None:
            return authorization_response
        content = '''<header class="topbar"><div><p class="eyebrow">Customer onboarding</p><h1>Setup wizard</h1></div></header><div class="mock-banner">Analytics remains in demo mode. Pricing is an estimate until the order is confirmed.</div><section class="health-grid"><form class="panel rule-form" id="setup-price-form"><label>Customer name<input id="setup-customer" required></label><label>Customer email<input id="setup-email" type="email" required></label><label>Site name<input id="setup-site" value="Home" required></label><label>Appliance<select id="setup-appliance"><option>AnyAiCam mini PC</option><option>Customer-owned computer</option></select></label><label>Resolution<select id="setup-resolution"><option value="2mp">2MP / 1080p</option><option value="4mp">4MP</option><option value="8mp">8MP / 4K</option></select></label><label>Recording mode<select id="setup-recording"><option value="motion">Motion</option><option value="continuous">Continuous</option></select></label><label>Retention<select id="setup-retention"><option value="2">2 days</option><option value="7">7 days</option><option value="14">14 days</option><option value="30">30 days</option></select></label><label>Camera quantity<input id="setup-quantity" type="number" min="1" max="128" value="4"></label><fieldset style="border:1px solid var(--line);border-radius:9px;padding:13px"><legend>Analytics per camera</legend><label><input class="setup-addon" type="checkbox" value="smart_motion"> Smart Motion</label><label><input class="setup-addon" type="checkbox" value="people_counting"> People Counting</label><label><input class="setup-addon" type="checkbox" value="lpr"> License Plate Recognition</label><label><input class="setup-addon" type="checkbox" value="ppe"> Construction PPE Monitoring</label></fieldset><button class="action-button">Calculate and create customer</button></form><div class="panel" id="setup-summary"><h2>Installation and subscription summary</h2><p class="health-detail">Complete the form to create the customer login, estimate, and checklist.</p></div></section>'''
        scripts = '''<script>const setupForm=document.getElementById('setup-price-form'),setupSummary=document.getElementById('setup-summary');setupForm.addEventListener('submit',async event=>{event.preventDefault();const pricing={resolution:document.getElementById('setup-resolution').value,recording:document.getElementById('setup-recording').value,retention:Number(document.getElementById('setup-retention').value),quantity:Number(document.getElementById('setup-quantity').value),addons:[...document.querySelectorAll('.setup-addon:checked')].map(item=>item.value)};let response=await fetch('/api/pricing/calculate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(pricing)}),totals=await response.json();if(!response.ok){setupSummary.innerHTML=`<div class="mock-banner">${totals.detail}</div>`;showToast(totals.detail);return}const payload={customer_name:document.getElementById('setup-customer').value,email:document.getElementById('setup-email').value,appliance_type:document.getElementById('setup-appliance').value,camera_count:pricing.quantity,sites:[{name:document.getElementById('setup-site').value,site_type:'Customer site',camera_count:pricing.quantity}],pricing};response=await fetch('/api/setup/complete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const result=await response.json();setupSummary.innerHTML=`<h2>Installation checklist</h2><p><strong>Login:</strong> ${result.login}<br><strong>Temporary password:</strong> ${result.temporary_password}</p><div class="health-row"><span>Monthly estimate</span><strong>${money(totals.monthly_total)}</strong></div><div class="health-row"><span>Annual estimate after ${totals.annual_discount_percent}% discount</span><strong>${money(totals.annual_total)}</strong></div><p>${totals.trial_days}-day free cloud-storage trial for new activations.</p><ol><li>Confirm estimate and pricing conflicts</li><li>Prepare appliance and network</li><li>Connect and verify cameras</li><li>Confirm recording and retention</li><li>Change temporary password</li><li>Test Tailscale remote access</li></ol>`;showToast(result.message)});function money(value){return new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(value)}</script>'''
        return shell('Setup wizard', 'setup', content, scripts)

    @app.get('/subscription', response_class=HTMLResponse)
    def subscription_page(request: Request) -> str:
        # /pricing, /quotes, /setup, and /subscription hand out and
        # display real customer/quote data (including a newly-created
        # customer's temporary password on /setup) and were reachable by
        # anyone, unauthenticated -- register_pricing_routes() never
        # required a partner identity. Lazily importing from main avoids a
        # circular import at module load time (main imports this module);
        # see page_shell()'s identical pattern for partner_identity.
        from main import partner_page_authorization_response
        authorization_response = partner_page_authorization_response(request)
        if authorization_response is not None:
            return authorization_response
        account = _read(ACCOUNT_FILE, {}); pricing = account.get('pricing', {}); summary = '<div class="empty">No customer subscription estimate has been saved yet. Use Setup Wizard or Quotes.</div>'
        if pricing:
            try:
                totals = calculate_quote(pricing)
                summary = f'<div class="health-row"><span>Customer</span><strong>{account.get("customer",{}).get("name","Customer")}</strong></div><div class="health-row"><span>Cameras</span><strong>{totals["quantity"]}</strong></div><div class="health-row"><span>Monthly estimate</span><strong>{_money(totals["monthly_total"])}</strong></div><div class="health-row"><span>Annual estimate</span><strong>{_money(totals["annual_total"])}</strong></div>'
            except ValueError as error:
                summary = f'<div class="mock-banner">{error}</div>'
        return shell('Subscription', 'subscription', '<header class="topbar"><div><p class="eyebrow">Account estimate</p><h1>Subscription summary</h1></div></header><div class="mock-banner">Estimate only until the order is confirmed.</div>'+f'<section class="panel">{summary}</section>')

    @app.get('/pricing-admin-legacy', response_class=HTMLResponse, include_in_schema=False)
    def pricing_admin_page() -> str:
        config = load_pricing(); price_inputs=[]
        for resolution, plan in config['plans'].items():
            for recording in ('motion','continuous'):
                for retention, price in plan[recording].items():
                    key=f'{resolution}.{recording}.{retention}'; conflict=config['conflicts'].get(key)
                    if conflict and not conflict.get('confirmed'):
                        choices=''.join(f'<option value="{option}">${option:.2f} — {conflict["sources"][index]}</option>' for index,option in enumerate(conflict['options']))
                        price_inputs.append(f'<label>{conflict["label"]}<select class="conflict" data-key="{key}"><option value="">Administrator confirmation required</option>{choices}</select></label>')
                    else:
                        price_inputs.append(f'<label>{plan["label"]} · {recording.title()} · {retention} days<input class="plan-price" data-key="{key}" type="number" min="0" step="0.01" value="{price if price is not None else ""}"></label>')
        addons=''.join(f'<label>{item["label"]}<input class="addon-price" data-key="{key}" type="number" min="0" step="0.01" value="{item["price"]}"></label>' for key,item in config['addons'].items())
        body=f'''<div class="mock-banner">Administrator page. Conflicted source prices must be explicitly confirmed below.</div><form id="admin-pricing" class="rule-form"><div class="account-grid"><section class="panel"><h2>Billing settings</h2><label>Free trial days<input id="trial-days" type="number" min="0" max="365" value="{config['trial_days']}"></label><label>Annual discount percent<input id="annual-discount" type="number" min="0" max="100" step="0.01" value="{config['annual_discount_percent']}"></label><h2>Analytics add-ons</h2>{addons}</section><section class="panel"><h2>Cloud recording prices</h2>{''.join(price_inputs)}</section></div><button class="action-button" style="margin-top:16px">Save pricing configuration</button></form>'''
        scripts='''<script>document.getElementById('admin-pricing').addEventListener('submit',async event=>{event.preventDefault();const payload={trial_days:Number(document.getElementById('trial-days').value),annual_discount_percent:Number(document.getElementById('annual-discount').value),addons:{},prices:{},conflict_resolutions:{}};document.querySelectorAll('.addon-price').forEach(input=>payload.addons[input.dataset.key]=Number(input.value));document.querySelectorAll('.plan-price').forEach(input=>payload.prices[input.dataset.key]=Number(input.value));document.querySelectorAll('.conflict').forEach(select=>{if(select.value)payload.conflict_resolutions[select.dataset.key]=Number(select.value)});const response=await fetch('/api/pricing-config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),result=await response.json();showToast(result.message);setTimeout(()=>location.reload(),700)});</script>'''
        return shell('Pricing administration', 'pricing-admin', '<header class="topbar"><div><p class="eyebrow">Authorized administrators</p><h1>Pricing administration</h1></div></header>'+body, scripts)
