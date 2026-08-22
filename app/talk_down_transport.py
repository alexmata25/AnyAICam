"""Generic camera talk-down (backchannel audio) transport interface,
plus the one concrete implementation this milestone builds: standards-
based ONVIF/RTSP backchannel (SETUP+RECORD on the audio media section
a camera's own SDP advertises, then RTP/PCMU packets over that
negotiated channel).

TalkDownTransport is the seam a vendor-specific fallback would plug
into later, exactly per the requirement that vendor-specific behavior
must be isolated behind a generic interface and never keyed off camera
number or model -- there is deliberately no camera_number parameter
anywhere in this file. Selection of which transport to use is driven
entirely by the discovered talk_down_metadata (an ONVIF-capable camera
gets OnvifBackchannelTransport; a future vendor-specific
implementation would be selected by inspecting metadata content, e.g.
an absence of a usable ONVIF audio_output_token, never by a
camera-number/model check).

Everything here that touches the network -- the RTSP digest-auth
handshake -- reuses the exact mechanism this session's earlier
read-only backchannel-capability probe scripts already validated
against the real lab cameras (WS-Security-adjacent RTSP digest with
qop=auth/nc/cnonce/opaque/algorithm support). RTP packetization is a
small, pure, independently-testable function.

Not exercised against a real camera in this milestone -- see the
module docstring's explicit scope note in talk_audio_relay_client.py.
No SETUP/RECORD/RTP packet is ever sent to a real device by anything
in this codebase yet; OnvifBackchannelTransport exists and is unit-
tested, but its .connect()/.send() are never invoked outside a test in
this commit.

connect() logs one INFO line per protocol stage -- target host/port/
path once at the start, then each stage's outcome (status line or
"no_response") as it happens, not just the final failure -- added
during the real Camera 2 DESCRIBE-gets-no-response investigation, to
make it possible to tell which exact stage (DESCRIBE, SDP parsing,
SETUP, session extraction, RECORD) is failing in production without
guessing from a single exception message. Host/port/path are not
secrets and are logged directly; credentials, the Authorization header
value, and any digest response are never logged anywhere in this
module.
"""

import hashlib
import logging
import re
import secrets
import socket
import struct
import time
from typing import Callable

logger = logging.getLogger("anyaicam.talk_down_transport")


class TalkDownTransport:
    """The generic seam. connect() negotiates whatever the concrete
    transport needs (e.g. RTSP SETUP/RECORD); send() delivers one
    already-encoded audio frame (PCMU bytes for the ONVIF
    implementation); close() tears the session down. No method here
    takes or returns anything camera-number/model-specific."""

    def connect(self) -> None:
        raise NotImplementedError

    def send(self, encoded_audio: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


# ---------------------------------------------------------- RTSP digest auth (proven this session)

def _parse_digest_challenge(header_value: str) -> dict:
    value = header_value.strip()
    if value.lower().startswith("digest"):
        value = value[len("digest"):].strip()
    params = {}
    for match in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', value):
        key = match.group(1).lower()
        params[key] = match.group(2) if match.group(2) is not None else match.group(3)
    return params


def _digest_authorization(challenge: dict, username: str, password: str, method: str, request_uri: str, cnonce: str, nc: str) -> str:
    def md5hex(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    algorithm = (challenge.get("algorithm") or "MD5").upper()
    qop_offered = challenge.get("qop")
    qop = None
    if qop_offered:
        options = [item.strip() for item in qop_offered.split(",")]
        qop = "auth" if "auth" in options else options[0]

    ha1 = md5hex(f"{username}:{realm}:{password}")
    if algorithm == "MD5-SESS":
        ha1 = md5hex(f"{ha1}:{nonce}:{cnonce}")
    ha2 = md5hex(f"{method}:{request_uri}")
    response = md5hex(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}") if qop == "auth" else md5hex(f"{ha1}:{nonce}:{ha2}")

    parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{request_uri}"', f'response="{response}"']
    if challenge.get("algorithm"):
        parts.append(f"algorithm={algorithm}")
    if qop:
        parts += [f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"']
    if "opaque" in challenge:
        parts.append(f'opaque="{challenge["opaque"]}"')
    return "Digest " + ", ".join(parts)


def _rtsp_request(sock: socket.socket, next_cseq: Callable[[], int], method: str, request_uri: str, extra_headers: str = "", auth_header: str | None = None, body: bytes = b"", attempt: str = "initial") -> str:
    """Sends one RTSP request and returns the raw response text.

    `next_cseq` is a zero-argument callable returning the next CSeq to
    use, called exactly once per actual request sent. Confirmed by real
    Camera 2 evidence and cross-checked against RFC 2326 SS12.17 and two
    mature reference clients (Live555's RTSPClient::resendCommand(),
    which does `request->cseq() = ++fCSeq` on a digest retry; FFmpeg's
    libavformat/rtsp.c, whose 401-retry path re-enters
    ff_rtsp_send_cmd_with_content_async(), which does `rt->seq++` on
    every call): a digest 401-challenge-then-retry is a distinct request
    (it received an actual response, so it isn't the "repeated because
    of lack of acknowledgement" case RFC 2326 exempts from incrementing)
    and must get a fresh, incremented CSeq, not reuse the original's.
    Threading a single shared counter through every call (see
    OnvifBackchannelTransport._next_cseq()) instead of a caller-hardcoded
    literal is what makes every request on one connection -- every
    stage, every retry -- get a strictly increasing CSeq with no caller
    needing to know how many requests came before it.

    `attempt` is a caller-supplied label ("initial" or "digest_retry")
    purely for the diagnostic log line below -- it has no effect on
    what's actually sent. Logs method/CSeq/attempt/request_uri before
    sending and the resulting status line (or "no_response") after.
    Never logs auth_header, credentials, passwords, or the digest
    nonce/response; those never appear in this log line at all."""
    cseq = next_cseq()
    headers = f"{method} {request_uri} RTSP/1.0\r\nCSeq: {cseq}\r\nUser-Agent: anyaicam-talk-down/0.1\r\n{extra_headers}"
    if auth_header:
        headers += f"Authorization: {auth_header}\r\n"
    if body:
        headers += f"Content-Length: {len(body)}\r\n"
    headers += "\r\n"
    logger.info("talk_down_transport.rtsp_request method=%s cseq=%s attempt=%s request_uri=%s", method, cseq, attempt, request_uri)
    sock.sendall(headers.encode() + body)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    response = data.decode(errors="replace")
    logger.info("talk_down_transport.rtsp_response method=%s cseq=%s attempt=%s status=%s", method, cseq, attempt, _status_line(response) or "no_response")
    return response


def _status_line(response: str) -> str:
    """The first line of an RTSP response, or "" for an empty/no
    response -- e.g. the camera closed the connection before sending
    anything back. splitlines()[0] on an empty string raises IndexError
    (splitlines() returns [] for ""); every call site in this module
    goes through this helper instead of repeating that guard inline, so
    there is exactly one place that can get it wrong, not several."""
    lines = response.splitlines()
    return lines[0] if lines else ""


def _authenticated_rtsp_request(sock: socket.socket, next_cseq: Callable[[], int], method: str, request_uri: str, username: str, password: str, extra_headers: str = "", body: bytes = b"") -> str:
    first = _rtsp_request(sock, next_cseq, method, request_uri, extra_headers=extra_headers, body=body, attempt="initial")
    status_line = _status_line(first)
    if " 401 " not in status_line:
        return first
    www_authenticate = next((line for line in first.splitlines() if line.lower().startswith("www-authenticate")), "")
    # partition(), not split(...)[1] -- partition() always returns a
    # 3-tuple even when the separator is absent (a camera returning 401
    # with no WWW-Authenticate header at all, or one this scan somehow
    # missed), so this can never raise. An empty challenge here still
    # produces a syntactically valid (if wrong) Authorization header on
    # the retry below; if that's also rejected, the caller's own
    # status-line check reports it as a normal, diagnosable auth
    # failure rather than crashing here.
    challenge = _parse_digest_challenge(www_authenticate.partition(":")[2].strip())
    cnonce = secrets.token_hex(8)
    auth = _digest_authorization(challenge, username, password, method, request_uri, cnonce, "00000001")
    return _rtsp_request(sock, next_cseq, method, request_uri, extra_headers=extra_headers, auth_header=auth, body=body, attempt="digest_retry")


# ---------------------------------------------------------- SDP control-URI resolution (pure)

def _resolve_control_uri(base_uri: str, control: str | None) -> str:
    """Resolves an SDP a=control: attribute value against the base RTSP
    request URI, per RTSP/SDP convention (RFC 2326 Appendix C.1.1 /
    RFC 8866): the value may be

      - None (no control attribute at all, or no audio section in the
        SDP) -- the base URI is used unchanged, exactly as before this
        function existed.
      - an already-absolute rtsp:// or rtsps:// URI -- used exactly
        as-is, never touched, never concatenated onto anything. Real
        Camera 2 evidence: this camera's SDP returns
        "rtsp://<host>:554/trackID=2" here, an absolute URI -- the
        previous code's f"{base_uri}/{control}" blindly concatenated
        it onto the base URI regardless, producing the doubled
        "rtsp://host:554//rtsp://host:554/trackID=2" that made SETUP
        (tolerated by a lenient camera) and then RECORD (not tolerated)
        fail.
      - a reference starting with "/" -- an absolute-path reference:
        replaces the base URI's own path entirely, keeping only its
        scheme+authority (host:port), the standard resolution rule for
        this shape.
      - a plain relative fragment (e.g. "trackID=2", the common ONVIF/
        Hikvision-style per-track suffix) -- appended to the base URI
        with exactly one "/" separator, regardless of whether the base
        URI itself already ends in one (this is what the previous code
        got wrong even for this simple case whenever rtsp_path was the
        bare "/" default: f"{base_uri}/{control}" produced a double
        slash there too).

    Pure function, no network/state -- independently testable."""
    if not control:
        return base_uri
    lowered = control.lower()
    if lowered.startswith("rtsp://") or lowered.startswith("rtsps://"):
        return control
    if control.startswith("/"):
        scheme_end = base_uri.find("://")
        if scheme_end == -1:
            return base_uri.rstrip("/") + "/" + control.lstrip("/")
        authority_start = scheme_end + 3
        authority_end = base_uri.find("/", authority_start)
        authority = base_uri[:authority_end] if authority_end != -1 else base_uri
        return authority + control
    return base_uri.rstrip("/") + "/" + control


# ---------------------------------------------------------- RTP/PCMU packetization (pure)

def build_rtp_packet(payload: bytes, sequence_number: int, timestamp: int, ssrc: int, payload_type: int = 0) -> bytes:
    """One standard 12-byte RTP header (RFC 3550) followed by the raw
    payload. payload_type 0 is the static PCMU (G.711 mu-law)
    assignment RTP itself defines -- not a project-specific choice.
    Pure function, no network/state -- independently testable."""
    version_flags = 0x80  # version=2, padding=0, extension=0, CSRC count=0
    marker_pt = payload_type & 0x7F  # marker bit always 0 for a continuous audio stream
    header = struct.pack("!BBHII", version_flags, marker_pt, sequence_number & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
    return header + payload


class OnvifBackchannelTransport(TalkDownTransport):
    """RTSP RECORD-based ONVIF backchannel delivery: SETUP the audio
    media section a camera's own DESCRIBE/SDP advertises, then RECORD
    to start the backchannel, then send RTP/PCMU packets over the
    negotiated UDP port for the session's duration, then TEARDOWN.

    Deliberately does not touch the camera's video RTSP session at all
    -- this opens its own independent RTSP connection/session purely
    for the audio backchannel, so it can never interfere with the
    existing video decode/HLS/recording pipeline, which has its own
    completely separate RTSP session via camera_url()."""

    def __init__(self, host: str, port: int, username: str, password: str, rtsp_path: str, timeout_seconds: float = 8.0):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.rtsp_path = rtsp_path
        self.timeout_seconds = timeout_seconds
        self._sock: socket.socket | None = None
        self._session_id: str | None = None
        self._rtp_socket: socket.socket | None = None
        self._rtp_remote: tuple[str, int] | None = None
        self._sequence = 0
        self._ssrc = secrets.randbits(32)
        self._cseq = 0  # one shared counter for every RTSP request on this connection, see _next_cseq()

    def _request_uri(self) -> str:
        return f"rtsp://{self.host}:{self.port}{self.rtsp_path}"

    def _next_cseq(self) -> int:
        """The single source of truth for this connection's CSeq --
        every RTSP request connect()/close() sends, across every stage
        and every digest retry, calls this instead of any stage
        hardcoding its own literal. Real Camera 2 evidence: DESCRIBE's
        401-then-retry consumed CSeq 1 and 2, and SETUP's own hardcoded
        literal (2) then collided with the CSeq the retry had already
        used -- this method is what makes that impossible, for any
        number of stages or retries, without any of them needing to
        know how many requests came before. Matches RFC 2326 SS12.17 and
        both Live555 (RTSPClient::fCSeq) and FFmpeg (rt->seq): a single
        per-connection counter, incremented once per actual request."""
        self._cseq += 1
        return self._cseq

    def connect(self) -> None:
        # Every stage below logs its own outcome (never credentials/
        # Authorization) as it happens, in addition to raising
        # ConnectionError with an explicit stage= tag on failure --
        # talk_audio_relay_client.py's own _start_session() already logs
        # str(error) verbatim on a connect() failure (session_id/
        # camera_id/camera_number/error), so between the two, production
        # logs show both the final outcome AND exactly how far the
        # handshake got before it failed.
        logger.info("talk_down_transport.connect_start host=%s port=%s path=%s", self.host, self.port, self.rtsp_path)
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout_seconds)
        uri = self._request_uri()

        describe = _authenticated_rtsp_request(self._sock, self._next_cseq, "DESCRIBE", uri, self.username, self.password, extra_headers="Accept: application/sdp\r\n")
        describe_status = _status_line(describe)
        logger.info("talk_down_transport.stage stage=describe_response host=%s port=%s status=%s", self.host, self.port, describe_status or "no_response")
        if " 200 " not in describe_status:
            raise ConnectionError(f"DESCRIBE failed (stage=describe_response): {describe_status or 'no response (connection closed before any data was received)'}")

        # SDP parsing: _find_backchannel_control() never raises -- an
        # empty/audio-section-less body just means setup_uri falls back
        # to the base RTSP uri (see its own docstring). _resolve_control
        # _uri() then correctly handles whichever of the three real-
        # world control-attribute shapes this camera used -- absolute,
        # leading-slash, or plain relative -- never naively concatenating.
        sdp_body = describe.split("\r\n\r\n", 1)[-1]
        audio_control = self._find_backchannel_control(sdp_body)
        setup_uri = _resolve_control_uri(uri, audio_control)
        logger.info("talk_down_transport.stage stage=sdp_parsing audio_control=%s setup_uri=%s", audio_control, setup_uri)

        self._rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rtp_socket.bind(("0.0.0.0", 0))
        local_port = self._rtp_socket.getsockname()[1]
        setup = _authenticated_rtsp_request(
            self._sock, self._next_cseq, "SETUP", setup_uri, self.username, self.password,
            extra_headers=f"Transport: RTP/AVP;unicast;client_port={local_port}-{local_port + 1}\r\n",
        )
        setup_status = _status_line(setup)
        logger.info("talk_down_transport.stage stage=setup_response host=%s port=%s status=%s", self.host, self.port, setup_status or "no_response")
        if " 200 " not in setup_status:
            raise ConnectionError(f"SETUP failed (stage=setup_response): {setup_status or 'no response (connection closed before any data was received)'}")

        self._session_id = self._extract_header(setup, "session").split(";")[0].strip()
        logger.info("talk_down_transport.stage stage=session_extraction session_id_present=%s", bool(self._session_id))
        if not self._session_id:
            raise ConnectionError("SETUP returned 200 but no Session header was present (stage=session_extraction)")
        self._rtp_remote = (self.host, self._extract_server_port(setup) or self.port)

        record = _authenticated_rtsp_request(
            self._sock, self._next_cseq, "RECORD", uri, self.username, self.password,
            extra_headers=f"Session: {self._session_id}\r\nRange: npt=0.000-\r\n",
        )
        record_status = _status_line(record)
        logger.info("talk_down_transport.stage stage=record_response host=%s port=%s status=%s", self.host, self.port, record_status or "no_response")
        if " 200 " not in record_status:
            raise ConnectionError(f"RECORD failed (stage=record_response): {record_status or 'no response (connection closed before any data was received)'}")
        logger.info("talk_down_transport.connect_succeeded host=%s port=%s", self.host, self.port)

    def send(self, encoded_audio: bytes) -> None:
        if self._rtp_socket is None or self._rtp_remote is None:
            raise RuntimeError("send() called before connect() established an RTP channel")
        packet = build_rtp_packet(encoded_audio, self._sequence, int(time.monotonic() * 8000) & 0xFFFFFFFF, self._ssrc)
        self._sequence += 1
        self._rtp_socket.sendto(packet, self._rtp_remote)

    def close(self) -> None:
        if self._sock is not None and self._session_id is not None:
            try:
                _rtsp_request(self._sock, self._next_cseq, "TEARDOWN", self._request_uri(), extra_headers=f"Session: {self._session_id}\r\n")
            except OSError:
                pass
        if self._sock is not None:
            self._sock.close()
        if self._rtp_socket is not None:
            self._rtp_socket.close()
        self._sock = None
        self._rtp_socket = None
        self._session_id = None

    @staticmethod
    def _find_backchannel_control(sdp_body: str) -> str | None:
        """Finds the 'a=control:' value of the audio media section
        closest to a sendonly/backchannel marker in the SDP, falling
        back to the first audio section's control if no explicit
        direction marker is present. Returns None (use the base RTSP
        URI as-is) if the SDP has no audio section at all."""
        lines = sdp_body.splitlines()
        in_audio = False
        control = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("m=audio"):
                in_audio = True
                control = None
                continue
            if stripped.startswith("m="):
                in_audio = False
                continue
            if in_audio and stripped.startswith("a=control:"):
                control = stripped[len("a=control:"):]
        return control

    @staticmethod
    def _extract_header(response: str, name: str) -> str:
        prefix = name.lower() + ":"
        for line in response.splitlines():
            if line.lower().startswith(prefix):
                return line.split(":", 1)[1].strip()
        return ""

    @classmethod
    def _extract_server_port(cls, setup_response: str) -> int | None:
        transport = cls._extract_header(setup_response, "transport")
        match = re.search(r"server_port=(\d+)-\d+", transport)
        return int(match.group(1)) if match else None
