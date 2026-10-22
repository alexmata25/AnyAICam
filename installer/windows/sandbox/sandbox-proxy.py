"""Local evidence proxy for the functional Sandbox scenarios (Blue, 2026-10-21). Test tooling only: never packaged.
Standard library only (runs on the bundled embedded Python). Listens on 127.0.0.1 only.

Headless Edge cannot be handed the owner's session cookie for 127.0.0.1:8000, and a WHEP offer to MediaMTX
(127.0.0.1:8889) from another origin needs CORS. This proxy does both, so Edge can render the REAL pages:
  * GET  /__whep.html?path=<camera_id>  a WHEP player page (recvonly video, loopback ICE): plays the VMS MediaMTX path,
                                        then POSTs its result and one decoded frame (PNG) back here;
  * POST /__whep/<path>                 forwarded to http://127.0.0.1:8889/<path>/whep (the VMS MediaMTX, unchanged);
  * POST /__result, /__frame            saved as <results>/webrtc-result.json and <results>/webrtc-frame.png;
  * anything else (GET)                 forwarded to the VMS http://127.0.0.1:8000 with the owner's session cookie read
                                        from the edge-http.py cookie jar (LWP format), for page screenshots.

    python sandbox-proxy.py --jar C:\\Sim\\owner.cookies --results C:\\Validation\\results [--port 18080]
"""
import argparse
import base64

import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VMS = "http://127.0.0.1:8000"
MEDIAMTX_WEBRTC = "http://127.0.0.1:8889"

WHEP_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>WHEP check</title>
<style>body{background:#111;color:#eee;font:14px sans-serif}video{width:640px;height:360px;background:#000}</style></head>
<body><h3>Local WebRTC (WHEP) check: <span id="state">starting</span></h3><video id="v" muted autoplay playsinline></video>
<canvas id="c" width="640" height="360" style="display:none"></canvas>
<script>
const path = new URLSearchParams(location.search).get('path');
const state = (s) => { document.getElementById('state').textContent = s; };
const report = (body) => fetch('/__result', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
(async () => {
  const started = Date.now();
  try {
    const pc = new RTCPeerConnection({iceServers: []});
    pc.addTransceiver('video', {direction: 'recvonly'});
    const video = document.getElementById('v');
    pc.ontrack = (e) => { video.srcObject = e.streams[0] || new MediaStream([e.track]); };
    const offer = await pc.createOffer(); await pc.setLocalDescription(offer);
    await new Promise((r) => { if (pc.iceGatheringState === 'complete') r(); pc.onicegatheringstatechange = () => pc.iceGatheringState === 'complete' && r(); setTimeout(r, 4000); });
    const answer = await fetch('/__whep/' + encodeURIComponent(path), {method: 'POST', headers: {'Content-Type': 'application/sdp'}, body: pc.localDescription.sdp});
    const sdp = await answer.text();
    if (!answer.ok) { state('whep ' + answer.status); await report({ok: false, stage: 'whep', status: answer.status, body: sdp.slice(0, 300)}); return; }
    await pc.setRemoteDescription({type: 'answer', sdp});
    state('connecting');
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 500));
      if (video.videoWidth > 0 && video.currentTime > 1.5) {
        const c = document.getElementById('c'); c.getContext('2d').drawImage(video, 0, 0, 640, 360);
        await fetch('/__frame', {method: 'POST', body: c.toDataURL('image/png')});
        state('playing');
        await report({ok: true, width: video.videoWidth, height: video.videoHeight, currentTime: video.currentTime,
                      ice: pc.iceConnectionState, seconds: (Date.now() - started) / 1000});
        return;
      }
    }
    state('no video'); await report({ok: false, stage: 'media', ice: pc.iceConnectionState, connection: pc.connectionState,
                                     width: video.videoWidth, currentTime: video.currentTime});
  } catch (e) { state('error'); await report({ok: false, stage: 'exception', error: String(e)}); }
})();
</script></body></html>"""


def session_cookie(jar: Path) -> str:
    try:
        text = jar.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    found = re.search(r'anyaicam_partner_session="?([^";\s]+)', text)
    return f"anyaicam_partner_session={found.group(1)}" if found else ""


def make_handler(jar: Path, results: Path, vms: str = VMS, mediamtx: str = MEDIAMTX_WEBRTC):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, body: bytes, content_type="text/plain", extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_GET(self):
            if self.path.startswith("/__whep.html"):
                return self._send(200, WHEP_PAGE.encode(), "text/html; charset=utf-8")
            request = urllib.request.Request(vms + self.path, headers={"Cookie": session_cookie(jar)})
            try:
                with urllib.request.urlopen(request, timeout=30) as answer:
                    return self._send(answer.status, answer.read(), answer.headers.get("Content-Type", "application/octet-stream"))
            except urllib.error.HTTPError as error:
                return self._send(error.code, error.read(), error.headers.get("Content-Type", "text/plain"))
            except (urllib.error.URLError, OSError) as error:
                return self._send(502, str(error).encode())

        def do_POST(self):
            body = self._body()
            if self.path.startswith("/__whep/"):
                target = mediamtx + "/" + self.path[len("/__whep/"):] + "/whep"
                request = urllib.request.Request(target, data=body, method="POST", headers={"Content-Type": "application/sdp"})
                try:
                    with urllib.request.urlopen(request, timeout=20) as answer:
                        return self._send(answer.status, answer.read(), "application/sdp")
                except urllib.error.HTTPError as error:
                    return self._send(error.code, error.read())
                except (urllib.error.URLError, OSError) as error:
                    return self._send(502, str(error).encode())
            if self.path == "/__result":
                (results / "webrtc-result.json").write_bytes(body)
                return self._send(204, b"")
            if self.path == "/__frame":
                text = body.decode("ascii", errors="replace")
                if text.startswith("data:image/png;base64,"):
                    (results / "webrtc-frame.png").write_bytes(base64.b64decode(text.split(",", 1)[1]))
                return self._send(204, b"")
            return self._send(404, b"")

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jar", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--vms", default=VMS)
    ap.add_argument("--mediamtx", default=MEDIAMTX_WEBRTC)
    args = ap.parse_args()
    results = Path(args.results)
    results.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(Path(args.jar), results, args.vms, args.mediamtx)).serve_forever()


if __name__ == "__main__":
    main()
