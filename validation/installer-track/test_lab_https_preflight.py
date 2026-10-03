import hashlib,json,pathlib,requests,sys
base='https://installer-test.test'
ca='/home/ubuntu/anyaicam-lab/lab-ca.crt'
expected='9dd0fe3fef6b46752a74ea49b4426cf9030313b926b1795a2e50c68bc48a53ac'
results=[]
def check(name,condition):
    results.append({'name':name,'passed':bool(condition)})
    if not condition:
        pathlib.Path('/home/ubuntu/anyaicam-lab/https-preflight-results.json').write_text(json.dumps(results,indent=2))
        raise AssertionError(name)
s=requests.Session()
s.verify=ca
r=s.get(base+'/customer-login.html',timeout=30)
check('HTTPS customer login page',r.status_code==200)
csrf=s.cookies.get('anyaicam_csrf','').strip('"')
check('Signed CSRF cookie issued securely',bool(csrf) and any(c.name=='anyaicam_csrf' and c.secure for c in s.cookies))
r=s.get(base+'/api/customer/downloads/vms-installer',timeout=30,allow_redirects=False)
check('Unsigned installer download refused',r.status_code in (401,403))
r=s.post(base+'/api/partner-login',json={'email':'installer-owner@example.test','password':'intentionally-invalid','customer_only':True},headers={'Origin':base},timeout=30,allow_redirects=False)
check('Login without CSRF refused',r.status_code==403)
r=s.post(base+'/api/partner-login',json={'email':'installer-owner@example.test','password':'intentionally-invalid','customer_only':True},headers={'Origin':'https://wrong-origin.example.test','X-CSRF-Token':csrf},timeout=30,allow_redirects=False)
check('Foreign origin refused',r.status_code==403)
credentials={}
for line in pathlib.Path('/var/lib/anyaicam-installer-lab/owner-login.txt').read_text().splitlines():
    if ': ' in line:
        k,v=line.split(': ',1);credentials[k]=v
r=s.post(base+'/api/partner-login',json={'email':credentials['Email'],'password':credentials['Password'],'customer_only':True},headers={'Origin':base,'X-CSRF-Token':csrf},timeout=30,allow_redirects=False)
credentials.clear()
check('Real scoped owner login succeeds',r.status_code==303)
location=r.headers.get('Location','')
check('Login redirect stays inside lab',location.startswith('/') and not location.startswith('//') or location.startswith(base+'/'))
check('Owner session cookie secure',any(c.name=='anyaicam_partner_session' and c.secure and 'HttpOnly' in c._rest for c in s.cookies))
r=s.get(base+'/subscription-portal',timeout=30)
check('Customer install page renders',r.status_code==200 and 'Download AnyAiCam' in r.text)
r=s.get(base+'/api/customer/downloads/vms-installer',timeout=60,stream=True)
check('Licensed owner download succeeds',r.status_code==200)
digest=hashlib.sha256()
size=0
for chunk in r.iter_content(1024*1024):
    digest.update(chunk);size+=len(chunk)
check('Exact investor bytes downloaded',size==36467580 and digest.hexdigest()==expected)
r=s.get(base+'/customer-login.html',headers={'Host':'wrong-host.example.test'},timeout=30,allow_redirects=False)
check('Wrong Host refused',r.status_code==400)
pathlib.Path('/home/ubuntu/anyaicam-lab/https-preflight-results.json').write_text(json.dumps({'passed':len(results),'tests':results,'scope':'Real isolated HTTPS/API preflight only; not Samsung installer/claim acceptance'},indent=2))
print(json.dumps({'passed':len(results),'failed':0,'scope':'real lab HTTPS/API preflight; Samsung install/claim not run'}))
