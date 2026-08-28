"""Onboarding Stage 2, appliance side: verifies a cloud-issued camera
provisioning job against the actual device on this LAN, reusing the
existing discovery engine (discovery.scan(), ONVIF WS-Discovery + RTSP
port probing) -- no second discovery system, no ffmpeg. This agent
stays control-plane only, per docs/AI_HANDOFF.md: the VMS app is the
only process that runs FFmpeg or touches recordings; this module never
imports or shells out to either.

Credentials arrive once, in-memory, in the cloud's single-delivery
provisioning-jobs GET response (see appliance_cloud.py's
appliance_provisioning_jobs()), and are used only for the RTSP
credential check below -- never logged, never written to disk, never
included in any outgoing request. PortalClient.request() also strips
username/password/credentials/secret keys from every outgoing payload
as a second, independent layer of defense (see portal.py's sanitize()),
so even a caller mistake here can't leak them to the cloud.
"""

import base64
import hashlib
import re
import socket

from .discovery import scan


def locate_device(device_key, networks=None):
    """Re-scan the LAN (same engine as onboarding discovery, not a
    second one) and return the candidate whose own device_key matches,
    or None if it isn't currently reachable. device_key is stable
    across IP/DHCP changes for ONVIF and MAC-identified devices (see
    discovery.scan()); a device that has changed IP since the customer
    selected it in onboarding is still found here."""
    for candidate in scan(networks):
        if candidate.get('device_key') == device_key:
            return candidate
    return None


def _rtsp_request(method, ip, port, path='/', username=None, password=None, digest=None, timeout=3):
    """One RTSP request/response round-trip for the given method. No
    media stream is requested or received regardless of method --
    DESCRIBE only returns a session description (SDP text), never an
    actual stream; this never starts, and cannot be confused with, an
    actual recording/live connection (those belong to the VMS app's own
    FFmpeg pipeline)."""
    lines = [f'{method} rtsp://{ip}:{port}{path} RTSP/1.0', 'CSeq: 1']
    if method == 'DESCRIBE': lines.append('Accept: application/sdp')
    # digest alone is sufficient -- it's a fully-formed Authorization
    # value already (see _digest_header()), unlike the basic branch
    # below which still needs the raw username/password to build one.
    # Requiring username/password to ALSO be truthy here was a latent
    # bug: every digest-authenticated retry call site passes only
    # `digest`, so this condition silently sent the retry with NO
    # Authorization header at all -- never actually exercised until
    # classify_rtsp_authentication()'s digest regression test caught it.
    if digest:
        lines.append('Authorization: ' + digest)
    elif username and password:
        token = base64.b64encode(f'{username}:{password}'.encode()).decode()
        lines.append(f'Authorization: Basic {token}')
    request = ('\r\n'.join(lines) + '\r\n\r\n').encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((ip, port))
        sock.sendall(request)
        data = sock.recv(4096).decode(errors='ignore')
    finally:
        sock.close()
    status = re.match(r'RTSP/1\.0 (\d+)', data)
    code = int(status.group(1)) if status else 0
    challenge = None
    match = re.search(r'WWW-Authenticate:\s*(.+)', data, re.I)
    if match: challenge = match.group(1).strip()
    return code, challenge


def _rtsp_options(ip, port, path='/', username=None, password=None, digest=None, timeout=3):
    """Minimal RTSP OPTIONS round-trip -- a capability handshake only,
    kept for pure reachability probing. Many RTSP servers (confirmed
    live against a real camera during onboarding validation) accept
    OPTIONS unauthenticated even when the actual media session requires
    credentials, so this alone cannot be used to verify a credential --
    see classify_rtsp_authentication()'s DESCRIBE-based check for that."""
    return _rtsp_request('OPTIONS', ip, port, path, username, password, digest, timeout)


def _digest_header(username, password, realm, nonce, method, uri):
    ha1 = hashlib.md5(f'{username}:{realm}:{password}'.encode()).hexdigest()
    ha2 = hashlib.md5(f'{method}:{uri}'.encode()).hexdigest()
    response = hashlib.md5(f'{ha1}:{nonce}:{ha2}'.encode()).hexdigest()
    return (f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}"')


# Four distinguishable outcomes classify_rtsp_authentication() can
# return -- plain string literals, matching this codebase's existing
# convention for status values (see discovery.py's connection_status,
# camera_scan_jobs.status, etc.) rather than introducing an enum type
# not used anywhere else in this module.
UNREACHABLE = 'unreachable'
NO_AUTH_REQUIRED = 'no_auth_required'
AUTH_CORRECT = 'auth_correct'
AUTH_INCORRECT = 'auth_incorrect'


def classify_rtsp_authentication(ip, port, username, password, path='/', timeout=3):
    """Issues a real RTSP DESCRIBE -- the request that actually asks for
    the stream's session description, not just a capability handshake
    like OPTIONS. Confirmed live during Samsung onboarding validation:
    OPTIONS returned 200 unauthenticated on a camera whose media session
    still requires credentials, so OPTIONS alone cannot verify a
    credential is correct, only that *something* is listening.

    Returns (status, detail), distinguishing all four real outcomes:
      - UNREACHABLE: connection failed, or the device gave a response
        this client can't interpret as authenticating anything.
      - NO_AUTH_REQUIRED: DESCRIBE succeeded without ever being
        challenged -- this device's media session has no credential
        gate to verify.
      - AUTH_CORRECT: the device challenged (401 + WWW-Authenticate),
        and the supplied credentials were accepted on retry.
      - AUTH_INCORRECT: the device challenged, and the supplied
        credentials were rejected on retry -- or the device requires
        auth but none was supplied at all, which is equally "not
        verified as correct".
    detail is always a secret-free, human-readable string, never the
    credentials or a raw device response. Never reports AUTH_CORRECT
    without a real 200 from the device on the authenticated retry."""
    try:
        code, challenge = _rtsp_request('DESCRIBE', ip, port, path, timeout=timeout)
    except OSError as error:
        return UNREACHABLE, f'Device unreachable: {error.__class__.__name__}'
    if code == 200:
        return NO_AUTH_REQUIRED, 'Device accepted DESCRIBE without credentials.'
    if code != 401 or not challenge:
        return UNREACHABLE, f'Unexpected device response (RTSP {code or "no response"}).'
    if not username and not password:
        return AUTH_INCORRECT, 'Device requires authentication but no credentials were provided.'
    if 'digest' in challenge.lower():
        realm_match = re.search(r'realm="([^"]+)"', challenge)
        nonce_match = re.search(r'nonce="([^"]+)"', challenge)
        if not (realm_match and nonce_match):
            return UNREACHABLE, 'Device sent an unparseable Digest challenge.'
        uri = f'rtsp://{ip}:{port}{path}'
        digest = _digest_header(username, password, realm_match.group(1), nonce_match.group(1), 'DESCRIBE', uri)
        try:
            code2, _ = _rtsp_request('DESCRIBE', ip, port, path, digest=digest, timeout=timeout)
        except OSError as error:
            return UNREACHABLE, f'Device unreachable on retry: {error.__class__.__name__}'
    else:
        try:
            code2, _ = _rtsp_request('DESCRIBE', ip, port, path, username=username, password=password, timeout=timeout)
        except OSError as error:
            return UNREACHABLE, f'Device unreachable on retry: {error.__class__.__name__}'
    if code2 == 200:
        return AUTH_CORRECT, 'Credentials verified against the device via DESCRIBE.'
    return AUTH_INCORRECT, 'Device rejected the provided credentials.'


def verify_rtsp_credentials(ip, port, username, password, path='/', timeout=3):
    """Backward-compatible (ok, detail) wrapper around
    classify_rtsp_authentication() for verify_device()'s existing
    success/message contract (which appliance_submit_provisioning()'s
    payload and this module's callers depend on unchanged). ok is True
    for both NO_AUTH_REQUIRED and AUTH_CORRECT -- either way the device
    is reachable and accepting this camera's entry, which is everything
    provisioning itself has ever needed to know; UNREACHABLE and
    AUTH_INCORRECT are both False. Use classify_rtsp_authentication()
    directly when the specific state (not just pass/fail) matters."""
    status, detail = classify_rtsp_authentication(ip, port, username, password, path, timeout)
    return status in (NO_AUTH_REQUIRED, AUTH_CORRECT), detail


def verify_device(device_key, credentials, networks=None):
    """Top-level entry point used by the provisioning poller
    (service.py's poll_provisioning()). Returns (success, message);
    message is always safe to log and to send back to the cloud as-is
    -- it never contains a credential, an IP, or a MAC address."""
    device = locate_device(device_key, networks)
    if not device:
        return False, 'Device is no longer reachable on the network.'
    if not device.get('rtsp_support'):
        # ONVIF-only presence, no RTSP port open -- confirmed reachable,
        # nothing further to verify without an RTSP endpoint to test.
        return True, 'Device is reachable; no RTSP credential check applicable.'
    username = str((credentials or {}).get('username', ''))
    password = str((credentials or {}).get('password', ''))
    if not username and not password:
        return True, 'Device is reachable; no credentials were provided to verify.'
    return verify_rtsp_credentials(device['ip'], 554, username, password)
