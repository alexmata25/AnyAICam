"""Read-only ONVIF media-profile resolution: GetCapabilities (to locate
the Media service) -> GetProfiles -> GetStreamUri. Closes the confirmed-
live Samsung gap where cameras.onvif_endpoint (the field camera_url()'s
_provisioned_camera_stream() actually reads -- see app/main.py) was
never populated by anything, so camera_url() always fell through to an
unset legacy CAMERA{n}_HOST env var and raised CameraNotConfiguredError
before FFmpeg was ever attempted.

Runs entirely from the appliance side (this agent has direct LAN
reachability to the camera; the cloud/VMS process does not need it and
never should). Never modifies any camera setting -- every ONVIF call
here is a read-only Get*/Probe operation. Never guesses an RTSP path:
the URI returned is exactly what the camera's own GetStreamUri response
says, unmodified except for stripping any embedded credentials before
it's ever logged or persisted (see sanitize_rtsp_uri()).

device_key is the identity this whole module is built around: the WS-
Discovery probe used to locate the device's XAddrs only accepts a reply
whose own advertised endpoint reference matches device_key exactly (see
_probe_xaddr()), so a result can never be misattributed to the wrong
physical camera even if IPs on the LAN have shifted. No username,
password, Authorization header, or credential-bearing URI is ever
included in a return value, a log line, or an exception message --
resolve_media_uri()'s result dict is safe to log/print/send to the
cloud as-is.

Deliberately stdlib-only (urllib + xml.etree), matching this package's
existing style (discovery.py, provisioning.py) -- no new dependency for
a handful of small SOAP calls.
"""

import re
import socket
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

DEFAULT_TIMEOUT = 5
DISCOVERY_TIMEOUT = 2
MAX_MESSAGE_CHARS = 300  # bound on any fault/error text before it's logged

_WSDD_MULTICAST_ADDRESS = ('239.255.255.250', 3702)


def _local_name(tag: str) -> str:
    """Strips the XML namespace off a tag, e.g. '{http://...}GetProfiles'
    -> 'GetProfiles'. ONVIF namespace URIs are standardized but the
    prefixes devices choose are not, so every lookup in this module
    matches by local name rather than depending on a specific prefix."""
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def _find(root, *local_names):
    """Depth-first search for the first descendant (or root itself)
    whose tag chain matches local_names in order -- e.g. _find(root,
    'Body', 'GetProfilesResponse') -- without caring which namespace
    prefix the device used."""
    node = root
    for wanted in local_names:
        found = None
        for child in node.iter():
            if _local_name(child.tag) == wanted:
                found = child
                break
        if found is None:
            return None
        node = found
    return node


def _find_all(root, local_name):
    return [element for element in root.iter() if _local_name(element.tag) == local_name]


def _soap_envelope(body_xml: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        f'<s:Body>{body_xml}</s:Body></s:Envelope>'
    )


class OnvifAuthRequired(Exception):
    """Raised internally when the device challenges an unauthenticated
    request. Never carries a credential -- only a fixed, safe reason."""


class OnvifRequestError(Exception):
    """Raised internally for any other request failure (unreachable,
    malformed response, SOAP fault unrelated to auth). Message is
    always safe to log -- see _soap_post()/_parse_fault_reason()."""


def _parse_fault_reason(body: bytes) -> str | None:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None
    reason = _find(root, 'Fault', 'Text')
    if reason is not None and reason.text:
        return reason.text.strip()[:MAX_MESSAGE_CHARS]
    code = _find(root, 'Fault', 'Value')
    return code.text.strip()[:MAX_MESSAGE_CHARS] if code is not None and code.text else None


def _looks_like_auth_fault(reason: str | None) -> bool:
    if not reason:
        return False
    lowered = reason.lower()
    return any(marker in lowered for marker in ('notauthorized', 'not authorized', 'authenticationfailed', 'unauthorized'))


def _soap_post(url: str, body_xml: str, timeout: float) -> ElementTree.Element:
    """One unauthenticated SOAP request/response round-trip. Raises
    OnvifAuthRequired on a 401/403 or an auth-shaped SOAP fault,
    OnvifRequestError on anything else that prevents getting a usable
    response. Never sends or logs a credential -- this module never
    even has one to send; see the module docstring for why an
    authentication challenge here means "stop", not "retry with
    credentials"."""
    envelope = _soap_envelope(body_xml)
    request = urllib.request.Request(
        url,
        data=envelope.encode('utf-8'),
        headers={'Content-Type': 'application/soap+xml; charset=utf-8'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        body = b''
        try:
            body = error.read()
        except OSError:
            pass
        if error.code in (401, 403):
            raise OnvifAuthRequired(f'HTTP {error.code}') from error
        reason = _parse_fault_reason(body)
        if _looks_like_auth_fault(reason):
            raise OnvifAuthRequired(reason or f'HTTP {error.code}') from error
        raise OnvifRequestError(reason or f'HTTP {error.code}') from error
    except (urllib.error.URLError, socket.timeout, TimeoutError) as error:
        raise OnvifRequestError(f'unreachable: {type(error).__name__}') from error
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise OnvifRequestError('malformed_response') from error
    fault = _find(root, 'Fault')
    if fault is not None:
        reason = _parse_fault_reason(payload)
        if _looks_like_auth_fault(reason):
            raise OnvifAuthRequired(reason or 'soap_fault')
        raise OnvifRequestError(reason or 'soap_fault')
    return root


# --------------------------------------------------------------- discovery

def device_service_url(ip: str, xaddr: str | None = None) -> str:
    """The camera's own advertised XAddr (from a fresh, targeted WS-
    Discovery probe -- see _probe_xaddr()) is always preferred when
    available; it's the device telling us where its Device service
    actually is, not a guess. Only when no fresh XAddr could be
    obtained does this fall back to the single location the ONVIF
    Core Specification itself designates as the well-known Device
    service path -- a protocol-standard default, not a vendor-specific
    RTSP-style guess."""
    if xaddr and xaddr.lower().startswith(('http://', 'https://')):
        return xaddr
    return f'http://{ip}/onvif/device_service'


def _probe_xaddr(ip: str, device_key: str, timeout: float = DISCOVERY_TIMEOUT) -> str | None:
    """A single, passive WS-Discovery multicast Probe -- identical in
    kind to discovery.py's own onboarding probe, never a second,
    heavier mechanism -- kept only long enough to find the ONE reply
    whose own advertised endpoint reference equals device_key exactly.
    Never returns an XAddr for any other device, even one that
    replies from the same IP address (a device_key mismatch is treated
    as no match at all)."""
    message = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f'<e:Header><w:MessageID>uuid:{uuid.uuid4()}</w:MessageID>'
        '<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        '<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>'
        '<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>'
    ).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.25)
    try:
        sock.sendto(message, _WSDD_MULTICAST_ADDRESS)
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, address = sock.recvfrom(65535)
            except socket.timeout:
                continue
            if address[0] != ip:
                continue
            text = data.decode(errors='ignore')
            endpoint = re.search(r'<[^>]*Address[^>]*>\s*(urn:uuid:[^\s<]+)', text, re.I)
            if not endpoint or endpoint.group(1) != device_key:
                continue  # a reply from this IP, but not the physical device we mean to associate this result with
            xaddrs = re.search(r'<[^>]*XAddrs[^>]*>\s*([^<]+)</', text, re.I)
            if xaddrs:
                return xaddrs.group(1).split()[0]
            return None
    except OSError:
        return None
    finally:
        sock.close()
    return None


# ------------------------------------------------------------- SOAP calls

def get_media_service_url(device_url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    root = _soap_post(device_url, '<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>', timeout)
    media = _find(root, 'Media', 'XAddr')
    if media is None or not (media.text or '').strip():
        raise OnvifRequestError('no_media_service_advertised')
    return media.text.strip()


def get_profiles(media_url: str, timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    root = _soap_post(media_url, '<trt:GetProfiles/>', timeout)
    profiles = []
    for element in _find_all(root, 'Profiles'):
        token = element.get('token') or element.get('Token')
        if not token:
            continue
        name_element = _find(element, 'Name')
        name = (name_element.text or '').strip() if name_element is not None and name_element.text else ''
        profiles.append({'token': token, 'name': name})
    return profiles


def select_profile(profiles: list[dict]) -> dict | None:
    """Prefers a profile whose own name or token clearly identifies it
    as the main/primary stream (the common 'MainStream'/'Profile_1
    (Main)' naming convention). ONVIF's core Media service has no
    formal is-default flag on GetProfilesResponse, so absent that
    naming signal, the first profile in the device's own returned
    order is used -- by near-universal vendor convention (and ONVIF
    Profile S guidance) a device's first-listed profile is its
    primary/highest-quality one. Deterministic either way: the same
    GetProfilesResponse always yields the same selection."""
    if not profiles:
        return None
    for profile in profiles:
        haystack = f"{profile.get('name', '')} {profile.get('token', '')}".lower()
        if 'main' in haystack:
            return profile
    return profiles[0]


def get_stream_uri(media_url: str, profile_token: str, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    body = (
        '<trt:GetStreamUri>'
        '<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>'
        '<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>'
        f'<trt:ProfileToken>{profile_token}</trt:ProfileToken></trt:GetStreamUri>'
    )
    root = _soap_post(media_url, body, timeout)
    uri = _find(root, 'MediaUri', 'Uri')
    return uri.text.strip() if uri is not None and uri.text else None


# --------------------------------------------------------------- sanitize

def sanitize_rtsp_uri(uri: str | None) -> tuple[str | None, bool]:
    """Returns (clean_uri, had_embedded_credentials). clean_uri is None
    for anything empty, unparseable, or not rtsp:// -- never a
    best-effort guess at what the caller 'probably meant'. Embedded
    userinfo (rtsp://user:pass@host/...) is always stripped before the
    URI is returned, logged, or persisted: credentials belong in
    encrypted credential storage (camera_credentials), never embedded
    in a stored/logged URI, regardless of whether this module supplied
    them (it never does) or the camera's own GetStreamUri response
    happened to include them."""
    if not isinstance(uri, str) or not uri.strip():
        return None, False
    try:
        parsed = urlsplit(uri.strip())
    except ValueError:
        return None, False
    if parsed.scheme.lower() != 'rtsp' or not parsed.hostname:
        return None, False
    had_credentials = bool(parsed.username or parsed.password)
    netloc = parsed.hostname
    if parsed.port:
        netloc += f':{parsed.port}'
    clean = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    return clean, had_credentials


# ------------------------------------------------------------- orchestration

def resolve_media_uri(ip: str, device_key: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """The single entry point the agent's sync cycle calls. Every
    return path yields a dict that is always safe to log, print, or
    submit to the cloud as-is -- never a username, password,
    Authorization header, or credential-bearing URI.

    status values:
      'resolved'       -- rtsp_uri is a real, sanitized rtsp:// URI from
                           this device's own GetStreamUri response.
      'auth_required'  -- the device challenged an unauthenticated
                           request; caller must stop, not guess
                           credentials (see module docstring).
      'no_uri'         -- profiles were retrieved but GetStreamUri
                           returned nothing usable.
      'invalid_uri'    -- GetStreamUri returned a non-rtsp:// or
                           unparseable value; never persisted as-is.
      'no_profiles'    -- GetProfiles returned zero profiles.
      'unreachable'    -- the device (or its Media service) could not
                           be reached at all within the timeout.
      'error'          -- any other non-auth failure; message has the
                           safe, capped reason.
    """
    result = {
        'device_key': device_key, 'status': 'error', 'rtsp_uri': None,
        'had_embedded_credentials': False, 'profile_token': None,
        'profile_name': None, 'profile_count': 0, 'message': '',
    }
    xaddr = _probe_xaddr(ip, device_key, timeout=DISCOVERY_TIMEOUT)
    device_url = device_service_url(ip, xaddr)
    try:
        media_url = get_media_service_url(device_url, timeout=timeout)
    except OnvifAuthRequired:
        result.update(status='auth_required', message='Device requires authentication for ONVIF Device service access.')
        return result
    except OnvifRequestError as error:
        status = 'unreachable' if str(error).startswith('unreachable') else 'error'
        result.update(status=status, message=str(error)[:MAX_MESSAGE_CHARS])
        return result

    try:
        profiles = get_profiles(media_url, timeout=timeout)
    except OnvifAuthRequired:
        result.update(status='auth_required', message='Device requires authentication for ONVIF Media service access.')
        return result
    except OnvifRequestError as error:
        status = 'unreachable' if str(error).startswith('unreachable') else 'error'
        result.update(status=status, message=str(error)[:MAX_MESSAGE_CHARS])
        return result

    result['profile_count'] = len(profiles)
    profile = select_profile(profiles)
    if profile is None:
        result.update(status='no_profiles', message='Device advertised a Media service but returned zero profiles.')
        return result
    result['profile_token'] = profile['token']
    result['profile_name'] = profile['name'] or None

    try:
        raw_uri = get_stream_uri(media_url, profile['token'], timeout=timeout)
    except OnvifAuthRequired:
        result.update(status='auth_required', message='Device requires authentication for ONVIF GetStreamUri.')
        return result
    except OnvifRequestError as error:
        status = 'unreachable' if str(error).startswith('unreachable') else 'error'
        result.update(status=status, message=str(error)[:MAX_MESSAGE_CHARS])
        return result

    if not raw_uri:
        result.update(status='no_uri', message=f"GetStreamUri returned no URI for profile '{profile['token']}'.")
        return result

    clean_uri, had_credentials = sanitize_rtsp_uri(raw_uri)
    if clean_uri is None:
        result.update(status='invalid_uri', message='GetStreamUri did not return a usable rtsp:// URI.')
        return result

    selection_note = 'main/primary' if 'main' in f"{profile['name']} {profile['token']}".lower() else 'first-listed'
    result.update(status='resolved', rtsp_uri=clean_uri, had_embedded_credentials=had_credentials,
                   message=f"Resolved via {selection_note} profile '{profile['token']}' ({len(profiles)} profile(s) seen).")
    return result
