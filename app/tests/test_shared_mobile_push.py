"""Shared push tests: real database/fanout, mocked provider, no live delivery."""
from datetime import timedelta
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_notification_external_delivery import _isolated_db, _seed, _set_preferences, _appliance, fake_channels
from partner_db import connection
import notification_engine as engine
import mobile_push as push
import mobile_push_provider as provider
import mobile_push_routes as routes


def device(db, uid='owner-cust-1', cid='cust-1', did='device-1', token=None):
    db.execute('INSERT INTO mobile_push_devices(id,user_id,customer_id,installation_id,platform,token,enabled,updated_at) VALUES(?,?,?,?,?,?,1,?)',
               (did,uid,cid,'installation-'+did,'android',token or 'token-for-testing-'+did,push.utcnow().isoformat()))


def seed(*, events=None, quiet=False, email=False, devices=1):
    with connection() as db:
        uid = _seed(db)
        _set_preferences(db,user_id=uid,customer_id='cust-1',event_types=events or ['person'],quiet_hours_enabled=quiet,quiet_start='00:00',quiet_end='23:59',email_enabled=email,email_address='owner@example.test' if email else '')
        for i in range(devices):
            device(db,did=f'device-{i}')
    return uid


def fanout(kind='person', eid='event-1', camera='cam-1'):
    return engine.fanout_appliance_event(_appliance(),{'id':eid,'event_type':kind,'camera_id':camera})


def jobs():
    with connection() as db:
        return [dict(r) for r in db.execute('SELECT * FROM mobile_push_outbox ORDER BY created_at,id').fetchall()]


@pytest.mark.parametrize('kind', sorted(engine.SUPPORTED))
def test_every_shared_event_routes_when_selected(kind, fake_channels):
    seed(events=[kind])
    fanout(kind)
    assert len(jobs()) == 1


def test_no_enrollment_no_push(fake_channels):
    seed(devices=0)
    fanout()
    assert jobs() == []


def test_quiet_hours_and_selection_are_respected(fake_channels):
    seed(quiet=True)
    fanout()
    fanout('vehicle','event-2')
    assert jobs() == []


def test_intrusion_bypasses_quiet_selection_and_cooldown_but_deduplicates(fake_channels):
    seed(quiet=True)
    fanout('intrusion_alarm','alarm-1')
    fanout('intrusion_alarm','alarm-2')
    fanout('intrusion_alarm','alarm-1')
    assert len(jobs()) == 2


def test_push_cooldown_does_not_suppress_email(fake_channels):
    seed()
    fanout(eid='first')
    with connection() as db:
        db.execute("UPDATE customer_notification_channels SET email_enabled=1,email_address='owner@example.test'")
    fanout(eid='second')
    assert len(jobs()) == 1
    with connection() as db:
        assert db.execute("SELECT count(*) FROM notification_deliveries WHERE channel='email'").fetchone()[0] == 1


def test_email_cooldown_does_not_suppress_push(fake_channels):
    seed(email=True, devices=0)
    fanout(eid='first')
    with connection() as db:
        device(db)
    fanout(eid='second')
    assert len(jobs()) == 1


def test_multiple_devices_partial_failure_and_no_resend_success(monkeypatch,fake_channels):
    seed(devices=2)
    fanout('intrusion_alarm')
    calls=[]
    def send(d,n,**kw):
        calls.append(d['id'])
        return {'status':'sent' if d['id']=='device-0' else 'retry'}
    monkeypatch.setattr(provider,'send',send)
    now=push.utcnow()
    assert push.drain(now=now)['sent']==1
    push.drain(now=now+timedelta(seconds=130))
    assert calls.count('device-0')==1
    assert calls.count('device-1')==2


def test_revoked_account_never_sends_queued_alert(monkeypatch,fake_channels):
    seed();fanout()
    with connection() as db:
        db.execute("UPDATE partner_users SET account_status='disabled'")
    monkeypatch.setattr(provider,'send',lambda *a,**k: pytest.fail('provider must not run'))
    push.drain()
    assert jobs()[0]['status']=='skipped'


def test_current_camera_scope_rechecked(monkeypatch,fake_channels):
    seed();fanout()
    with connection() as db:
        db.execute("UPDATE customer_notification_channels SET camera_scope='selected'")
    monkeypatch.setattr(provider,'send',lambda *a,**k: pytest.fail('provider must not run'))
    push.drain()
    assert jobs()[0]['status']=='skipped'


def test_expired_visitor_call_never_delivered(monkeypatch,fake_channels):
    seed(events=['aac_voice_call']);fanout('aac_voice_call')
    monkeypatch.setattr(provider,'send',lambda *a,**k: pytest.fail('expired'))
    push.drain(now=push.utcnow()+timedelta(seconds=121))
    assert jobs()[0]['status']=='expired'


def test_unregistered_token_disabled(monkeypatch,fake_channels):
    seed();fanout()
    monkeypatch.setattr(provider,'send',lambda *a,**k: {'status':'invalid_token'})
    push.drain()
    with connection() as db:
        assert db.execute('SELECT enabled FROM mobile_push_devices').fetchone()[0]==0


def test_missing_credentials_honest_and_no_network(monkeypatch,fake_channels):
    monkeypatch.delenv('ANYAICAM_MOBILE_PUSH_BACKEND',raising=False)
    seed();fanout()
    push.drain()
    assert jobs()[0]['status']=='unavailable'
    assert jobs()[0]['error']=='fcm_not_configured'


def test_live_lease_skipped_and_expired_lease_recovered(monkeypatch,fake_channels):
    seed();fanout()
    now=push.utcnow()
    with connection() as db:
        db.execute("UPDATE mobile_push_outbox SET status='sending',lease_until=?",((now+timedelta(seconds=10)).isoformat(),))
    monkeypatch.setattr(provider,'send',lambda *a,**k: {'status':'sent'})
    assert push.drain(now=now)['attempted']==0
    assert push.drain(now=now+timedelta(seconds=11))['sent']==1


@pytest.fixture
def client(monkeypatch):
    seed(devices=0)
    monkeypatch.setattr(routes,'_resolve_customer_identity',lambda req: {'email':'owner-cust-1@example.test','customer_id':'cust-1','role':'customer_owner'})
    app=FastAPI();routes.register_routes(app)
    return TestClient(app)


def enrollment(token='test-fcm-token-not-a-real-secret'):
    return {'installation_id':'install-1234567890','platform':'ios','token':token}


def test_device_refresh_list_and_revoke(client):
    first=client.put('/api/mobile/push/devices',json=enrollment())
    assert first.status_code==200
    did=first.json()['device']['id']
    refreshed=client.put('/api/mobile/push/devices',json=enrollment('a-new-test-fcm-token-for-refresh'))
    assert refreshed.json()['device']['id']==did
    assert 'token' not in client.get('/api/mobile/push/devices').text
    assert client.delete('/api/mobile/push/devices/'+did).status_code==200
    assert client.get('/api/mobile/push/devices').json()['devices']==[]


def test_unauthenticated_registration_rejected(client,monkeypatch):
    monkeypatch.setattr(routes,'_resolve_customer_identity',lambda req: None)
    assert client.put('/api/mobile/push/devices',json=enrollment()).status_code==401


def test_cross_customer_token_and_revoke_rejected(client):
    with connection() as db:
        uid=_seed(db,customer_id='other',camera_id='other-cam')
        device(db,uid=uid,cid='other',did='other-device',token=enrollment()['token'])
    assert client.put('/api/mobile/push/devices',json=enrollment()).status_code==409
    assert client.delete('/api/mobile/push/devices/other-device').status_code==404
    assert client.get('/api/mobile/push/devices').json()['devices']==[]


def test_notification_tap_scoped_to_owner(client,fake_channels):
    fanout('intrusion_alarm')
    with connection() as db:
        nid=db.execute('SELECT id FROM notifications').fetchone()[0]
    result=client.get('/api/mobile/push/notifications/'+nid)
    assert result.status_code==200
    assert result.json()['path'].startswith('/customer/cameras/cam-1/live')
    assert client.get('/api/mobile/push/notifications/not-owned').status_code==404


@pytest.mark.parametrize('kind,priority,title',[('intrusion_alarm','high','INTRUSION ALARM'),('aac_voice_call','high','Visitor Call'),('person','normal','AnyAiCam activity')])
def test_provider_payload_and_no_private_message(monkeypatch,kind,priority,title):
    class Payload:
        def __init__(self,**kw): self.__dict__.update(kw)
    captured=[]
    messaging=SimpleNamespace(**{key:Payload for key in ['Message','Notification','AndroidConfig','AndroidNotification','APNSConfig','APNSPayload','Aps','WebpushConfig','WebpushNotification']})
    messaging.send=lambda msg,app: captured.append(msg) or 'provider-id'
    sdk=SimpleNamespace(messaging=messaging,get_app=lambda name: 'app')
    monkeypatch.setitem(sys.modules,'firebase_admin',sdk)
    monkeypatch.setenv('ANYAICAM_MOBILE_PUSH_BACKEND','fcm');monkeypatch.setenv('ANYAICAM_FCM_PROJECT_ID','test-project')
    result=provider.send({'token':'opaque'},{'id':'nid','event_type':kind,'message':'private transcript'},ttl_seconds=120)
    assert result['status']=='sent'
    assert captured[0].android.priority==priority
    assert captured[0].notification.title==title
    assert captured[0].data=={'notification_id':'nid','event_type':kind}
    assert captured[0].apns.headers['apns-push-type']=='alert'
    assert 'private' not in captured[0].notification.body


def test_web_configuration_allowlist_and_project_match(monkeypatch):
    import json
    monkeypatch.setenv('ANYAICAM_MOBILE_PUSH_BACKEND','fcm')
    monkeypatch.setenv('ANYAICAM_FCM_PROJECT_ID','test-project')
    monkeypatch.setenv('ANYAICAM_FIREBASE_WEB_VAPID_PUBLIC_KEY','public-test-key')
    config={'apiKey':'public-api-key','appId':'web-app-id','messagingSenderId':'12345','projectId':'test-project','private_key':'MUST NEVER LEAVE SERVER'}
    monkeypatch.setenv('ANYAICAM_FIREBASE_WEB_CONFIG_JSON',json.dumps(config))
    response=provider.web_configuration()
    assert response['available']
    assert 'private_key' not in response['firebase']
    monkeypatch.setenv('ANYAICAM_FCM_PROJECT_ID','different-project')
    assert provider.web_configuration()=={'available':False,'firebase':{},'vapid_public_key':''}


def test_disabled_device_never_receives_emergency(monkeypatch,fake_channels):
    seed()
    with connection() as db: db.execute('UPDATE mobile_push_devices SET enabled=0')
    fanout('intrusion_alarm')
    assert jobs()==[]


def test_viewer_camera_permission_revocation_rechecked(monkeypatch,fake_channels):
    seed();fanout()
    with connection() as db:
        db.execute("UPDATE partner_users SET role='customer_viewer',camera_access_mode='selected'")
    monkeypatch.setattr(provider,'send',lambda *a,**k: pytest.fail('revoked camera'))
    push.drain()
    assert jobs()[0]['status']=='skipped'


def test_new_camera_included_without_resaving_preferences(fake_channels):
    seed()
    with connection() as db:
        db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES('new-camera','cust-1','site-cust-1','appl-cust-1','New','2026-09-29')")
    fanout(camera='new-camera')
    assert len(jobs())==1


def test_token_refresh_inflight_not_disabled(monkeypatch,fake_channels):
    seed();fanout()
    def send(d,n,**kw):
        with connection() as db:
            db.execute('UPDATE mobile_push_devices SET token=? WHERE id=?',('refreshed-token',d['id']))
        return {'status':'invalid_token'}
    monkeypatch.setattr(provider,'send',send)
    push.drain()
    with connection() as db:
        assert db.execute('SELECT enabled FROM mobile_push_devices').fetchone()[0]==1


def test_second_worker_cannot_claim_inflight_delivery(monkeypatch,fake_channels):
    seed();fanout()
    def send(*a,**kw):
        assert push.drain()['attempted']==0
        return {'status':'sent'}
    monkeypatch.setattr(provider,'send',send)
    assert push.drain()['sent']==1


def test_failure_retries_bounded(monkeypatch,fake_channels):
    seed();fanout()
    monkeypatch.setattr(provider,'send',lambda *a,**kw: {'status':'retry'})
    for _ in range(6):
        push.drain()
        with connection() as db:
            db.execute('UPDATE mobile_push_outbox SET next_at=?',(push.utcnow().isoformat(),))
    assert jobs()[0]['status']=='failed'
    assert jobs()[0]['attempt']==5


def test_unregistered_error_only_invalidates_specific_token(monkeypatch):
    class UnregisteredError(Exception): pass
    messaging=SimpleNamespace()
    class Payload:
        def __init__(self,**kw): pass
    for key in ['Message','Notification','AndroidConfig','AndroidNotification','APNSConfig','APNSPayload','Aps','WebpushConfig','WebpushNotification']:
        setattr(messaging,key,Payload)
    def send(*a,**kw): raise UnregisteredError('do not persist credential-like data')
    messaging.send=send
    monkeypatch.setitem(sys.modules,'firebase_admin',SimpleNamespace(messaging=messaging,get_app=lambda name:'app'))
    monkeypatch.setenv('ANYAICAM_MOBILE_PUSH_BACKEND','fcm');monkeypatch.setenv('ANYAICAM_FCM_PROJECT_ID','test')
    assert provider.send({'token':'opaque'},{'id':'nid','event_type':'person'},ttl_seconds=60)=={'status':'invalid_token','error':'unregistered'}


def test_web_platform_and_public_unconfigured_routes(client,monkeypatch):
    data=enrollment();data['platform']='web'
    assert client.put('/api/mobile/push/devices',json=data).status_code==200
    monkeypatch.delenv('ANYAICAM_MOBILE_PUSH_BACKEND',raising=False)
    assert client.get('/api/mobile/push/config').json()['available'] is False
    assert client.get('/api/mobile/push/firebase-config.js').status_code==503
    assert client.get('/mobile-push-sw.js').status_code==200


def test_push_scripts_parse_and_safe_click_destination():
    import subprocess
    import shutil
    from pathlib import Path
    node=shutil.which('node')
    if not node: pytest.skip('Node is not installed')
    static=Path(__file__).parents[1]/'static'
    for script in ['mobile-push.js','mobile-push-sw.js']:
        subprocess.run([node,'--check',str(static/script)],check=True,capture_output=True)
    harness=r'''
    const vm=require('vm'),fs=require('fs'),assert=require('assert');
    let callback,opened=[];
    const self={location:{origin:'https://portal.example'},addEventListener:(name,fn)=>{if(name==='notificationclick')callback=fn}};
    const ctx={self,URL,importScripts:()=>{},firebase:{initializeApp:()=>{},messaging:()=>{}},
      fetch:async()=>({ok:true,json:async()=>({path:'https://evil.example/'})}),
      clients:{matchAll:async()=>[],openWindow:url=>opened.push(url)}};
    vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
    let wait;
    callback({stopImmediatePropagation(){},notification:{close(){},data:{notification_id:'nid'}},waitUntil:p=>wait=p});
    wait.then(()=>{assert.equal(opened.length,0)}).catch(e=>{console.error(e);process.exit(1)});
    '''
    subprocess.run([node,'-e',harness,str(static/'mobile-push-sw.js')],check=True,capture_output=True)


def test_push_csp_only_allows_firebase_on_push_pages():
    from fastapi import Request
    from cloud_security import _connect_src_csp
    def request(path): return Request({'type':'http','path':path,'headers':[],'scheme':'https','server':('portal.example',443),'query_string':b''})
    assert 'firebaseinstallations.googleapis.com' in _connect_src_csp(request('/settings/notifications'))
    assert 'fcmregistrations.googleapis.com' in _connect_src_csp(request('/mobile-push-sw.js'))
    assert 'firebase' not in _connect_src_csp(request('/dashboard'))


def test_real_sdk_serializes_payload_without_network(monkeypatch):
    import json
    firebase_admin=pytest.importorskip('firebase_admin')
    from firebase_admin import messaging
    monkeypatch.setenv('ANYAICAM_MOBILE_PUSH_BACKEND','fcm')
    monkeypatch.setenv('ANYAICAM_FCM_PROJECT_ID','unit-test')
    monkeypatch.setattr(firebase_admin,'get_app',lambda name: object())
    encoded=[]
    def capture(message,app):
        encoded.append(json.loads(messaging._MessagingService.JSON_ENCODER.encode(message)))
        return 'mock-provider-id'
    monkeypatch.setattr(messaging,'send',capture)
    for kind in ['intrusion_alarm','aac_voice_call','person','lpr','camera_offline']:
        assert provider.send({'token':'unit-test-token'},{'id':'nid','event_type':kind},ttl_seconds=60)['status']=='sent'
    assert len(encoded)==5
    assert encoded[0]['webpush']['headers']['Urgency']=='high'
    assert encoded[0]['apns']['headers']['apns-priority']=='10'
    assert encoded[2]['android']['priority']=='normal'


def test_service_worker_displays_foreground_and_background_once():
    import subprocess, shutil
    from pathlib import Path
    node=shutil.which('node')
    if not node: pytest.skip('Node is not installed')
    worker=Path(__file__).parents[1]/'static'/'mobile-push-sw.js'
    harness=r"""
    const vm=require('vm'),fs=require('fs'),assert=require('assert');
    let callbacks={},shown=[],stopped=0,wait;
    const self={location:{origin:'https://portal.example'},addEventListener:(name,fn)=>callbacks[name]=fn,
      registration:{showNotification:async(title,opts)=>shown.push({title,opts})}};
    vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{self,URL,importScripts:()=>{},firebase:{initializeApp:()=>{},messaging:()=>{}}});
    callbacks.push({stopImmediatePropagation(){stopped++},data:{json:()=>({data:{notification_id:'unique-id',event_type:'intrusion_alarm'},notification:{body:'private data'}})},waitUntil:p=>wait=p});
    wait.then(()=>{assert.equal(stopped,1);assert.equal(shown.length,1);assert.equal(shown[0].title,'INTRUSION ALARM');assert.equal(shown[0].opts.tag,'unique-id');assert(!shown[0].opts.body.includes('private'))}).catch(e=>{console.error(e);process.exit(1)});
    """
    subprocess.run([node,'-e',harness,str(worker)],check=True,capture_output=True)


def test_urgent_lane_independent_of_activity(monkeypatch,fake_channels):
    seed(events=['person','aac_voice_call'])
    fanout('person','activity');fanout('intrusion_alarm','alarm');fanout('aac_voice_call','call')
    sent=[]
    monkeypatch.setattr(provider,'send',lambda d,n,**kw: sent.append(n['event_type']) or {'status':'sent'})
    assert push.drain(urgent_only=True)['sent']==2
    assert sent==['intrusion_alarm','aac_voice_call']
    assert push.drain(urgent_only=False)['sent']==1
    assert sent[-1]=='person'


def test_urgent_retry_backoff_and_quota_minimum(monkeypatch,fake_channels):
    seed();fanout('intrusion_alarm')
    now=push.utcnow()
    monkeypatch.setattr(provider,'send',lambda *a,**k: {'status':'retry','error':'provider_transient'})
    push.drain(now=now)
    from datetime import datetime
    assert datetime.fromisoformat(jobs()[0]['next_at'])==now+timedelta(seconds=5)
    monkeypatch.setattr(provider,'send',lambda *a,**k: {'status':'retry','error':'provider_throttled'})
    push.drain(now=now+timedelta(seconds=5))
    assert datetime.fromisoformat(jobs()[0]['next_at'])==now+timedelta(seconds=65)


def test_worker_activation_finishing_before_listener_does_not_timeout():
    import subprocess, shutil
    from pathlib import Path
    node=shutil.which('node')
    if not node: pytest.skip('Node is not installed')
    script=Path(__file__).parents[1]/'static'/'mobile-push.js'
    harness=r"""
    const vm=require('vm'),fs=require('fs'),assert=require('assert');
    const status={},devices={replaceChildren(){}};
    const ctx=vm.createContext({document:{getElementById:id=>id==='vms-push-status'?status:id==='vms-push-devices'?devices:null},
      navigator:{serviceWorker:{register:async()=>({active:null,installing:{state:'activated',addEventListener(){}}})}},
      localStorage:{getItem:()=> 'stable-installation',setItem(){}},setTimeout,clearTimeout,
      fetch:async()=>({ok:true,json:async()=>({devices:[]})})});
    vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
    vm.runInContext("firebaseModules=Promise.resolve([{getApps:()=>[],initializeApp:()=>({})},{isSupported:async()=>true,getMessaging:()=>({}),getToken:async()=> 'fake-token'}])",ctx);
    Promise.race([vm.runInContext("enroll({firebase:{},vapid_public_key:'fake'})",ctx),new Promise((_,reject)=>setTimeout(()=>reject(new Error('activation race timed out')),500))]).then(()=>{assert(status.textContent.startsWith('Browser enrolled.'))}).catch(e=>{console.error(e);process.exit(1)});
    """
    subprocess.run([node,'-e',harness,str(script)],check=True,capture_output=True)


def test_full_app_only_public_push_assets_bypass_login(monkeypatch):
    import main
    monkeypatch.delenv('ANYAICAM_MOBILE_PUSH_BACKEND',raising=False)
    client=TestClient(main.app,follow_redirects=False)
    worker=client.get('/mobile-push-sw.js')
    assert worker.status_code==200
    assert 'https://www.gstatic.com/firebasejs/12.0.0/' in worker.headers['content-security-policy']
    assert client.get('/api/mobile/push/config').status_code==200
    assert client.get('/api/mobile/push/firebase-config.js').status_code==503
    for path in ['/api/mobile/push/devices','/api/mobile/push/deliveries','/api/mobile/push/notifications/anything','/api/mobile/push/config-private']:
        assert client.get(path).status_code in (401,403)


def test_full_app_enrollment_uses_real_customer_session_and_csrf():
    import main,partner_portal
    from token_security import sign
    seed(devices=0)
    client=TestClient(main.app,follow_redirects=False)
    cookie=partner_portal._token('owner-cust-1@example.test','customer_owner',None,'cust-1',None)
    csrf=sign('csrf',28800)
    client.cookies.set(partner_portal.SESSION_COOKIE,cookie)
    client.cookies.set('anyaicam_csrf',csrf)
    result=client.put('/api/mobile/push/devices',json=enrollment(),headers={'X-CSRF-Token':csrf})
    assert result.status_code==200,result.text
    listed=client.get('/api/mobile/push/devices')
    assert len(listed.json()['devices'])==1
    assert 'token' not in listed.text


def test_customer_role_rechecked_at_enrollment(client):
    with connection() as db:
        db.execute("UPDATE partner_users SET role='technician'")
    assert client.put('/api/mobile/push/devices',json=enrollment()).status_code==403


@pytest.mark.parametrize('kind',['person','lpr'])
def test_push_tap_preserves_available_event_clip(client,fake_channels,kind):
    from urllib.parse import urlsplit,parse_qs
    fanout(kind,eid='clip-event')
    with connection() as db:
        db.execute('INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
            ('clip-event','cust-1','site-cust-1','appl-cust-1','cam-1','local-clip',kind,'2026-09-29T12:00:00','2026-09-29T12:00:00'))
        db.execute('INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,started_at,ended_at,created_at) VALUES(?,?,?,?,?,?,?,?)',
            ('media-1','clip-event','cust-1','cam-1','test/clip.mp4','2026-09-29T12:00:00','2026-09-29T12:00:10','2026-09-29T12:00:00'))
        nid=db.execute('SELECT id FROM notifications').fetchone()[0]
    response=client.get('/api/mobile/push/notifications/'+nid)
    assert response.status_code==200
    parsed=urlsplit(response.json()['path'])
    assert parsed.path=='/playback'
    assert parse_qs(parsed.query)['event']==['clip-event']
    assert parse_qs(parsed.query)['camera']==['cam-1']


# ---------------------------------------------------------------- 2026-09-30 staging-readiness fixes

@pytest.mark.parametrize('kind', ['person', 'intrusion_alarm'])
def test_push_enqueue_failure_never_costs_in_app_email_or_sms(monkeypatch, fake_channels, kind):
    """A push error used to roll back the whole notification transaction."""
    seed(events=[kind], email=True)
    def broken(db, notification_id):
        db.execute("INSERT INTO mobile_push_outbox(id,notification_id,device_id,event_key,status,next_at,expires_at,created_at) VALUES('partial',?,'device-0','k','pending','','','')", (notification_id,))
        raise RuntimeError('push table problem')
    monkeypatch.setattr(push, 'enqueue', broken)
    fanout(kind, 'evt-broken-push')
    with connection() as db:
        stored = db.execute("SELECT id FROM notifications WHERE event_id='evt-broken-push'").fetchall()
    assert len(stored) == 1, 'the in-app notification must survive a push failure'
    with connection() as db:
        channels = {r['channel'] for r in db.execute('SELECT channel FROM notification_deliveries WHERE notification_id=?', (stored[0]['id'],)).fetchall()}
    # Email is recorded exactly as without push: sent now, or held for the event
    # picture (existing media-waiting behaviour for person events).
    assert {'in_app', 'email'} <= channels, channels
    if kind == 'intrusion_alarm':
        assert len(fake_channels['email'].calls) == 1, 'an alarm email is sent immediately'
    assert jobs() == [], 'partial push rows are rolled back with the savepoint'


def test_notification_tap_opens_a_window_when_an_open_tab_refuses_navigation():
    """Portal tabs are not controlled by the /mobile-push/ worker, so browsers
    reject navigate(); the tap must still open the event."""
    import subprocess
    import shutil
    from pathlib import Path
    node = shutil.which('node')
    if not node: pytest.skip('Node is not installed')
    harness = r'''
    const vm=require('vm'),fs=require('fs'),assert=require('assert');
    async function run(navigateImpl){
      let callback,opened=[],focused=0;
      const tab={url:'https://portal.example/dashboard',navigate:navigateImpl,focus:async()=>{focused++}};
      const self={location:{origin:'https://portal.example'},addEventListener:(n,fn)=>{if(n==='notificationclick')callback=fn}};
      const ctx={self,URL,importScripts:()=>{},firebase:{initializeApp:()=>{},messaging:()=>{}},
        fetch:async()=>({ok:true,json:async()=>({path:'/events?event=e1'})}),
        clients:{matchAll:async()=>[tab],openWindow:async url=>{opened.push(url)}}};
      vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
      let wait; callback({stopImmediatePropagation(){},notification:{close(){},data:{notification_id:'nid'}},waitUntil:p=>wait=p});
      await wait; return {opened,focused};
    }
    (async()=>{
      const refused=await run(async()=>{throw new TypeError('not controlled')});
      assert.deepEqual(refused.opened,['https://portal.example/events?event=e1']);
      const allowed=await run(async function(){return {focus:async()=>{}}});
      assert.deepEqual(allowed.opened,[]);
    })().catch(e=>{console.error(e);process.exit(1)});
    '''
    static = Path(__file__).parents[1]/'static'
    done = subprocess.run([node,'-e',harness,str(static/'mobile-push-sw.js')],capture_output=True,text=True)
    assert done.returncode == 0, done.stderr


def test_firebase_sdk_is_installed_only_for_opted_in_cloud_images():
    from pathlib import Path
    root = Path(__file__).parents[2]
    dockerfile = (root / 'Dockerfile').read_text(encoding='utf-8')
    assert 'ARG ANYAICAM_INSTALL_PUSH=0' in dockerfile
    # Wildcard: the appliance installer payload ships no requirements-push.txt.
    assert 'COPY requirements*.txt /tmp/push-requirements/' in dockerfile
    assert 'if [ "$ANYAICAM_INSTALL_PUSH" = "1" ]' in dockerfile
    assert 'firebase-admin==7.7.0' in (root / 'requirements-push.txt').read_text(encoding='utf-8')
