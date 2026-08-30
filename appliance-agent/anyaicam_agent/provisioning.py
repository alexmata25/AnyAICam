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
import logging
import re
import secrets
import socket

from .discovery import scan

_LOG = logging.getLogger('anyaicam.agent')


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


# RFC 2617 challenge parameters can be quoted (realm="x") or unquoted
# (algorithm=MD5) -- real camera firmware uses both forms, sometimes in
# the same challenge. Matches either: a quoted value (group 2) or a
# bare token up to the next comma/whitespace (group 3).
_CHALLENGE_PARAM_RE = re.compile(r'([A-Za-z0-9_-]+)\s*=\s*(?:"([^"]*)"|([^\s,]+))')


def _parse_challenge_params(challenge):
    """Parses the parameters following the scheme name in a
    WWW-Authenticate header (realm, nonce, qop, algorithm, opaque, ...)
    into a plain dict, keyed lower-case. Handles both quoted and
    unquoted parameter forms safely -- a value that fails to match
    either form is simply absent from the result, never raises."""
    params = {}
    for match in _CHALLENGE_PARAM_RE.finditer(challenge):
        key = match.group(1).lower()
        value = match.group(2) if match.group(2) is not None else match.group(3)
        params[key] = value
    return params


def _select_qop(qop_value):
    """RFC 2617 qop may list multiple space-or-comma-separated options
    (e.g. `qop="auth,auth-int"`); this client implements only 'auth'
    (message-integrity/'auth-int' would additionally require hashing
    the request body, never needed for the bodyless DESCRIBE requests
    this module ever sends). Returns 'auth' when it's among the
    offered options, else None -- including when qop_value is falsy,
    which the caller treats identically to "no qop offered at all"
    (the legacy, pre-RFC-2617 Digest fallback)."""
    if not qop_value:
        return None
    options = [item.strip().lower() for item in qop_value.replace(',', ' ').split()]
    return 'auth' if 'auth' in options else None


def _digest_header(username, password, realm, nonce, method, uri, qop=None, cnonce=None, nc='00000001'):
    """Builds a WWW-Authenticate: Digest response. With qop=None,
    computes the legacy (pre-RFC-2617 / RFC 2069) formula --
    response=MD5(HA1:nonce:HA2) -- preserved unchanged for older
    cameras that challenge without a qop parameter at all. With
    qop='auth' (the only qop this client supports -- see
    _select_qop()), computes the RFC 2617 qop-aware formula --
    response=MD5(HA1:nonce:nc:cnonce:qop:HA2) -- and includes qop
    (unquoted, per RFC 2617), nc, and cnonce in the returned header,
    which many cameras (including the one that prompted this fix)
    require and silently reject a response missing them, regardless of
    whether the password itself is correct."""
    ha1 = hashlib.md5(f'{username}:{realm}:{password}'.encode()).hexdigest()
    ha2 = hashlib.md5(f'{method}:{uri}'.encode()).hexdigest()
    if qop:
        response = hashlib.md5(f'{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}'.encode()).hexdigest()
        return (f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
                f'uri="{uri}", qop={qop}, nc={nc}, cnonce="{cnonce}", response="{response}"')
    response = hashlib.md5(f'{ha1}:{nonce}:{ha2}'.encode()).hexdigest()
    return (f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}"')


# Six distinguishable outcomes classify_rtsp_authentication() can
# return -- plain string literals, matching this codebase's existing
# convention for status values (see discovery.py's connection_status,
# camera_scan_jobs.status, etc.) rather than introducing an enum type
# not used anywhere else in this module. UNSUPPORTED_CHALLENGE and
# MALFORMED_RESPONSE were split out of what used to be a single
# overloaded UNREACHABLE bucket -- confirmed live (Samsung, camera
# device_key ...f6af) that a qop="auth" Digest challenge was being
# answered with the legacy, qop-less formula, which the camera
# correctly rejected as an invalid response regardless of the password
# -- indistinguishable, before this fix, from an actually wrong
# password. Both new states still count as "not verified as correct"
# in verify_rtsp_credentials()'s ok/not-ok wrapper (failing closed,
# same as before) while letting the caller's message explain *why*.
UNREACHABLE = 'unreachable'
NO_AUTH_REQUIRED = 'no_auth_required'
AUTH_CORRECT = 'auth_correct'
AUTH_INCORRECT = 'auth_incorrect'
UNSUPPORTED_CHALLENGE = 'unsupported_challenge'
MALFORMED_RESPONSE = 'malformed_response'


def _log_rtsp_diagnostic(diag):
    """TEMPORARY: logs exactly one 'rtsp_auth_diagnostic' line per
    classify_rtsp_authentication() call, at INFO level via the same
    'anyaicam.agent' logger service.py already writes to (lands in
    /var/log/anyaicam/agent.log). Fields, all secret-free:
      scheme          -- 'basic', 'digest', an unsupported scheme name
                         reported by the device, or None (never
                         challenged / connection failed before then).
      qop_offered     -- True/False/None: whether the Digest challenge
                         included a qop parameter at all.
      qop_selected    -- 'auth' if this client selected it, else None
                         (including when qop_offered is False).
      path            -- the RTSP request path/URI this attempt used
                         (e.g. /Streaming/Channels/101) -- a resource
                         path, never a credential.
      first_status    -- the initial, unauthenticated DESCRIBE's RTSP
                         status code.
      retry_status    -- the authenticated retry's RTSP status code,
                         or None if no retry was attempted.
      algorithm       -- the Digest challenge's algorithm parameter
                         (defaults to 'MD5' when the challenge omits
                         it), or None for a non-Digest/no challenge.
      challenge_issue -- a short reason tag when the challenge itself
                         was malformed or unsupported (e.g.
                         'unsupported_qop', 'missing_realm_or_nonce'),
                         else None.
      outcome         -- the six-way classify_rtsp_authentication()
                         status this attempt concluded with.
    Never logs username, password, the Authorization header, a nonce/
    cnonce, or a computed response hash -- none of those are ever put
    into `diag` in the first place (see classify_rtsp_authentication()
    below), so there is nothing to redact here, only to assemble."""
    _LOG.info(
        'rtsp_auth_diagnostic scheme=%s qop_offered=%s qop_selected=%s path=%s '
        'first_status=%s retry_status=%s algorithm=%s challenge_issue=%s outcome=%s',
        diag.get('scheme'), diag.get('qop_offered'), diag.get('qop_selected'), diag.get('path'),
        diag.get('first_status'), diag.get('retry_status'), diag.get('algorithm'),
        diag.get('challenge_issue'), diag.get('outcome'),
    )


def classify_rtsp_authentication(ip, port, username, password, path='/', timeout=3):
    """Issues a real RTSP DESCRIBE -- the request that actually asks for
    the stream's session description, not just a capability handshake
    like OPTIONS. Confirmed live during Samsung onboarding validation:
    OPTIONS returned 200 unauthenticated on a camera whose media session
    still requires credentials, so OPTIONS alone cannot verify a
    credential is correct, only that *something* is listening.

    Returns (status, detail), distinguishing all six real outcomes:
      - UNREACHABLE: a real connection/timeout failure (socket-level
        OSError) talking to the device at all.
      - NO_AUTH_REQUIRED: DESCRIBE succeeded without ever being
        challenged -- this device's media session has no credential
        gate to verify.
      - AUTH_CORRECT: the device challenged (401 + WWW-Authenticate),
        and the supplied credentials were accepted on retry (a real
        200 from the device -- never reported without one).
      - AUTH_INCORRECT: the challenge was fully understood and
        answered per protocol, but the device still rejected it (401
        again) on retry -- or the device requires auth but no
        credentials were supplied at all. This is the only state that
        should ever be read as "the password is (probably) wrong."
      - UNSUPPORTED_CHALLENGE: the device's challenge uses a scheme,
        Digest algorithm, or qop option this client doesn't implement
        (e.g. Digest with only qop="auth-int" offered, or a non-MD5
        algorithm) -- fails closed (never verified as correct) without
        ever claiming the credentials themselves were wrong.
      - MALFORMED_RESPONSE: the device responded, but with something
        that can't be interpreted as authenticating anything at all
        (a 401 with no challenge header, an unparseable Digest missing
        realm/nonce, or an unexpected status code on either request).
    detail is always a secret-free, human-readable string -- never a
    credential, an Authorization header, a nonce/cnonce, or the raw
    device response. Never reports AUTH_CORRECT without a real 200
    from the device on the authenticated retry.

    TEMPORARY DIAGNOSTIC (remove once the live RTSP incompatibility
    behind repeated Samsung "Device rejected the provided credentials"
    results -- even after the qop fix and the /Streaming/Channels/101
    path fix -- is identified): every return path here logs one
    'rtsp_auth_diagnostic' line via _log_rtsp_diagnostic(), recording
    only protocol shape -- scheme, whether/which qop was selected, the
    request path, both status codes, the Digest algorithm name, and
    which challenge problem (if any) was hit. Never the username,
    password, Authorization header, nonce, cnonce, or response hash --
    see _log_rtsp_diagnostic()'s own docstring for the exact field
    list."""
    diag = {'path': path, 'scheme': None, 'qop_offered': None, 'qop_selected': None,
            'first_status': None, 'retry_status': None, 'algorithm': None, 'challenge_issue': None}

    def done(status, detail):
        diag['outcome'] = status
        _log_rtsp_diagnostic(diag)
        return status, detail

    try:
        code, challenge = _rtsp_request('DESCRIBE', ip, port, path, timeout=timeout)
    except OSError as error:
        return done(UNREACHABLE, f'Device unreachable: {error.__class__.__name__}')
    diag['first_status'] = code
    if code == 200:
        return done(NO_AUTH_REQUIRED, 'Device accepted DESCRIBE without credentials.')
    if code != 401:
        diag['challenge_issue'] = 'unexpected_first_status'
        return done(MALFORMED_RESPONSE, f'Unexpected device response (RTSP {code or "no response"}).')
    if not challenge:
        diag['challenge_issue'] = 'no_challenge_header'
        return done(MALFORMED_RESPONSE, 'Device returned 401 without an authentication challenge.')
    if not username and not password:
        return done(AUTH_INCORRECT, 'Device requires authentication but no credentials were provided.')
    scheme_word = challenge.split(None, 1)[0] if challenge.split() else ''
    scheme = scheme_word.lower()
    diag['scheme'] = scheme or scheme_word or 'unknown'
    if scheme == 'basic':
        digest = None
    elif scheme == 'digest':
        params = _parse_challenge_params(challenge)
        realm = params.get('realm')
        nonce = params.get('nonce')
        if not realm or not nonce:
            diag['challenge_issue'] = 'missing_realm_or_nonce'
            return done(UNSUPPORTED_CHALLENGE, 'Device sent a Digest challenge missing realm or nonce.')
        algorithm = (params.get('algorithm') or 'MD5').strip()
        diag['algorithm'] = algorithm
        if algorithm.upper() != 'MD5':
            diag['challenge_issue'] = 'unsupported_algorithm'
            return done(UNSUPPORTED_CHALLENGE, f'Device requires an unsupported Digest algorithm ({algorithm}).')
        qop_raw = params.get('qop')
        diag['qop_offered'] = bool(qop_raw)
        qop = _select_qop(qop_raw)
        diag['qop_selected'] = qop
        if qop_raw and qop is None:
            diag['challenge_issue'] = 'unsupported_qop'
            return done(UNSUPPORTED_CHALLENGE, 'Device only offers unsupported qop options (no "auth").')
        uri = f'rtsp://{ip}:{port}{path}'
        if qop:
            digest = _digest_header(username, password, realm, nonce, 'DESCRIBE', uri, qop=qop, cnonce=secrets.token_hex(8), nc='00000001')
        else:
            digest = _digest_header(username, password, realm, nonce, 'DESCRIBE', uri)
    else:
        diag['challenge_issue'] = 'unsupported_scheme'
        return done(UNSUPPORTED_CHALLENGE, f'Device sent an unsupported authentication scheme ({scheme_word or "unknown"}).')
    try:
        if digest:
            code2, _ = _rtsp_request('DESCRIBE', ip, port, path, digest=digest, timeout=timeout)
        else:
            code2, _ = _rtsp_request('DESCRIBE', ip, port, path, username=username, password=password, timeout=timeout)
    except OSError as error:
        return done(UNREACHABLE, f'Device unreachable on retry: {error.__class__.__name__}')
    diag['retry_status'] = code2
    if code2 == 200:
        return done(AUTH_CORRECT, 'Credentials verified against the device via DESCRIBE.')
    if code2 == 401:
        return done(AUTH_INCORRECT, 'Device rejected the provided credentials.')
    diag['challenge_issue'] = 'unexpected_retry_status'
    return done(MALFORMED_RESPONSE, f'Unexpected device response on authenticated retry (RTSP {code2 or "no response"}).')


def verify_rtsp_credentials(ip, port, username, password, path='/', timeout=3):
    """Backward-compatible (ok, detail) wrapper around
    classify_rtsp_authentication() for verify_device()'s existing
    success/message contract (which appliance_submit_provisioning()'s
    payload and this module's callers depend on unchanged). ok is True
    for both NO_AUTH_REQUIRED and AUTH_CORRECT -- either way the device
    is reachable and accepting this camera's entry, which is everything
    provisioning itself has ever needed to know; every other state
    (UNREACHABLE, AUTH_INCORRECT, UNSUPPORTED_CHALLENGE,
    MALFORMED_RESPONSE) is False -- failing closed the same way
    regardless of which of those four applies. Use
    classify_rtsp_authentication() directly when the specific state
    (not just pass/fail) matters, e.g. to tell a customer their
    password is probably wrong (AUTH_INCORRECT) apart from a device/
    protocol-level problem this client can't verify through at all
    (the other three)."""
    status, detail = classify_rtsp_authentication(ip, port, username, password, path, timeout)
    return status in (NO_AUTH_REQUIRED, AUTH_CORRECT), detail


# Confirmed live (Samsung, camera device_key ...f6af, verified read-only
# against the codebase's own conventions before this fix): verify_device()
# never passed a `path` to verify_rtsp_credentials() at all, so every
# real-camera verification silently tested authentication against the
# RTSP server's bare root ('/', verify_rtsp_credentials()'s own generic
# default for a caller that only wants a reachability probe) instead of
# an actual stream resource. RFC 2617's HA2=MD5(method:uri) binds the
# Digest response to the specific request URI, and many camera RTSP
# stacks additionally scope authorization per-resource -- a
# mathematically correct response for '/' can still be rejected because
# '/' was never the resource the account is authorized for, independent
# of the qop fix above. discovery.py's candidate records carry no
# per-device stream path at all (nothing upstream has ever resolved
# one), so there is no "discovered" path to use yet -- this reuses the
# exact same default this codebase already assumes everywhere else a
# path isn't independently known (main.py's camera_url() legacy
# fallback, and the Ryzen appliance's own CAMERA{n}_PATH default),
# confirmed correct for this exact camera via FFmpeg on Ryzen. A real,
# per-device discovered path (once one exists) should take priority
# over this default, not replace it as the fallback.
DEFAULT_RTSP_STREAM_PATH = '/Streaming/Channels/101'


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
    # A per-device discovered path always wins once discovery.py can
    # supply one; falls back to the fleet's known-good default above.
    path = device.get('rtsp_path') or DEFAULT_RTSP_STREAM_PATH
    return verify_rtsp_credentials(device['ip'], 554, username, password, path=path)
