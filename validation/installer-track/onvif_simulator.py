"""Read-only synthetic ONVIF camera. Default prints plan; --serve is future VM only."""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import socket
import threading
import uuid
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

CAMERA_ID = '414e5941-4943-414d-8123-123456789abc'
GROUP = '239.255.255.250'
NAMESPACES = ('xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
    'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
    'xmlns:tt="http://www.onvif.org/ver10/schema" '
    'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"')


def envelope(body, header=''):
    return ('<?xml version="1.0"?><s:Envelope '+NAMESPACES+
            '><s:Header>'+header+'</s:Header><s:Body>'+body+'</s:Body></s:Envelope>').encode()


def discovery(ip, relates_to):
    header = ('<w:MessageID>uuid:'+str(uuid.uuid4())+'</w:MessageID>'
        '<w:RelatesTo>'+escape(relates_to)+'</w:RelatesTo>'
        '<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</w:Action>')
    body = ('<d:ProbeMatches><d:ProbeMatch><w:EndpointReference><w:Address>urn:uuid:'+CAMERA_ID+
        '</w:Address></w:EndpointReference><d:XAddrs>http://'+ip+
        ':8080/onvif/device_service</d:XAddrs><d:MetadataVersion>1</d:MetadataVersion>'
        '</d:ProbeMatch></d:ProbeMatches>')
    return envelope(body, header)


def response(action, ip):
    base = 'http://'+ip+':8080/onvif/'
    resolution = '<tt:Width>1280</tt:Width><tt:Height>720</tt:Height>'
    if action == 'GetDeviceInformation':
        body = ('<tds:GetDeviceInformationResponse><tds:Manufacturer>AnyAiCam Test</tds:Manufacturer>'
            '<tds:Model>SIMULATOR ONLY</tds:Model><tds:FirmwareVersion>1</tds:FirmwareVersion>'
            '<tds:SerialNumber>SYNTHETIC-001</tds:SerialNumber>'
            '<tds:HardwareId>test-only</tds:HardwareId></tds:GetDeviceInformationResponse>')
    elif action == 'GetServices':
        body = '<tds:GetServicesResponse>'
        for namespace, endpoint in [('device','device_service'),('media','media_service')]:
            body += ('<tds:Service><tds:Namespace>http://www.onvif.org/ver10/'+namespace+
                '/wsdl</tds:Namespace><tds:XAddr>'+base+endpoint+
                '</tds:XAddr><tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor>'
                '</tds:Version></tds:Service>')
        body += '</tds:GetServicesResponse>'
    elif action == 'GetCapabilities':
        body = ('<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Device><tt:XAddr>'+base+
            'device_service</tt:XAddr></tt:Device><tt:Media><tt:XAddr>'+base+
            'media_service</tt:XAddr></tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>')
    elif action == 'GetVideoSources':
        body = ('<trt:GetVideoSourcesResponse><trt:VideoSources token="sensor1"><tt:Framerate>15</tt:Framerate>'
            '<tt:Resolution>'+resolution+'</tt:Resolution></trt:VideoSources></trt:GetVideoSourcesResponse>')
    elif action == 'GetProfiles':
        body = ('<trt:GetProfilesResponse><trt:Profiles token="profile1" fixed="true"><tt:Name>Test Pattern</tt:Name>'
            '<tt:VideoSourceConfiguration token="source1"><tt:Name>Test Source</tt:Name><tt:UseCount>1</tt:UseCount>'
            '<tt:SourceToken>sensor1</tt:SourceToken><tt:Bounds x="0" y="0" width="1280" height="720"/>'
            '</tt:VideoSourceConfiguration><tt:VideoEncoderConfiguration token="encoder1"><tt:Name>H264</tt:Name>'
            '<tt:UseCount>1</tt:UseCount><tt:Encoding>H264</tt:Encoding><tt:Resolution>'+resolution+
            '</tt:Resolution><tt:Quality>5</tt:Quality><tt:RateControl><tt:FrameRateLimit>15</tt:FrameRateLimit>'
            '<tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>2000</tt:BitrateLimit></tt:RateControl>'
            '<tt:H264><tt:GovLength>30</tt:GovLength><tt:H264Profile>Baseline</tt:H264Profile></tt:H264>'
            '<tt:SessionTimeout>PT60S</tt:SessionTimeout></tt:VideoEncoderConfiguration>'
            '</trt:Profiles></trt:GetProfilesResponse>')
    elif action == 'GetStreamUri':
        body = ('<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://'+ip+
            ':8554/testcamera</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>'
            '<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT60S</tt:Timeout>'
            '</trt:MediaUri></trt:GetStreamUriResponse>')
    elif action == 'GetSystemDateAndTime':
        now = datetime.now(timezone.utc)
        body = ('<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:DateTimeType>NTP</tt:DateTimeType>'
            '<tt:DaylightSavings>false</tt:DaylightSavings><tt:UTCDateTime><tt:Time>'
            f'<tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second>'
            '</tt:Time><tt:Date>'+f'<tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day>'
            '</tt:Date></tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>')
    else:
        raise ValueError('Read-only test simulator does not support that operation.')
    return envelope(body)


def validate_network(ip, cidr):
    address, network = ipaddress.ip_address(ip), ipaddress.ip_network(cidr)
    if address.version != 4 or not network.is_private or address not in network or network.prefixlen < 24:
        raise ValueError('Require an IPv4 address on an explicit private /24 or smaller lab network.')
    return network


def serve(ip, cidr):
    network = validate_network(ip, cidr)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            if ipaddress.ip_address(self.client_address[0]) not in network:
                self.send_error(403); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536: raise ValueError()
                xml = ET.fromstring(self.rfile.read(length))
                body = next(x for x in xml.iter() if x.tag.endswith('}Body'))
                action = list(body)[0].tag.rsplit('}', 1)[-1]
                data = response(action, ip)
            except (ValueError, ET.ParseError, StopIteration, IndexError):
                self.send_error(400); return
            self.send_response(200)
            self.send_header('Content-Type','application/soap+xml; charset=utf-8')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers(); self.wfile.write(data)
    # Multicast is joined only on the explicitly chosen PRIVATE interface.
    stop = threading.Event()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('',3702))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                   socket.inet_aton(GROUP)+socket.inet_aton(ip))
    sock.settimeout(1)
    def probes():
        while not stop.is_set():
            try:
                packet, peer = sock.recvfrom(65536)
                if ipaddress.ip_address(peer[0]) not in network: continue
                xml = ET.fromstring(packet)
                if not any(x.tag.endswith('}Probe') for x in xml.iter()): continue
                message_id = next(x.text for x in xml.iter() if x.tag.endswith('}MessageID'))
                if not message_id or len(message_id)>128: continue
                sock.sendto(discovery(ip,message_id),peer)
            except (socket.timeout, ET.ParseError, StopIteration): continue
    worker = threading.Thread(target=probes, daemon=True)
    server = ThreadingHTTPServer((ip,8080),Handler)
    worker.start()
    try: server.serve_forever()
    finally:
        stop.set(); worker.join(timeout=2); sock.close(); server.server_close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('--ip',default='192.168.253.10')
    parser.add_argument('--cidr',default='192.168.253.0/24')
    args=parser.parse_args()
    validate_network(args.ip,args.cidr)
    if args.serve: serve(args.ip,args.cidr)
    else: print('PREPARED ONLY: private ONVIF UDP/3702, HTTP/8080; no listeners started.')
