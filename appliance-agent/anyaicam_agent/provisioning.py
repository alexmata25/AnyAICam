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


# Confirmed by comparing against the known-working Ryzen appliance's
# FFmpeg/libavformat RTSP client (same camera, same
# /Streaming/Channels/101 path, same credentials): this module's
# original per-request "open a socket, send, read, close" model
# doesn't match how a real RTSP client behaves at all. FFmpeg keeps
# ONE persistent TCP connection open across the whole exchange
# (OPTIONS -> unauthenticated DESCRIBE -> authenticated DESCRIBE),
# with a strictly incrementing CSeq, an OPTIONS preflight, and a
# User-Agent header -- none of which the old per-call model did. Some
# embedded camera RTSP stacks scope a Digest nonce's validity to the
# connection it was issued on; a mathematically correct response
# presented on a *different* connection can be silently rejected,
# indistinguishable from a wrong credential from the outside -- which
# is exactly what kept happening here even after the qop and path
# fixes independently confirmed correct. _RtspSession replaces the old
# per-call socket helpers with this session-shaped model.
_RTSP_USER_AGENT = 'AnyAiCamAgent/1.0'


def _parse_rtsp_response(data):
    """Parses one raw RTSP response into (status_code, headers).
    headers is a plain dict keyed lower-case; the first occurrence of
    a repeated header name wins (this client only ever needs one
    WWW-Authenticate challenge at a time)."""
    lines = data.split('\r\n')
    status_match = re.match(r'RTSP/1\.0 (\d+)', lines[0]) if lines and lines[0] else None
    code = int(status_match.group(1)) if status_match else 0
    headers = {}
    for line in lines[1:]:
        if not line:
            break
        if ':' not in line:
            continue
        key, _, value = line.partition(':')
        key = key.strip().lower()
        if key not in headers:
            headers[key] = value.strip()
    return code, headers


class _RtspSession:
    """One persistent TCP connection spanning an entire RTSP
    authentication exchange -- OPTIONS, then an unauthenticated
    DESCRIBE, then an authenticated DESCRIBE, and (on a stale-nonce
    re-challenge) one further authenticated DESCRIBE, all on this same
    connection with a strictly incrementing CSeq, matching FFmpeg/
    libavformat's own session shape instead of this module's previous
    fresh-connection-per-request model.

    No media stream is ever requested or received regardless of
    method -- DESCRIBE only returns a session description (SDP text);
    this never starts, and cannot be confused with, an actual
    recording/live connection (those belong to the VMS app's own
    FFmpeg pipeline, never this control-plane-only agent)."""

    def __init__(self, ip, port, timeout=3):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect((ip, port))
        self._cseq = 0

    def request(self, method, uri, extra_headers=None):
        self._cseq += 1
        lines = [f'{method} {uri} RTSP/1.0', f'CSeq: {self._cseq}', f'User-Agent: {_RTSP_USER_AGENT}']
        if extra_headers:
            lines.extend(extra_headers)
        self._sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
        data = self._sock.recv(4096).decode(errors='ignore')
        return _parse_rtsp_response(data)

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


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


def _is_stale(params):
    """RFC 2617: a Digest challenge repeating the same failure may set
    stale="true" to mean "the nonce itself was valid but has expired --
    retry once with the fresh nonce this same challenge provides,
    without re-prompting for credentials" -- distinct from a genuine
    credential rejection. Matches FFmpeg/libavformat's own behavior of
    retrying exactly once on this signal. Value may be quoted or not;
    only a literal 'true' (case-insensitive) means retry."""
    return (params.get('stale') or '').strip().lower() == 'true'


def _digest_header(username, password, realm, nonce, method, uri, qop=None, cnonce=None, nc='00000001', algorithm=None, opaque=None):
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
    whether the password itself is correct.

    algorithm and opaque are echoed back exactly as the caller supplies
    them -- i.e. only when the challenge itself included them -- never
    invented when the server never mentioned either, matching FFmpeg's
    own behavior of echoing exactly what the server asked for."""
    ha1 = hashlib.md5(f'{username}:{realm}:{password}'.encode()).hexdigest()
    ha2 = hashlib.md5(f'{method}:{uri}'.encode()).hexdigest()
    if qop:
        response = hashlib.md5(f'{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}'.encode()).hexdigest()
        parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"',
                 f'qop={qop}', f'nc={nc}', f'cnonce="{cnonce}"', f'response="{response}"']
    else:
        response = hashlib.md5(f'{ha1}:{nonce}:{ha2}'.encode()).hexdigest()
        parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"', f'response="{response}"']
    if algorithm:
        parts.append(f'algorithm={algorithm}')
    if opaque:
        parts.append(f'opaque="{opaque}"')
    return 'Digest ' + ', '.join(parts)


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
      scheme             -- 'basic', 'digest', an unsupported scheme
                            name reported by the device, or None
                            (never challenged / connection failed
                            before then).
      qop_offered        -- True/False/None: whether the Digest
                            challenge included a qop parameter at all.
      qop_selected       -- 'auth' if this client selected it, else
                            None (including when qop_offered is False).
      path               -- the RTSP request path/URI this attempt
                            used (e.g. /Streaming/Channels/101) -- a
                            resource path, never a credential.
      first_status       -- the initial, unauthenticated DESCRIBE's
                            RTSP status code.
      retry_status       -- the first authenticated retry's RTSP
                            status code, or None if never attempted.
      algorithm          -- the Digest challenge's own algorithm
                            parameter, verbatim, or None when the
                            challenge didn't specify one.
      challenge_issue    -- a short reason tag when the challenge
                            itself was malformed or unsupported (e.g.
                            'unsupported_qop', 'missing_realm_or_nonce'),
                            else None.
      stale_retry        -- True if the device issued a stale="true"
                            re-challenge and this client retried once
                            with the new nonce, else False.
      stale_retry_status -- the stale-nonce retry's RTSP status code,
                            or None if no stale retry was attempted.
      outcome            -- the six-way classify_rtsp_authentication()
                            status this attempt concluded with.
    Never logs username, password, the Authorization header, a nonce/
    cnonce, or a computed response hash -- none of those are ever put
    into `diag` in the first place (see classify_rtsp_authentication()
    below), so there is nothing to redact here, only to assemble."""
    _LOG.info(
        'rtsp_auth_diagnostic scheme=%s qop_offered=%s qop_selected=%s path=%s '
        'first_status=%s retry_status=%s algorithm=%s challenge_issue=%s '
        'stale_retry=%s stale_retry_status=%s outcome=%s',
        diag.get('scheme'), diag.get('qop_offered'), diag.get('qop_selected'), diag.get('path'),
        diag.get('first_status'), diag.get('retry_status'), diag.get('algorithm'),
        diag.get('challenge_issue'), diag.get('stale_retry'), diag.get('stale_retry_status'),
        diag.get('outcome'),
    )


def _prepare_digest_or_basic(challenge, username, password, uri, diag):
    """Parses one WWW-Authenticate challenge and returns either
    ('ok', authorization_header_value) or ('fail', status, detail)
    when the challenge can't be answered. Updates `diag` in place
    (scheme/qop_offered/qop_selected/algorithm) exactly as
    classify_rtsp_authentication() always has. Shared between the
    initial challenge and a stale-nonce re-challenge -- both are
    answered the same way, just against a different nonce."""
    scheme_word = challenge.split(None, 1)[0] if challenge.split() else ''
    scheme = scheme_word.lower()
    diag['scheme'] = scheme or scheme_word or 'unknown'
    if scheme == 'basic':
        token = base64.b64encode(f'{username}:{password}'.encode()).decode()
        return 'ok', f'Basic {token}'
    if scheme != 'digest':
        diag['challenge_issue'] = 'unsupported_scheme'
        return 'fail', UNSUPPORTED_CHALLENGE, f'Device sent an unsupported authentication scheme ({scheme_word or "unknown"}).'
    params = _parse_challenge_params(challenge)
    realm = params.get('realm')
    nonce = params.get('nonce')
    if not realm or not nonce:
        diag['challenge_issue'] = 'missing_realm_or_nonce'
        return 'fail', UNSUPPORTED_CHALLENGE, 'Device sent a Digest challenge missing realm or nonce.'
    algorithm = params.get('algorithm')
    diag['algorithm'] = algorithm
    if algorithm and algorithm.strip().upper() != 'MD5':
        diag['challenge_issue'] = 'unsupported_algorithm'
        return 'fail', UNSUPPORTED_CHALLENGE, f'Device requires an unsupported Digest algorithm ({algorithm}).'
    qop_raw = params.get('qop')
    diag['qop_offered'] = bool(qop_raw)
    qop = _select_qop(qop_raw)
    diag['qop_selected'] = qop
    if qop_raw and qop is None:
        diag['challenge_issue'] = 'unsupported_qop'
        return 'fail', UNSUPPORTED_CHALLENGE, 'Device only offers unsupported qop options (no "auth").'
    opaque = params.get('opaque')
    if qop:
        digest = _digest_header(username, password, realm, nonce, 'DESCRIBE', uri, qop=qop, cnonce=secrets.token_hex(8), nc='00000001', algorithm=algorithm, opaque=opaque)
    else:
        digest = _digest_header(username, password, realm, nonce, 'DESCRIBE', uri, algorithm=algorithm, opaque=opaque)
    return 'ok', digest


def classify_rtsp_authentication(ip, port, username, password, path='/', timeout=3):
    """Issues a real RTSP DESCRIBE -- the request that actually asks for
    the stream's session description, not just a capability handshake
    like OPTIONS. Confirmed live during Samsung onboarding validation:
    a bare OPTIONS returned 200 unauthenticated on a camera whose media
    session still requires credentials, so OPTIONS alone cannot verify
    a credential is correct, only that *something* is listening.

    Matches the known-working Ryzen/FFmpeg exchange for this same
    camera and path (confirmed by direct comparison): one persistent
    TCP connection (_RtspSession) spans OPTIONS -> unauthenticated
    DESCRIBE -> authenticated DESCRIBE -> (on a stale-nonce
    re-challenge) one further authenticated DESCRIBE, with a strictly
    incrementing CSeq, a normal User-Agent, and Accept: application/sdp
    on every DESCRIBE -- never a fresh connection per request as
    before. OPTIONS's own result is never used to decide anything;
    it's sent purely to match the session shape a real RTSP client
    establishes, since some embedded camera RTSP stacks don't fully
    arm challenge/session state for a client that skips straight to
    DESCRIBE.

    Returns (status, detail), distinguishing all six real outcomes:
      - UNREACHABLE: a real connection/timeout failure (socket-level
        OSError) talking to the device at all.
      - NO_AUTH_REQUIRED: DESCRIBE succeeded without ever being
        challenged -- this device's media session has no credential
        gate to verify.
      - AUTH_CORRECT: the device challenged (401 + WWW-Authenticate),
        and the supplied credentials were accepted on retry (a real
        200 from the device -- never reported without one), including
        after exactly one stale-nonce re-challenge.
      - AUTH_INCORRECT: the challenge was fully understood and
        answered per protocol, but the device still rejected it (401
        again, and not a stale-nonce re-challenge) -- or the device
        requires auth but no credentials were supplied at all. This is
        the only state that should ever be read as "the password is
        (probably) wrong."
      - UNSUPPORTED_CHALLENGE: the device's challenge uses a scheme,
        Digest algorithm, or qop option this client doesn't implement
        (e.g. Digest with only qop="auth-int" offered, or a non-MD5
        algorithm) -- fails closed (never verified as correct) without
        ever claiming the credentials themselves were wrong.
      - MALFORMED_RESPONSE: the device responded, but with something
        that can't be interpreted as authenticating anything at all
        (a 401 with no challenge header, an unparseable Digest missing
        realm/nonce, or an unexpected status code on any request).
    detail is always a secret-free, human-readable string -- never a
    credential, an Authorization header, a nonce/cnonce, or the raw
    device response. Never reports AUTH_CORRECT without a real 200
    from the device on an authenticated retry.

    TEMPORARY DIAGNOSTIC (remove once the live RTSP incompatibility
    behind repeated Samsung "Device rejected the provided credentials"
    results is identified): every return path here logs one
    'rtsp_auth_diagnostic' line via _log_rtsp_diagnostic() -- see its
    own docstring for the exact, secret-free field list."""
    diag = {'path': path, 'scheme': None, 'qop_offered': None, 'qop_selected': None,
            'first_status': None, 'retry_status': None, 'algorithm': None,
            'challenge_issue': None, 'stale_retry': False, 'stale_retry_status': None}

    def done(status, detail):
        diag['outcome'] = status
        _log_rtsp_diagnostic(diag)
        return status, detail

    uri = f'rtsp://{ip}:{port}{path}'
    try:
        session = _RtspSession(ip, port, timeout=timeout)
    except OSError as error:
        return done(UNREACHABLE, f'Device unreachable: {error.__class__.__name__}')
    try:
        try:
            session.request('OPTIONS', uri)
        except OSError:
            pass  # a capability probe only -- its result never gates anything below
        try:
            code, headers = session.request('DESCRIBE', uri, extra_headers=['Accept: application/sdp'])
        except OSError as error:
            return done(UNREACHABLE, f'Device unreachable: {error.__class__.__name__}')
        diag['first_status'] = code
        if code == 200:
            return done(NO_AUTH_REQUIRED, 'Device accepted DESCRIBE without credentials.')
        if code != 401:
            diag['challenge_issue'] = 'unexpected_first_status'
            return done(MALFORMED_RESPONSE, f'Unexpected device response (RTSP {code or "no response"}).')
        challenge = headers.get('www-authenticate')
        if not challenge:
            diag['challenge_issue'] = 'no_challenge_header'
            return done(MALFORMED_RESPONSE, 'Device returned 401 without an authentication challenge.')
        if not username and not password:
            return done(AUTH_INCORRECT, 'Device requires authentication but no credentials were provided.')

        outcome = _prepare_digest_or_basic(challenge, username, password, uri, diag)
        if outcome[0] == 'fail':
            return done(outcome[1], outcome[2])
        auth_header = outcome[1]

        try:
            code2, headers2 = session.request('DESCRIBE', uri, extra_headers=['Accept: application/sdp', f'Authorization: {auth_header}'])
        except OSError as error:
            return done(UNREACHABLE, f'Device unreachable on retry: {error.__class__.__name__}')
        diag['retry_status'] = code2
        if code2 == 200:
            return done(AUTH_CORRECT, 'Credentials verified against the device via DESCRIBE.')
        if code2 == 401 and diag['scheme'] == 'digest':
            retry_challenge = headers2.get('www-authenticate')
            retry_params = _parse_challenge_params(retry_challenge) if retry_challenge else {}
            if retry_challenge and _is_stale(retry_params):
                diag['stale_retry'] = True
                stale_outcome = _prepare_digest_or_basic(retry_challenge, username, password, uri, diag)
                if stale_outcome[0] == 'fail':
                    return done(stale_outcome[1], stale_outcome[2])
                stale_auth_header = stale_outcome[1]
                try:
                    code3, _headers3 = session.request('DESCRIBE', uri, extra_headers=['Accept: application/sdp', f'Authorization: {stale_auth_header}'])
                except OSError as error:
                    return done(UNREACHABLE, f'Device unreachable on stale-nonce retry: {error.__class__.__name__}')
                diag['stale_retry_status'] = code3
                if code3 == 200:
                    return done(AUTH_CORRECT, 'Credentials verified against the device via DESCRIBE (after a stale-nonce retry).')
                if code3 == 401:
                    return done(AUTH_INCORRECT, 'Device rejected the provided credentials.')
                diag['challenge_issue'] = 'unexpected_stale_retry_status'
                return done(MALFORMED_RESPONSE, f'Unexpected device response on stale-nonce retry (RTSP {code3 or "no response"}).')
        if code2 == 401:
            return done(AUTH_INCORRECT, 'Device rejected the provided credentials.')
        diag['challenge_issue'] = 'unexpected_retry_status'
        return done(MALFORMED_RESPONSE, f'Unexpected device response on authenticated retry (RTSP {code2 or "no response"}).')
    finally:
        session.close()


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
