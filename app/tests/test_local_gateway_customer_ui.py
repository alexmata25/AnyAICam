"""Mocked customer UI and appliance status authorization; no VMS or provider."""
import ast
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from contextlib import contextmanager

from app import local_gateway_customer_ui as ui
from app import local_gateway_remote_billing as billing
from app import cloud_gateway_customer_status as cloud_status


class FakeError(Exception):
    def __init__(self, status_code, detail): self.status_code, self.detail = status_code, detail


class Router:
    def __init__(self): self.routes = {}
    def get(self, path): return lambda fn: self.routes.setdefault(('GET', path), fn)
    def post(self, path): return lambda fn: self.routes.setdefault(('POST', path), fn)


class CustomerGatewayUiTests(unittest.TestCase):
    def setUp(self):
        self.identity = {'role':'customer_owner','customer_id':'customer-a'}
        self.appliances = {'a':{'id':'a','customer_id':'customer-a','gateway_edition':'local_gateway',
                                'activation_status':'activated','state':'online','cloud_id':'AIC-A'},
                           'b':{'id':'b','customer_id':'customer-b','gateway_edition':'local_gateway',
                                'activation_status':'activated','state':'online','cloud_id':'AIC-B'}}
        self.binding = None
        self.pending = None
        self.license_capacity = 4
        self.reversed = False
        self.verified = True
        self.cameras = []
        # Exercise the existing checkout contract with a synthetic provider;
        # separate tests verify the real source-level stop stays closed.
        held = patch.object(billing, 'CHECKOUT_AUTHORIZATION_ATOMICALLY_ENFORCED', True)
        held.start(); self.addCleanup(held.stop)
        verified = patch.object(billing, '_edition_verified', side_effect=lambda customer,appliance,edition:self.verified)
        verified.start(); self.addCleanup(verified.stop)
        def row(sql, args):
            if 'FROM appliances' in sql:
                item = self.appliances.get(args[0])
                return item if item and item['customer_id'] == args[1] and (len(args)<3 or item['gateway_edition'] == args[2]) else None
            if 'local_gateway_remote_subscriptions' in sql: return self.binding
            if 'checkout_pending' in sql: return self.pending
            raise AssertionError(sql)
        def rows(sql, args):
            if 'FROM cameras' in sql:
                return list(self.cameras)
            return [x for x in self.appliances.values() if x['customer_id'] == args[0] and (x['gateway_edition'] in args[1:] or (x.get('activated_via') == 'claim' and x['gateway_edition'] is None))]
        api = ModuleType('fastapi'); api.HTTPException = FakeError; api.Request = type('Request', (), {})
        self.modules = {'fastapi':api, 'partner_portal':SimpleNamespace(partner_identity=lambda request:self.identity), 'cloud_gateway_customer_status':cloud_status,
                        'partner_db':SimpleNamespace(row=row,rows=rows),
                        'customer_entitlements':SimpleNamespace(get_entitlements_for_customer=lambda customer:[],
                            _purchased_vms_license_capacity=lambda entitlements:self.license_capacity),
                        'per_camera_billing':SimpleNamespace(own_pc_licence_reversed=lambda customer:self.reversed),
                        'appliance_readiness':SimpleNamespace(appliance_activated=lambda appliance:
                            appliance.get('activation_status') == 'activated' or
                            (appliance.get('activated_via') == 'claim' and bool(appliance.get('ready'))))}
        self.patches = patch.dict(sys.modules, self.modules); self.patches.start(); self.addCleanup(self.patches.stop)
        self.env = patch.dict(os.environ, {'ANYAICAM_LOCAL_GATEWAY_CHECKOUT_ENABLED':'true'}); self.env.start(); self.addCleanup(self.env.stop)
        self.router = Router(); billing.register_routes(self.router)
        self.list_route = self.router.routes[('GET','/api/v2/customer/local-gateway/appliances')]
        self.detail_route = self.router.routes[('GET','/api/v2/customer/local-gateway/appliances/{appliance_id}')]

    def test_list_and_detail_are_customer_scoped(self):
        self.assertEqual([item['appliance_id'] for item in self.list_route(object())['appliances']], ['a'])
        self.assertEqual(self.detail_route('a',object())['name'], 'AIC-A')
        with self.assertRaises(FakeError) as error: self.detail_route('b',object())
        self.assertEqual(error.exception.status_code, 404)
        self.identity = None
        with self.assertRaises(FakeError) as error: self.list_route(object())
        self.assertEqual(error.exception.status_code, 403)

    def test_subscription_statuses_are_conservative(self):
        appliance = self.appliances['a']
        base = {'status':'active','stripe_status':'active','payment_failed_at':None,
                'current_period_end':2000,'cancel_at_period_end':False}
        for updates, expected in [({},'active'), ({'current_period_end':1000},'expired'),
            ({'current_period_end':None},'unknown'), ({'cancel_at_period_end':True},'canceling'),
            ({'stripe_status':'canceled','status':'inactive'},'canceled'),
            ({'stripe_status':'incomplete_expired','status':'inactive'},'expired'),
            ({'payment_failed_at':'failed'},'payment_failed'), ({'stripe_status':'past_due'},'payment_failed'),
            ({'status':'suspended'},'suspended'), ({'stripe_status':'unexpected'},'unknown')]:
            with self.subTest(updates=updates):
                self.binding = {**base, **updates}
                self.assertEqual(billing.customer_appliance_status('customer-a',appliance,owner=True,now=1000)['status'], expected)
        self.binding = None; self.pending = {'status':'open'}
        self.assertEqual(billing.customer_appliance_status('customer-a',appliance,owner=True,now=1000)['status'], 'pending')
        self.pending = {'status':'open','expires_at':999}
        self.assertEqual(billing.customer_appliance_status('customer-a',appliance,owner=True,now=1000)['status'], 'not_subscribed')
        self.pending = {'status':'paid','expires_at':999}
        self.assertEqual(billing.customer_appliance_status('customer-a',appliance,owner=True,now=1000)['status'], 'pending')

    def test_checkout_button_requires_owner_flag_active_appliance_and_license(self):
        self.assertTrue(self.detail_route('a',object())['can_subscribe'])
        with patch.dict(os.environ, {'ANYAICAM_LOCAL_GATEWAY_CHECKOUT_ENABLED':'false'}):
            self.assertFalse(self.detail_route('a',object())['can_subscribe'])
        self.reversed = True; self.assertFalse(self.detail_route('a',object())['can_subscribe']); self.reversed = False
        self.license_capacity = 0; self.assertFalse(self.detail_route('a',object())['can_subscribe']); self.license_capacity = 4
        self.binding = {'status':'active','stripe_status':'active','payment_failed_at':None,'current_period_end':1,'cancel_at_period_end':False}
        self.assertEqual(self.detail_route('a',object())['status'], 'expired')
        self.assertFalse(self.detail_route('a',object())['can_subscribe'])
        self.binding = None
        self.appliances['a']['state'] = 'retired'; self.assertFalse(self.detail_route('a',object())['can_subscribe'])
        self.identity['role'] = 'customer_viewer'; self.assertFalse(self.detail_route('a',object())['can_subscribe'])
        self.identity['role'] = 'administrator'
        with self.assertRaises(FakeError): self.detail_route('a',object())

    def test_unverified_edition_is_pending_and_cannot_start_checkout(self):
        self.verified = False
        item = self.detail_route('a',object())
        self.assertFalse(item['edition_verified']); self.assertFalse(item['can_subscribe'])
        checkout = self.router.routes[('POST','/api/v2/customer/local-gateway/checkout')]
        guard = SimpleNamespace(create_session=__import__('unittest.mock',fromlist=['Mock']).Mock())
        with patch.dict(sys.modules, {'checkout_guard':guard,'main':SimpleNamespace()}):
            with self.assertRaises(FakeError) as error: checkout({'appliance_id':'a'},object())
        self.assertEqual(error.exception.status_code,409)
        guard.create_session.assert_not_called()

    def test_checkout_stays_closed_if_authorization_changes_before_handoff(self):
        checkout = self.router.routes[('POST','/api/v2/customer/local-gateway/checkout')]
        guard = SimpleNamespace(create_session=__import__('unittest.mock',fromlist=['Mock']).Mock())
        def revoked_after_check(*args):
            self.verified = False
            return True
        with patch.object(billing, 'CHECKOUT_AUTHORIZATION_ATOMICALLY_ENFORCED',False), \
             patch.object(billing, '_edition_verified',side_effect=revoked_after_check), \
             patch.dict(sys.modules, {'checkout_guard':guard}):
            self.assertFalse(self.detail_route('a',object())['can_subscribe'])
            with self.assertRaises(FakeError) as error: checkout({'appliance_id':'a'},object())
        self.assertEqual(error.exception.status_code,503)
        guard.create_session.assert_not_called()

    def test_pending_and_cloud_appliances_are_customer_scoped(self):
        self.appliances['pending'] = {'id':'pending','customer_id':'customer-a','gateway_edition':None,'activated_via':'claim','cloud_id':'PENDING'}
        self.appliances['cloud'] = {'id':'cloud','customer_id':'customer-a','gateway_edition':'cloud_gateway','cloud_id':'CLOUD'}
        self.appliances['foreign'] = {'id':'foreign','customer_id':'customer-b','gateway_edition':None,'activated_via':'claim','cloud_id':'FOREIGN'}
        self.cameras = [{'id':'camera-a','name':'Front','camera_number':1,'device_key':'sensor-a',
                         'customer_id':'customer-a','appliance_id':'cloud','status':'active'},
                        {'id':'camera-b','name':'Private','camera_number':2,'device_key':'sensor-b',
                         'customer_id':'customer-b','appliance_id':'cloud','status':'active'}]
        result = self.list_route(object())
        self.assertEqual([x['name'] for x in result['pending_appliances']],['PENDING'])
        self.assertEqual([x['name'] for x in result['cloud_appliances']],['CLOUD'])
        self.assertEqual([x['name'] for x in result['cloud_appliances'][0]['cameras']],['Front'])
        self.assertEqual(result['cloud_appliances'][0]['cameras'][0]['entitlement_status'],'unavailable')
        self.assertEqual(result['cloud_appliances'][0]['cameras'][0]['event_upload_status'],'held')
        self.verified = False
        result = self.list_route(object())
        self.assertEqual(result['cloud_appliances'],[])
        self.assertEqual({x['name'] for x in result['pending_appliances']},{'CLOUD','PENDING'})

    def test_claimed_computer_confirmation_and_ownership_changes(self):
        db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript('''
            CREATE TABLE appliances(id TEXT PRIMARY KEY,customer_id TEXT,gateway_edition TEXT,activated_via TEXT,
                activation_status TEXT,state TEXT,cloud_id TEXT,created_at TEXT,last_check_in TEXT);
            CREATE TABLE appliance_credentials(appliance_id TEXT,created_at TEXT,last_used_at TEXT,revoked_at TEXT);
            CREATE TABLE local_gateway_remote_subscriptions(appliance_id TEXT,customer_id TEXT,status TEXT,stripe_status TEXT,
                payment_failed_at TEXT,current_period_end INTEGER,cancel_at_period_end INTEGER);
            CREATE TABLE checkout_pending(customer_id TEXT,purchase TEXT,status TEXT,expires_at INTEGER);
            CREATE TABLE appliance_edition_provisioning_decisions(appliance_id TEXT,revoked_at TEXT);
            INSERT INTO appliances VALUES('claimed-a','customer-a',NULL,'claim','pending','online','AIC-A','now',NULL);
            INSERT INTO appliance_credentials VALUES('claimed-a','2026-01-01','2026-01-02',NULL);
        ''')
        @contextmanager
        def transaction():
            with db: yield db
        def row(sql,args):
            result = db.execute(sql,args).fetchone()
            return dict(result) if result else None
        def rows(sql,args): return [dict(item) for item in db.execute(sql,args).fetchall()]
        audit = __import__('unittest.mock',fromlist=['Mock']).Mock()
        grant = {'allowed':False}
        partner_db = SimpleNamespace(transaction=transaction,row=row,rows=rows,audit=audit)
        admin = SimpleNamespace(has_global_administrator_grant=lambda db,email:grant['allowed'])
        readiness_source = (Path(__file__).resolve().parents[1] / 'appliance_readiness.py').read_text(encoding='utf-8')
        readiness_tree = ast.parse(readiness_source)
        names = {'enrollment_ready','_live','appliance_activated'}
        readiness_functions = [n for n in readiness_tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
        readiness_ns = {'row':row}
        exec(compile(ast.Module(body=readiness_functions,type_ignores=[]),'appliance_readiness.py', 'exec',
                     flags=__import__('__future__').annotations.compiler_flag),readiness_ns)
        with patch.dict(sys.modules, {'partner_db':partner_db,'appliance_identity':admin,
                                      'appliance_readiness':SimpleNamespace(appliance_activated=readiness_ns['appliance_activated'])}):
            self.identity = {'role':'customer_owner','customer_id':'customer-a','email':'a@example.test'}
            self.assertEqual(self.list_route(object())['appliances'], [])
            confirm = self.router.routes[('POST','/api/operations/appliances/{appliance_id}/confirm-local-gateway')]
            with self.assertRaises(FakeError) as denied: confirm('claimed-a',{'customer_id':'customer-a'},object())
            self.assertEqual(denied.exception.status_code, 403)
            self.identity = {'role':'administrator','email':'admin@example.test'}
            with self.assertRaises(FakeError): confirm('claimed-a',{'customer_id':'customer-a'},object())
            grant['allowed'] = True
            with self.assertRaises(FakeError) as foreign: confirm('claimed-a',{'customer_id':'customer-b'},object())
            self.assertEqual(foreign.exception.status_code, 404)
            db.execute("UPDATE appliances SET gateway_edition='cloud_gateway' WHERE id='claimed-a'"); db.commit()
            with self.assertRaises(FakeError) as assigned: confirm('claimed-a',{'customer_id':'customer-a'},object())
            self.assertEqual(assigned.exception.status_code,409)
            db.execute("UPDATE appliances SET gateway_edition=NULL WHERE id='claimed-a'"); db.commit()
            with self.assertRaises(FakeError) as missing: confirm('claimed-a',{'customer_id':'customer-a'},object())
            self.assertEqual(missing.exception.status_code, 409)
            self.assertIsNone(db.execute("SELECT gateway_edition FROM appliances WHERE id='claimed-a'").fetchone()[0])
            audit.assert_not_called()
            db.execute("UPDATE appliance_credentials SET revoked_at='2026-01-04' WHERE appliance_id='claimed-a'"); db.commit()
            with self.assertRaises(FakeError) as revoked: confirm('claimed-a',{'customer_id':'customer-a'},object())
            self.assertEqual(revoked.exception.status_code, 409)
            db.execute("UPDATE appliance_credentials SET revoked_at=NULL WHERE appliance_id='claimed-a'"); db.commit()
            # Existing trusted edition: exercise the downstream lifecycle;
            # the currently unavailable evidence workflow did not assign it.
            db.execute("UPDATE appliances SET gateway_edition='local_gateway' WHERE id='claimed-a'"); db.commit()
            self.assertEqual(confirm('claimed-a',{'customer_id':'customer-a'},object())['status'], 'already_confirmed')
            self.identity = {'role':'customer_owner','customer_id':'customer-a'}
            self.assertFalse(self.list_route(object())['appliances'][0]['can_subscribe'])
            db.execute("UPDATE appliances SET last_check_in='2026-01-03' WHERE id='claimed-a'"); db.commit()
            self.assertTrue(self.list_route(object())['appliances'][0]['can_subscribe'])
            guard = SimpleNamespace(canonical_stripe_customer=lambda customer,email:'cus-synthetic',
                                    create_session=lambda *args,**kwargs:{'url':'https://checkout.example.test'})
            main = SimpleNamespace(PUBLIC_BASE_URL='https://example.test',require_stripe_price_matches_catalog=lambda *args:None)
            checkout = self.router.routes[('POST','/api/v2/customer/local-gateway/checkout')]
            request=SimpleNamespace(cookies={'anyaicam_csrf':'signed'},headers={'x-csrf-token':'signed'})
            handoff=SimpleNamespace(create_session=lambda *args,**kwargs:guard.create_session())
            with patch.dict(sys.modules, {'checkout_guard':guard,'main':main,'gateway_checkout_handoff':handoff,
                    'gateway_checkout_attempts':SimpleNamespace(ATTEMPTS_SERVICE_READY=True),
                    'token_security':SimpleNamespace(unsign=lambda token:'csrf' if token=='signed' else None)}), \
                    patch.dict(os.environ, {'ANYAICAM_STRIPE_PRICE_LOCAL_GATEWAY_REMOTE':'price-synthetic'}):
                self.assertEqual(checkout({'appliance_id':'claimed-a'},request)['status'],'complete')
                db.execute("UPDATE appliance_credentials SET revoked_at='2026-01-04' WHERE appliance_id='claimed-a'"); db.commit()
                with self.assertRaises(FakeError) as denied: checkout({'appliance_id':'claimed-a'},object())
                self.assertEqual(denied.exception.status_code,403)
                db.execute("UPDATE appliance_credentials SET revoked_at=NULL WHERE appliance_id='claimed-a'"); db.commit()
            db.execute("INSERT INTO local_gateway_remote_subscriptions VALUES('claimed-a','customer-a','active','active',NULL,4102444800,0)"); db.commit()
            self.assertEqual(self.list_route(object())['appliances'][0]['status'], 'active')
            # A Gateway without a decision fails closed even if billing is paid.
            self.assertFalse(billing.remote_access_for_appliance('customer-a','claimed-a'))
            db.execute("UPDATE appliance_credentials SET revoked_at='2026-01-04' WHERE appliance_id='claimed-a'"); db.commit()
            self.assertFalse(billing.remote_access_for_appliance('customer-a','claimed-a'))
            self.assertFalse(self.detail_route('claimed-a',object())['appliance_ready'])
            db.execute("UPDATE appliances SET customer_id='customer-b' WHERE id='claimed-a'"); db.commit()
            self.assertEqual(self.list_route(object())['appliances'], [])
            with self.assertRaises(FakeError): self.detail_route('claimed-a',object())
            self.identity = {'role':'customer_owner','customer_id':'customer-b'}
            item = self.detail_route('claimed-a',object())
            self.assertEqual(item['status'], 'not_subscribed')

    def test_panel_is_embedded_in_existing_subscription_page(self):
        html, script = ui.panel_html(), ui.panel_script()
        self.assertIn('$9.99/month per appliance', html)
        self.assertIn('perpetual local VMS software license', html)
        self.assertIn('Local viewing, recording', html)
        self.assertIn('$9.99/month per camera', html)
        self.assertIn('Advanced analytics require separate authorization', html)
        self.assertIn('camera permission and opt-in', html)
        self.assertIn('/api/v2/customer/local-gateway/checkout', script)
        self.assertIn('/api/customer/billing-portal', script)
        self.assertIn('textContent', script)
        source = (Path(__file__).resolve().parents[1] / 'per_camera_billing.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        page = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'customer_portal_page')
        segment = ast.get_source_segment(source,page)
        self.assertIn('local_gateway_customer_ui.panel_html()', segment)
        self.assertIn('local_gateway_customer_ui.panel_script()', segment)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the small mocked UI interaction')
    def test_pending_and_cloud_panel_render_without_interpreting_names_as_html(self):
        script = ui.panel_script().replace('<script>', '', 1).rsplit('</script>', 1)[0]
        prelude = '''
const target={children:[],appendChild(x){this.children.push(x)}}, panel={hidden:true}, cloud={children:[],appendChild(x){this.children.push(x)}}, msg={};
const nodes={'local-gateway-appliances':target,'local-gateway-message':msg,'cloud-gateway-subscription':panel,'cloud-gateway-appliances':cloud};
const document={getElementById:id=>nodes[id],createElement:tag=>({children:[],appendChild(x){this.children.push(x)}})};
async function fetch(){return {ok:true,json:async()=>({appliances:[],pending_appliances:[{name:'<pending>'}],cloud_appliances:[{name:'<cloud>'}]})};}
'''
        check = '''
setImmediate(()=>{try{if(panel.hidden||cloud.children[0].children[0].textContent!=='<cloud>')throw Error('cloud');
if(target.children[0].textContent!=='<pending> — Edition pending verification. Contact your administrator.')throw Error('pending');}
catch(e){console.error(e);process.exitCode=1;}});
'''
        result = subprocess.run([shutil.which('node'),'-e',prelude+script+check],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the small mocked UI interaction')
    def test_panel_actions_use_existing_customer_endpoints(self):
        script = ui.panel_script().replace('<script>', '', 1).rsplit('</script>', 1)[0]
        for action in ('subscribe', 'manage'):
            with self.subTest(action=action):
                prelude = '''
const calls=[]; const target={children:[],appendChild(x){this.children.push(x)}}; const msg={};
const document={getElementById:id=>id==='local-gateway-appliances'?target:msg,
 createElement:tag=>({children:[],appendChild(x){this.children.push(x)}})};
const location={href:''};
const item={appliance_id:'a',name:'<synthetic>',status:'active',local_license_active:true,appliance_ready:true,edition_verified:true,
 can_subscribe:ACTION==='subscribe',can_manage:ACTION==='manage'};
async function fetch(url,options){calls.push([url,options]);return {ok:true,json:async()=>url.endsWith('/appliances')?
 {appliances:[item]}:(ACTION==='subscribe'?{checkout_url:'https://checkout.example.test'}:{url:'https://billing.example.test'})};}
'''.replace('ACTION', repr(action))
                check = '''
setImmediate(async()=>{try{const row=target.children[0];if(!row||row.children[0].textContent!=='<synthetic> — Billing: Active · Local VMS license: active')throw Error('render');
const button=row.children[1];await button.onclick();const call=calls[1];
if(ACTION==='subscribe'){if(call[0]!=='/api/v2/customer/local-gateway/checkout'||JSON.parse(call[1].body).appliance_id!=='a')throw Error('checkout');}
else if(call[0]!=='/api/customer/billing-portal')throw Error('portal');
if(!location.href.startsWith('https://'))throw Error('redirect');}catch(e){console.error(e);process.exitCode=1;}});
'''.replace('ACTION', repr(action))
                result = subprocess.run([shutil.which('node'), '-e', prelude + script + check], capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
