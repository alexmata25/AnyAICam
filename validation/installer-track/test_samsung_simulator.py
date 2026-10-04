import json,pathlib,sys,socket,urllib.request,urllib.error,uuid
work=pathlib.Path('/home/alejandro-mata/anyaicam-clean-validation')
root=pathlib.Path((work/'installer-root.txt').read_text().strip())
sys.path.insert(0,str(root/'payload/vms/app'))
from onvif_discovery import parse_probe_response,default_send_probe
results=[]
def check(name,condition):
 results.append({'name':name,'passed':bool(condition)})
 print(name+'='+('PASS' if condition else 'FAIL'),flush=True)
url='http://192.168.253.10:8080/onvif/device_service'
for action,expected,status in [('GetDeviceInformation','SYNTHETIC-001',200),('SetSystemFactoryDefault','',400),('GetStreamUri','rtsp://192.168.253.10:8554/testcamera',200)]:
 xml=f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:t="http://www.onvif.org/ver10/device/wsdl"><s:Body><t:{action}/></s:Body></s:Envelope>'
 req=urllib.request.Request(url,data=xml.encode(),headers={'Content-Type':'application/soap+xml'})
 try: r=urllib.request.urlopen(req,timeout=5)
 except urllib.error.HTTPError as e: r=e
 check('Real Samsung SOAP '+action,r.code==status and expected in r.read().decode())
keys=[]
for n in range(2):
 responses=default_send_probe(timeout=3)
 parsed=[parse_probe_response(x) for x in responses]
 valid=[x for x in parsed if x and x['ip_address']=='192.168.253.10']
 check('Real Samsung multicast discovery '+str(n+1),bool(valid))
 keys.extend(x['device_key'] for x in valid)
check('Stable UUID across probes',bool(keys) and set(keys)=={'414e5941-4943-414d-8123-123456789abc'})
for port in [8080,8554]:
 try:
  with socket.create_connection(('192.168.253.10',port),timeout=5): ok=True
 except OSError: ok=False
 check('Samsung camera port '+str(port),ok)
(work/'samsung-simulator-results.json').write_text(json.dumps(results,indent=2))
print('TOTAL_PASS='+str(sum(x['passed'] for x in results))+' TOTAL_FAIL='+str(sum(not x['passed'] for x in results)))
