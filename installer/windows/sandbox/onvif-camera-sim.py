"""Simulated ONVIF camera for Windows 1.3.2 acceptance (Blue, 2026-10-20). Test tooling only: never packaged.

Answers exactly what the AnyAiCam agent asks a camera (appliance-agent/anyaicam_agent/discovery.py, onvif_media.py):

* WS-Discovery (UDP 3702): a Probe -> one ProbeMatches, sent unicast to the prober, with a stable
  EndpointReference ``urn:uuid:...`` (the agent's device_key), Types ``dn:NetworkVideoTransmitter``, manufacturer/model
  Scopes and the Device-service XAddr. It listens on --bind:3702 for unicast probes (CAM-5 add by address) and, with
  --multicast, also joins 239.255.255.250 on that interface (automatic discovery). --reply-from-ephemeral answers
  from a different source port (the CAM-10 Windows-firewall case).
* SOAP over HTTP: GetCapabilities (Media XAddr), GetDeviceInformation, GetProfiles (MainStream + SubStream) and
  GetStreamUri (an rtsp:// URI per profile, e.g. MediaMTX publishing an FFmpeg test pattern in the same guest).
* Optional authentication (--username/--password): requests without a valid WS-Security UsernameToken PasswordDigest
  (SHA-1 of nonce + created + password, created within 5 minutes) get HTTP 400 with a SOAP "Sender not Authorized"
  fault, as real cameras do -- so the agent's unauthenticated-first, then-with-credentials path is exercised.

Run (in a disposable test guest, never on a customer network):
    python onvif-camera-sim.py --bind 10.20.30.40 --http-port 80 --multicast \
        --rtsp rtsp://10.20.30.40:8554/cam1 --username admin --password <test-only>
Standard library only (runs on the bundled embedded Python).
"""
import argparse
import base64
import hashlib
import re
import socket
import struct
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WSDD_GROUP = "239.255.255.250"
WSDD_PORT = 3702
NS = ('xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
      'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
      'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
      'xmlns:dn="http://www.onvif.org/ver10/network/wsdl" '
      'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
      'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
      'xmlns:tt="http://www.onvif.org/ver10/schema"')


class Camera:
    def __init__(self, *, bind, http_port, rtsp, endpoint=None, manufacturer="AnyAiCamSim", model="SIM-1",
                 username=None, password=None, sub_rtsp=None):
        self.bind, self.http_port = bind, http_port
        self.rtsp, self.sub_rtsp = rtsp, sub_rtsp or rtsp
        self.endpoint = endpoint or f"urn:uuid:{uuid.uuid4()}"
        self.manufacturer, self.model = manufacturer, model
        self.username, self.password = username, password
        self.requests = []          # (operation, authenticated) seen, for tests and the console

    @property
    def device_url(self):
        return f"http://{self.bind}:{self.http_port}/onvif/device_service"

    @property
    def media_url(self):
        return f"http://{self.bind}:{self.http_port}/onvif/media_service"

    # ---------------------------------------------------------------- WS-Discovery
    def probe_match(self, probe: bytes) -> bytes | None:
        text = probe.decode("utf-8", "ignore")
        if "Probe" not in text or "ProbeMatches" in text:
            return None
        types = re.search(r"<[^>]*Types[^>]*>([^<]*)</", text)
        if types and types.group(1).strip() and "NetworkVideoTransmitter" not in types.group(1):
            return None
        relates = re.search(r"<[^>]*MessageID[^>]*>\s*([^<\s]+)\s*</", text)
        scopes = (f"onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/manufacturer/{self.manufacturer} "
                  f"onvif://www.onvif.org/model/{self.model} onvif://www.onvif.org/name/{self.model}")
        return (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {NS}><s:Header>'
                f"<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>"
                f"<a:RelatesTo>{relates.group(1) if relates else ''}</a:RelatesTo>"
                "<a:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:To>"
                "<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</a:Action></s:Header>"
                "<s:Body><d:ProbeMatches><d:ProbeMatch>"
                f"<a:EndpointReference><a:Address>{self.endpoint}</a:Address></a:EndpointReference>"
                "<d:Types>dn:NetworkVideoTransmitter</d:Types>"
                f"<d:Scopes>{scopes}</d:Scopes><d:XAddrs>{self.device_url}</d:XAddrs>"
                "<d:MetadataVersion>1</d:MetadataVersion></d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"
                ).encode("utf-8")

    # ---------------------------------------------------------------- SOAP
    def authorized(self, body: str) -> bool:
        if not self.username:
            return True
        user = re.search(r"<[^>]*Username[^>]*>([^<]*)</", body)
        digest = re.search(r"<[^>]*Password[^>]*>([^<]*)</", body)
        nonce = re.search(r"<[^>]*Nonce[^>]*>([^<]*)</", body)
        created = re.search(r"<[^>]*Created[^>]*>([^<]*)</", body)
        if not (user and digest and nonce and created) or user.group(1) != self.username:
            return False
        try:
            when = datetime.strptime(created.group(1).strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            raw_nonce = base64.b64decode(nonce.group(1).strip(), validate=True)
        except ValueError:
            return False
        if abs(datetime.now(timezone.utc) - when) > timedelta(minutes=5):
            return False
        expected = base64.b64encode(hashlib.sha1(raw_nonce + created.group(1).strip().encode() + self.password.encode()).digest()).decode()
        return expected == digest.group(1).strip()

    def respond(self, path: str, body: str) -> tuple[int, str]:
        operation = re.search(r"<(?:\w+:)?Body[^>]*>\s*<(?:\w+:)?(\w+)", body)
        operation = operation.group(1) if operation else ""
        ok = self.authorized(body)
        self.requests.append((operation, ok))
        if not ok:
            return 400, (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {NS}><s:Body><s:Fault><s:Code>'
                         "<s:Value>s:Sender</s:Value><s:Subcode><s:Value>ter:NotAuthorized</s:Value></s:Subcode></s:Code>"
                         '<s:Reason><s:Text xml:lang="en">Sender not Authorized</s:Text></s:Reason></s:Fault></s:Body></s:Envelope>')
        if operation == "GetCapabilities":
            inner = (f"<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Device><tt:XAddr>{self.device_url}</tt:XAddr></tt:Device>"
                     f"<tt:Media><tt:XAddr>{self.media_url}</tt:XAddr></tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>")
        elif operation == "GetDeviceInformation":
            inner = (f"<tds:GetDeviceInformationResponse><tds:Manufacturer>{self.manufacturer}</tds:Manufacturer>"
                     f"<tds:Model>{self.model}</tds:Model><tds:FirmwareVersion>sim-1.0</tds:FirmwareVersion>"
                     "<tds:SerialNumber>SIM0001</tds:SerialNumber><tds:HardwareId>sim</tds:HardwareId></tds:GetDeviceInformationResponse>")
        elif operation == "GetProfiles":
            inner = ('<trt:GetProfilesResponse><trt:Profiles token="Profile_1" fixed="true"><tt:Name>MainStream</tt:Name></trt:Profiles>'
                     '<trt:Profiles token="Profile_2" fixed="true"><tt:Name>SubStream</tt:Name></trt:Profiles></trt:GetProfilesResponse>')
        elif operation == "GetStreamUri":
            uri = self.sub_rtsp if "Profile_2" in body else self.rtsp
            inner = (f"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>{uri}</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
                     "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT0S</tt:Timeout></trt:MediaUri></trt:GetStreamUriResponse>")
        else:
            return 400, (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {NS}><s:Body><s:Fault><s:Code><s:Value>s:Sender</s:Value>'
                         "</s:Code><s:Reason><s:Text>ActionNotSupported</s:Text></s:Reason></s:Fault></s:Body></s:Envelope>")
        return 200, f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {NS}><s:Body>{inner}</s:Body></s:Envelope>'


class Simulator:
    """Starts the HTTP and WS-Discovery listeners for one Camera; stop() closes everything."""
    def __init__(self, camera: Camera, *, wsdd_port=WSDD_PORT, multicast=False, reply_from_ephemeral=False):
        self.camera, self.wsdd_port, self.multicast, self.ephemeral = camera, wsdd_port, multicast, reply_from_ephemeral
        self._threads, self._stop = [], threading.Event()

    def start(self):
        camera = self.camera

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                status, payload = camera.respond(self.path, self.rfile.read(length).decode("utf-8", "ignore"))
                data = payload.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.http = ThreadingHTTPServer((camera.bind, camera.http_port), Handler)
        camera.http_port = self.http.server_address[1]            # 0 -> an OS-assigned port
        self._spawn(self.http.serve_forever)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp.bind((camera.bind if not self.multicast else "", self.wsdd_port))
        self.wsdd_port = self.udp.getsockname()[1]
        if self.multicast:
            membership = struct.pack("4s4s", socket.inet_aton(WSDD_GROUP), socket.inet_aton(camera.bind))
            self.udp.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        self.udp.settimeout(0.2)
        self._spawn(self._serve_wsdd)
        return self

    def _spawn(self, target):
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _serve_wsdd(self):
        while not self._stop.is_set():
            try:
                data, sender = self.udp.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            reply = self.camera.probe_match(data)
            if not reply:
                continue
            if self.ephemeral:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
                    other.bind((self.camera.bind, 0))
                    other.sendto(reply, sender)
            else:
                self.udp.sendto(reply, sender)

    def stop(self):
        self._stop.set()
        self.http.shutdown()
        self.http.server_close()
        self.udp.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bind", required=True, help="the camera's IPv4 address in the test guest")
    parser.add_argument("--http-port", type=int, default=80)
    parser.add_argument("--rtsp", required=True, help="rtsp:// URI returned for the main profile")
    parser.add_argument("--sub-rtsp", help="rtsp:// URI for the sub profile (default: same as --rtsp)")
    parser.add_argument("--endpoint", help="urn:uuid:... (default: a new random UUID)")
    parser.add_argument("--manufacturer", default="AnyAiCamSim")
    parser.add_argument("--model", default="SIM-1")
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--multicast", action="store_true", help="also answer multicast WS-Discovery on --bind")
    parser.add_argument("--reply-from-ephemeral", action="store_true", help="answer probes from another source port")
    args = parser.parse_args()
    if bool(args.username) != bool(args.password):
        parser.error("--username and --password go together")
    camera = Camera(bind=args.bind, http_port=args.http_port, rtsp=args.rtsp, sub_rtsp=args.sub_rtsp,
                    endpoint=args.endpoint, manufacturer=args.manufacturer, model=args.model,
                    username=args.username, password=args.password)
    simulator = Simulator(camera, multicast=args.multicast, reply_from_ephemeral=args.reply_from_ephemeral).start()
    print(f"ONVIF simulator {camera.endpoint} on {camera.device_url} (WS-Discovery udp/{simulator.wsdd_port}"
          f"{' + multicast' if args.multicast else ''}); Ctrl+C stops it", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        simulator.stop()


if __name__ == "__main__":
    main()
