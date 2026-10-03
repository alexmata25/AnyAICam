"""Presentation handoff exercises real JavaScript; APIs keep their security gates."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import appliance_claims
import partner_portal
from test_appliance_claims import (  # noqa: F401 -- established isolated fixtures
    DEVICE_SECRET, _begin, _confirm, _customer_cookie, _lookup, _seeded_customer,
    _status, claim_flow_key, client, db_path, identity_file,
)


def _node(harness, source):
    node = shutil.which('node')
    assert node, 'Node.js is required for the executable browser-message security tests'
    result = subprocess.run([node, '-e', harness], input=source, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_portal_prefill_executes_only_for_its_loopback_opener_and_never_confirms():
    _node(r"""
const vm = require('vm'), assert = require('assert'), source = require('fs').readFileSync(0, 'utf8');
function page(initial='') {
  let listener, lookups=0, confirmations=0, replies=[];
  const opener = {postMessage: (data, origin) => replies.push({data, origin})};
  const field = {value: initial};
  const context = {window: {opener, addEventListener: (_, fn) => listener=fn}, document: {
    getElementById: id => id==='claim-code-input' ? field : id==='claim-lookup-button' ? {click: () => lookups++} : {click: () => confirmations++}
  }};
  vm.runInNewContext(source, context);
  const nonce='12345678-1234-4234-8234-123456789abc';
  const emit = (data, origin='http://127.0.0.1:4567', from=opener) => listener({data, origin, source: from});
  return {field, replies, emit, nonce, get lookups(){return lookups}, get confirmations(){return confirmations}};
}
const p=page(), probe={type:'anyaicam-claim-probe', challenge:p.nonce}, fill={type:'anyaicam-claim-prefill', challenge:p.nonce, code:'ABCD2345'};
p.emit(fill); assert.equal(p.field.value, ''); // cannot skip handshake
for(const origin of ['https://evil.example','http://localhost:4567','null','http://127.0.0.1:4567.evil.example']) {
  p.emit(probe, origin); p.emit(fill, origin);
}
p.emit(probe, undefined, {}); p.emit(null); p.emit({type:'anyaicam-claim-probe',challenge:42});
assert.equal(p.replies.length, 0); assert.equal(p.lookups, 0);
p.emit(probe); assert.equal(p.replies.length,1); assert.equal(p.replies[0].origin,'http://127.0.0.1:4567');
p.emit({...fill, challenge:'87654321-1234-4234-8234-123456789abc'});
p.emit({...fill, code:'<script>'}); assert.equal(p.field.value,'');
p.emit(fill); assert.equal(p.field.value,'ABCD2345'); assert.equal(p.lookups,1); assert.equal(p.confirmations,0);
assert.equal(p.replies[1].data.type,'anyaicam-claim-prefilled');
p.emit(fill); assert.equal(p.lookups,1); // retry gets ack, never an extra action
p.emit({...fill, code:'ZZZZ2345'}); assert.equal(p.field.value,'ABCD2345');
const manual=page('MANU2345'); manual.emit({type:'anyaicam-claim-probe',challenge:manual.nonce});
assert.equal(manual.replies.length,0); assert.equal(manual.field.value,'MANU2345');
""", appliance_claims.CLAIM_PREFILL_SCRIPT)


def test_local_browser_script_checks_exact_portal_and_child_and_scrubs_nonce():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'installer'))
    import customer_setup
    _node(r"""
const vm=require('vm'), assert=require('assert'), source=require('fs').readFileSync(0,'utf8');
(async()=>{
  let listener, interval, requests=[], messages=[], urls=[], scrubbed=[];
  const elements={status:{textContent:''},connect:{disabled:true},'manual-code':{textContent:''}};
  const child={closed:false,postMessage:(data,origin)=>messages.push({data,origin})};
  const nonce='12345678-1234-4234-8234-123456789abc';
  const context={location:{hash:'#local-nonce'},history:{replaceState:(...args)=>scrubbed.push(args)},
    crypto:{randomUUID:()=>nonce}, document:{getElementById:id=>elements[id]},
    fetch:(path,opts)=>{requests.push({path,opts});return Promise.resolve({ok:true,json:()=>Promise.resolve({portal_url:'https://portal.example.test',claim_code:'ABCD2345'})})},
    window:{open:(url)=>{urls.push(url);return child},addEventListener:(_,fn)=>listener=fn},
    setInterval:fn=>{interval=fn;return 1},clearInterval:()=>{}};
  vm.runInNewContext(source,context); await new Promise(resolve=>setImmediate(resolve));
  assert.equal(scrubbed[0][2],'/'); assert.equal(requests[0].path,'/handoff');
  assert.equal(requests[0].opts.headers['X-AnyAiCam-Local-Token'],'local-nonce');
  elements.connect.onclick(); assert.equal(urls[0],'https://portal.example.test/customer/claim-appliance');
  assert(!urls[0].includes('ABCD2345')); interval(); assert.equal(messages[0].data.type,'anyaicam-claim-probe');
  const ready={data:{type:'anyaicam-claim-ready',challenge:nonce},origin:'https://portal.example.test',source:child};
  listener({...ready, origin:'https://evil.example'}); listener({...ready, source:{}});
  listener({...ready, data:{...ready.data,challenge:'wrong'}}); assert.equal(messages.length,1);
  listener(ready); assert.equal(messages[1].data.code,'ABCD2345'); assert.equal(messages[1].origin,'https://portal.example.test');
  interval(); assert.equal(messages[2].data.type,'anyaicam-claim-prefill'); // retries until receipt
  listener({...ready,data:{type:'anyaicam-claim-prefilled',challenge:nonce}});
  const count=messages.length; interval(); assert.equal(messages.length,count);
  assert.equal(elements['manual-code'].textContent,'Code sent to portal.');
})().catch(error=>{console.error(error);process.exit(1)});
""", customer_setup.BRIDGE_SCRIPT)


def test_authenticated_page_handoff_reuses_existing_api_and_no_url_secrets(client, db_path):
    _seeded_customer(db_path)
    opened = _begin(client).json()
    owner = _customer_cookie()
    page = client.get('/customer/claim-appliance', cookies={partner_portal.SESSION_COOKIE: owner})
    assert page.status_code == 200 and 'anyaicam-claim-prefill' in page.text
    for secret in (opened['claim_code'], opened['claim_session_id'], DEVICE_SECRET):
        assert secret not in page.text
    assert _lookup(client, opened['claim_code'], cookie=owner).status_code == 200
    assert _confirm(client, opened['claim_code'], 'site-other', cookie=owner).status_code == 403
    # An owner looking up the code has not linked the device yet.
    assert _status(client, opened['claim_session_id']).json()['status'] == 'pending'
    confirmed = _confirm(client, opened['claim_code'], 'site-1', cookie=owner)
    assert confirmed.status_code == 200 and 'claim_proof' not in confirmed.text
    stolen = client.post('/api/appliance/claim/status', json={
        'claim_session_id': opened['claim_session_id'], 'device_secret': 'wrong-secret'})
    assert 'claim_proof' not in stolen.text
    assert _status(client, opened['claim_session_id']).json()['claim_proof']


def test_login_handoff_contains_only_a_fixed_return_path_and_viewers_are_refused(client, db_path):
    _seeded_customer(db_path)
    response = client.get('/customer/claim-appliance', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/customer-login.html?next=/customer/claim-appliance'
    viewer = partner_portal._token('viewer@example.test', 'customer_viewer', None, 'cust-1')
    response = client.get('/customer/claim-appliance', cookies={partner_portal.SESSION_COOKIE: viewer})
    assert response.status_code == 403 and 'anyaicam-claim-prefill' not in response.text
