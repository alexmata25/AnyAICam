import json,pathlib,requests,socket,sys,uuid
from xml.etree import ElementTree as ET
sys.path.insert(0,'/app')
from onvif_discovery import parse_probe_response
results=[]
def check(name,condition):
    results.append({'name':name,'passed':bool(condition)})
    if not condition: raise AssertionError(name)
url='http://192.168.253.10:8080/onvif/device_service'
template='<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:t="http://www.onvif.org/ver10/device/wsdl"><s:Body><t:{}/></s:Body></s:Envelope>'
r=requests.post(url,data=template.format('GetDeviceInformation'),headers={'Content-Type':'application/soap+xml'},timeout=10)
check('Real ONVIF device information',r.status_code==200 and 'SYNTHETIC-001' in r.text)
r=requests.post(url,data=template.format('SetSystemFactoryDefault'),headers={'Content-Type':'application/soap+xml'},timeout=10)
check('ONVIF mutation refused',r.status_code==400)
r=requests.post(url,data=template.format('GetStreamUri'),headers={'Content-Type':'application/soap+xml'},timeout=10)
check('Real ONVIF synthetic stream URI',r.status_code==200 and 'rtsp://192.168.253.10:8554/testcamera' in r.text)
devices=[]
for n in range(2):
    msg='uuid:'+str(uuid.uuid4())
    packet=('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"><s:Header><w:MessageID>'+msg+'</w:MessageID></s:Header><s:Body><d:Probe/></s:Body></s:Envelope>').encode()
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP) as sock:
        sock.bind(('192.168.253.10',0))
        sock.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_IF,socket.inet_aton('192.168.253.10'))
        sock.settimeout(5)
        sock.sendto(packet,('192.168.253.10',3702))
        data,sender=sock.recvfrom(65536)
    parsed=parse_probe_response(data.decode())
    devices.append(parsed)
    check('Real directed UDP probe response '+str(n+1),bool(parsed))
check('Stable camera UUID across changing message IDs',devices[0]['device_key']==devices[1]['device_key']=='414e5941-4943-414d-8123-123456789abc')
pathlib.Path('/home/ubuntu/anyaicam-lab/simulator-preflight-results.json').write_text(json.dumps({'passed':len(results),'tests':results,'scope':'Real helper-local directed UDP/SOAP; Samsung multicast discovery pending'},indent=2))
print(json.dumps({'passed':len(results),'failed':0,'scope':'helper-local simulator only'}))
