import requests,pathlib,sqlite3,json
base='https://installer-test.test';w=pathlib.Path('/home/ubuntu/anyaicam-lab');s=requests.Session();s.verify=str(w/'lab-ca.crt')
results=[]
def check(name,r,status):
 ok=r.status_code in status;results.append({'name':name,'passed':ok,'http':r.status_code});print(name+'='+str(r.status_code)+' '+('PASS' if ok else 'FAIL'),flush=True);assert ok
r=s.get(base+'/api/customer/cameras',timeout=10);check('Unsigned camera list refused',r,[401,403])
s.get(base+'/customer-login.html',timeout=10);csrf=s.cookies.get('anyaicam_csrf','').strip('"')
creds={}
for line in pathlib.Path('/var/lib/anyaicam-installer-lab/owner-login.txt').read_text().splitlines():
 if ': ' in line:k,v=line.split(': ',1);creds[k]=v
r=s.post(base+'/api/partner-login',json={'email':creds['Email'],'password':creds['Password'],'customer_only':True},headers={'Origin':base,'X-CSRF-Token':csrf},timeout=10,allow_redirects=False);creds.clear();assert r.status_code==303
c=sqlite3.connect('file:/var/lib/anyaicam-installer-lab/portal.db?mode=ro',uri=True);aid=c.execute('select id from appliances').fetchone()[0]
url=base+'/api/customer/appliances/'+aid+'/scan'
r=s.post(url,json={},headers={'Origin':base},timeout=10);check('Authenticated scan without CSRF refused',r,[403])
r=s.post(url,json={},headers={'Origin':'https://wrong-origin.example.test','X-CSRF-Token':csrf},timeout=10);check('Authenticated scan wrong Origin refused',r,[403])
r=s.post(url,json={},headers={'Origin':base,'X-CSRF-Token':'intentionally-invalid'},timeout=10);check('Authenticated scan invalid CSRF refused',r,[403])
s.headers.update({'Origin':base,'X-CSRF-Token':csrf})
r=s.post(base+'/api/customer/appliances/not-owned-test-id/scan',json={},timeout=10);check('Unowned appliance scan refused',r,[404])
r=s.post(base+'/api/customer/cameras/provision',json={'appliance_id':aid,'device_key':'urn:uuid:414e5941-4943-414d-8123-123456789abc','site_id':'not-owned-test-site','name':'Must not create'},timeout=10);check('Unowned location provisioning refused',r,[404])
r=s.post(base+'/api/portal/claims/lookup',json={'claim_code':'INVALID0'},timeout=10);check('Invalid temporary code refused',r,[404])
r=s.post(base+'/api/portal/claims/confirm',json={'claim_code':'INVALID0','site_id':'installer-test-site'},timeout=10);check('Invalid claim confirmation refused',r,[404])
(w/'samsung-owner-security-results.json').write_text(json.dumps(results,indent=2));print('SECURITY_TOTAL_PASS='+str(len(results)))
