import concurrent.futures
import ipaddress
import json
import re
import socket
import subprocess
import uuid
from pathlib import Path


def local_networks(configured=None):
    if configured: return [ipaddress.ip_network(item,strict=False) for item in configured]
    try:
        result=subprocess.run(['ip','-j','-4','addr','show','scope','global'],capture_output=True,text=True,timeout=5,check=True); data=json.loads(result.stdout); networks=[]
        for interface in data:
            for address in interface.get('addr_info',[]):
                network=ipaddress.ip_network(f"{address['local']}/{min(24,int(address['prefixlen']))}",strict=False)
                if network.is_private: networks.append(network)
        return networks
    except (OSError,subprocess.SubprocessError,json.JSONDecodeError,KeyError): return []


def arp_table():
    output={}
    try:
        for line in Path('/proc/net/arp').read_text().splitlines()[1:]:
            fields=line.split()
            if len(fields)>=4: output[fields[0]]=fields[3]
    except OSError: pass
    return output


def _port(ip,port,timeout=.25):
    sock=socket.socket(); sock.settimeout(timeout)
    try: return sock.connect_ex((str(ip),port))==0
    finally: sock.close()


def _onvif_probe(timeout=2):
    message=f'''<?xml version="1.0" encoding="UTF-8"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header><w:MessageID>uuid:{uuid.uuid4()}</w:MessageID><w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To><w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>'''.encode(); found={}; sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); sock.settimeout(.25)
    try:
        sock.sendto(message,('239.255.255.250',3702)); end=__import__('time').time()+timeout
        while __import__('time').time()<end:
            try:
                data,address=sock.recvfrom(65535); text=data.decode(errors='ignore'); scopes=' '.join(re.findall(r'<[^>]*Scopes[^>]*>(.*?)</',text,re.I)); found[address[0]]=scopes
            except socket.timeout: continue
    except OSError: pass
    finally: sock.close()
    return found


def _scope_value(scopes,key):
    match=re.search(rf'onvif://www.onvif.org/{key}/([^\s<]+)',scopes,re.I); return match.group(1).replace('%20',' ') if match else 'Unknown'


def scan(networks=None,max_hosts=1024):
    networks=local_networks(networks); onvif=_onvif_probe(); arp=arp_table(); addresses=[]
    for network in networks:
        addresses.extend(list(network.hosts())[:max(0,max_hosts-len(addresses))])
        if len(addresses)>=max_hosts: break
    addresses=list({str(item) for item in addresses}|set(onvif)); results=[]
    def inspect(ip):
        rtsp=_port(ip,554) or _port(ip,8554); scopes=onvif.get(ip,''); onvif_supported=bool(scopes) or _port(ip,80) or _port(ip,8000)
        if not rtsp and not scopes: return None
        return {'id':'camera-'+ip.replace('.','-'),'name':'Camera '+ip,'ip':ip,'manufacturer':_scope_value(scopes,'manufacturer'),'model':_scope_value(scopes,'model'),'mac_address':arp.get(ip,'Unknown'),'onvif_support':onvif_supported,'rtsp_support':rtsp,'connection_status':'reachable','online':True,'recording':False,'analytics':False,'last_recording_at':None,'last_error':None}
    with concurrent.futures.ThreadPoolExecutor(max_workers=48) as pool:
        for item in pool.map(inspect,addresses):
            if item: results.append(item)
    return sorted(results,key=lambda item:ipaddress.ip_address(item['ip']))
