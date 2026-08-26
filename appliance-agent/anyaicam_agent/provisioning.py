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


def _rtsp_options(ip, port, path='/', username=None, password=None, digest=None, timeout=3):
    """Minimal RTSP OPTIONS round-trip -- enough to learn whether the
    device is present and, when credentials are supplied, whether they
    are accepted. No media stream is requested or received; this never
    starts, and cannot be confused with, an actual recording/live
    connection (those belong to the VMS app's own FFmpeg pipeline)."""
    lines = [f'OPTIONS rtsp://{ip}:{port}{path} RTSP/1.0', 'CSeq: 1']
    if digest and username and password:
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


def _digest_header(username, password, realm, nonce, method, uri):
    ha1 = hashlib.md5(f'{username}:{realm}:{password}'.encode()).hexdigest()
    ha2 = hashlib.md5(f'{method}:{uri}'.encode()).hexdigest()
    response = hashlib.md5(f'{ha1}:{nonce}:{ha2}'.encode()).hexdigest()
    return (f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}"')


def verify_rtsp_credentials(ip, port, username, password, path='/', timeout=3):
    """Returns (ok, detail); detail is always a secret-free, human-
    readable string, never the credentials or a raw device response.
    Tries an unauthenticated OPTIONS first; if the device challenges,
    retries once with Basic or Digest per its own WWW-Authenticate
    header. Never reports success without a real device response."""
    try:
        code, challenge = _rtsp_options(ip, port, path, timeout=timeout)
    except OSError as error:
        return False, f'Device unreachable: {error.__class__.__name__}'
    if code == 200:
        return True, 'Device accepted connection without credentials.'
    if code != 401 or not challenge:
        return False, f'Unexpected device response (RTSP {code or "no response"}).'
    if 'digest' in challenge.lower():
        realm_match = re.search(r'realm="([^"]+)"', challenge)
        nonce_match = re.search(r'nonce="([^"]+)"', challenge)
        if not (realm_match and nonce_match):
            return False, 'Device sent an unparseable Digest challenge.'
        uri = f'rtsp://{ip}:{port}{path}'
        digest = _digest_header(username, password, realm_match.group(1), nonce_match.group(1), 'OPTIONS', uri)
        try:
            code2, _ = _rtsp_options(ip, port, path, digest=digest, timeout=timeout)
        except OSError as error:
            return False, f'Device unreachable on retry: {error.__class__.__name__}'
    else:
        try:
            code2, _ = _rtsp_options(ip, port, path, username=username, password=password, timeout=timeout)
        except OSError as error:
            return False, f'Device unreachable on retry: {error.__class__.__name__}'
    if code2 == 200:
        return True, 'Credentials verified against the device.'
    return False, 'Device rejected the provided credentials.'


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
