import pathlib,json,sqlite3,time,requests
base='https://installer-test.test'; w=pathlib.Path('/home/ubuntu/anyaicam-lab')
s=requests.Session();s.verify=str(w/'lab-ca.crt')
s.get(base+'/customer-login.html',timeout=15)
csrf=s.cookies.get('anyaicam_csrf','').strip('"')
s.headers.update({'Origin':base,'X-CSRF-Token':csrf})
creds={}
for line in pathlib.Path('/var/lib/anyaicam-installer-lab/owner-login.txt').read_text().splitlines():
 if ': ' in line:
  k,v=line.split(': ',1);creds[k]=v
r=s.post(base+'/api/partner-login',json={'email':creds['Email'],'password':creds['Password'],'customer_only':True},timeout=15,allow_redirects=False);creds.clear()
assert r.status_code==303,'Owner login failed'
c=sqlite3.connect('file:/var/lib/anyaicam-installer-lab/portal.db?mode=ro',uri=True)
a=c.execute("select id from appliances where customer_id='installer-test-customer' and site_id='installer-test-site'").fetchall();assert len(a)==1
aid=a[0][0]
r=s.post(base+'/api/customer/appliances/'+aid+'/scan',json={},timeout=15);assert r.status_code==200,'Scan request failed'
job=r.json()['job_id'];print('OWNER_SCAN_REQUEST_ACCEPTED',flush=True)
for n in range(24):
 r=s.get(base+'/api/customer/camera-scans/'+job,timeout=15);assert r.status_code==200
 d=r.json()
 if d['status'] in ['complete','error','timed_out','cancelled']: break
 time.sleep(3)
(w/'samsung-owner-scan-results.json').write_text(json.dumps(d,indent=2))
print('OWNER_SCAN_STATUS='+d['status'],flush=True)
results=d.get('results',[])
print('DISCOVERED_COUNT='+str(len(results)),flush=True)
assert d['status']=='complete','Scan has not completed'
assert len(results)==1,'Expected only the synthetic camera'
key=results[0]['device_key'];assert key=='urn:uuid:414e5941-4943-414d-8123-123456789abc', 'Wrong camera identity'
r=s.post(base+'/api/customer/cameras/provision',json={'appliance_id':aid,'device_key':key,'site_id':'installer-test-site','name':'Synthetic Installer Test Camera','recording_mode':'continuous','analytics':[]},timeout=15)
print('OWNER_PROVISION_REQUEST_HTTP='+str(r.status_code),flush=True)
if r.status_code!=200: print('PROVISION_REFUSAL='+str(r.json().get('detail',''))); raise SystemExit(1)
(w/'samsung-owner-provision-request.json').write_text(json.dumps(r.json(),indent=2))
print('OWNER_PROVISION_REQUEST_ACCEPTED',flush=True)
