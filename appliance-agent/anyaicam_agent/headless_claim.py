"""Headless label claim (2026-10-07): a shipped, unclaimed appliance needs
only power and Ethernet.

While the appliance has no credential, anyaicam-agent.service runs this
instead of only waiting for someone to run `anyaicam-setup --claim` at a
terminal the customer does not have. It opens a claim with the AnyAiCam
cloud carrying the verifier of the code printed on the unit's label
(written by installer/09-identity.sh into label_claim.json), keeps that
claim open, and as soon as the owner enters or scans the label code in the
portal (/customer/claim-appliance) redeems the confirmation into the
appliance's permanent credential through the same _finish_enrollment() the
terminal flow uses -- minus its camera-discovery prompt (cameras are added
from the portal).

It is the same claim exchange as setup_wizard.claim_main() (same claim-state
file, same resume/retry rules, same cloud endpoints), driven as a step
function the service calls between waits, so it:
  * never raises -- every failure becomes a log line and a later retry;
  * never logs a secret: no device secret, session id, proof, credential or
    label verifier ever appears in a log message;
  * persists every transition (claim_state.json, 0600) before acting on it,
    so a reboot or power cut resumes the same claim instead of starting over;
  * only talks to an https portal that is not this appliance's own VMS.

Eligibility (all required, otherwise the service only waits, as before):
label_claim.json with a valid verifier, the installer identity file with a
UUIDv4 appliance_id, and a portal_url that is https:// and not local.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from datetime import datetime
from typing import Callable
from urllib.parse import urlsplit

from .config import AgentConfig, clear_claim_state, load_claim_state, save_claim_state
from .portal import PortalClient, PortalError

VERIFIER_PATTERN = re.compile(r'^[0-9a-f]{64}$')
CLAIM_CODE_PATTERN = re.compile(r'^[0-9A-Z]{8}$')  # what claim/begin returns (appliance_claims._generate_claim_code)
DEVICE_ID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')
LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1', '0.0.0.0'}

PENDING_POLL_SECONDS = 15      # waiting for the owner; the claim stays open
CLAIMED_RETRY_SECONDS = 2      # proof known: complete right away
IN_PROGRESS_RETRY_SECONDS = 60  # a claim this unit cannot resume is still open on the cloud
PROVISIONED_RETRY_SECONDS = 3600  # the cloud already has this unit; a person must re-enroll it
MAX_BACKOFF_SECONDS = 300


def _read_json(path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def portal_origin(url: str) -> str:
    parts = urlsplit(str(url or '').strip())
    return f'{parts.scheme.lower()}://{(parts.netloc or "").lower()}'


def ineligibility(config: AgentConfig) -> str | None:
    """Why this appliance cannot claim itself headlessly, or None."""
    label = _read_json(config.label_claim_file)
    if not label:
        return 'no label claim file (this unit was not imaged for a label claim)'
    if not VERIFIER_PATTERN.match(str(label.get('verifier', ''))):
        return 'the label claim file has no valid verifier'
    identity = _read_json(config.installer_identity_file)
    if not identity or not DEVICE_ID_PATTERN.match(str(identity.get('appliance_id', '')).strip().lower()):
        return 'the installer identity file has no valid appliance_id'
    parts = urlsplit(str(config.portal_url or '').strip())
    if parts.scheme.lower() != 'https' or not parts.hostname or parts.hostname.lower() in LOCAL_HOSTS:
        return 'no AnyAiCam cloud portal is configured (portal_url must be an https:// cloud address)'
    return None


class HeadlessClaim:
    def __init__(self, config: AgentConfig, log, client: PortalClient | None = None,
                 finish_enrollment: Callable | None = None, clock: Callable[[], float] = time.time):
        self.config = config
        self.log = log
        self.client = client or PortalClient(config.portal_url)
        if finish_enrollment is None:
            from .setup_wizard import _finish_enrollment
            finish_enrollment = _finish_enrollment
        self.finish_enrollment = finish_enrollment
        self.clock = clock
        self.failures = 0
        self.done = False
        self._last_note = None
        # Zero-terminal onboarding (2026-10-08, link_server.py): the local
        # setup page asks for a claim code from a different thread than the
        # service's step loop; one lock keeps them from opening two claims.
        self._lock = threading.RLock()
        self._opened_code = None

    @classmethod
    def for_config(cls, config: AgentConfig, log, **kwargs):
        """A HeadlessClaim, or None (after logging why) if not eligible."""
        reason = ineligibility(config)
        if reason:
            log.info('Headless claim not available: %s; waiting for anyaicam-setup instead.', reason)
            return None
        return cls(config, log, **kwargs)

    # ---------------------------------------------------------------- helpers
    @property
    def device_id(self) -> str:
        return str(_read_json(self.config.installer_identity_file)['appliance_id']).strip().lower()

    @property
    def verifier(self) -> str:
        return str(_read_json(self.config.label_claim_file)['verifier'])

    def _note(self, message: str, *args) -> None:
        """Log a waiting-state message once, not on every poll."""
        key = (message, args)
        if key != self._last_note:
            self.log.info(message, *args)
            self._last_note = key

    def _backoff(self) -> float:
        self.failures += 1
        return float(min(MAX_BACKOFF_SECONDS, 5 * 2 ** (self.failures - 1)))

    def _ok(self) -> None:
        self.failures = 0

    # ---------------------------------------------------------------- the step
    def step(self) -> float:
        """Advance the claim by one action; returns seconds until the next
        call. Never raises."""
        if self.done:
            return float(PROVISIONED_RETRY_SECONDS)
        try:
            with self._lock:
                return self._step()
        except Exception as error:  # never let the claim break the service
            self.log.warning('Headless claim step failed (%s); retrying.', type(error).__name__)
            return self._backoff()

    def _step(self) -> float:
        origin = portal_origin(self.config.portal_url)
        device_id = self.device_id
        state = load_claim_state(self.config)
        if state and (state.get('portal_origin') != origin or not state.get('device_secret')
                      or state.get('device_id') != device_id or not state.get('claim_session_id')):
            self.log.info('Discarding a saved claim that does not belong to this appliance and portal.')
            clear_claim_state(self.config)
            state = None
        if state and state.get('claim_proof'):
            return self._complete(state)
        if state:
            return self._poll(state)
        return self._begin(device_id, origin)

    def _begin(self, device_id: str, origin: str) -> float:
        device_secret = secrets.token_urlsafe(32)
        try:
            session = self.client.claim_begin(device_id, device_secret, label_verifier=self.verifier)
        except PortalError as error:
            status = getattr(error, 'status_code', None)
            if status == 409 and 'already provisioned' in str(error):
                self._note('The AnyAiCam cloud already has this appliance; it must be re-enrolled by support.')
                return float(PROVISIONED_RETRY_SECONDS)
            if status == 409:
                self._note('A claim for this appliance is still open on the AnyAiCam cloud; waiting for it to expire.')
                return float(IN_PROGRESS_RETRY_SECONDS)
            self.log.warning('Could not open a claim with the AnyAiCam cloud (HTTP %s); retrying.', status or 'unreachable')
            return self._backoff()
        save_claim_state(self.config, {'device_id': device_id, 'claim_session_id': session['claim_session_id'],
                                       'device_secret': device_secret, 'portal_origin': origin,
                                       'opened_at': datetime.now().isoformat(), 'headless': True})
        self._opened_code = session.get('claim_code')  # memory only, for link_code()
        self._ok()
        self._note('Waiting for the owner to claim this appliance with the code on its label.')
        return float(PENDING_POLL_SECONDS)

    def _poll(self, state: dict) -> float:
        try:
            status = self.client.claim_status(state['claim_session_id'], state['device_secret'])
        except PortalError as error:
            self.log.warning('Could not check the claim with the AnyAiCam cloud (HTTP %s); retrying.',
                             getattr(error, 'status_code', None) or 'unreachable')
            return self._backoff()
        self._ok()
        value = status.get('status')
        if value == 'claimed' and status.get('claim_proof'):
            state['claim_proof'] = status['claim_proof']
            save_claim_state(self.config, state)  # before redeeming it
            self.log.info('The owner confirmed the claim; activating.')
            return float(CLAIMED_RETRY_SECONDS)
        if value in ('expired', 'completed', 'revoked') or value is None:
            # An open claim lasts minutes; while unclaimed the appliance keeps
            # a fresh one open so the label code always works.
            clear_claim_state(self.config)
            return 0.0
        return float(PENDING_POLL_SECONDS)

    def _complete(self, state: dict) -> float:
        try:
            activated = self.client.claim_complete(state['claim_session_id'], state['claim_proof'], state['device_secret'])
        except PortalError as error:
            status = getattr(error, 'status_code', None)
            if status in (403, 409):
                # Refused, not lost: the proof is spent or expired. Start over;
                # the owner sees the appliance waiting again and can re-confirm.
                self.log.warning('The AnyAiCam cloud refused to complete the claim (HTTP %s); opening a new claim.', status)
                clear_claim_state(self.config)
                return float(CLAIMED_RETRY_SECONDS)
            self.log.warning('Could not complete the claim with the AnyAiCam cloud (HTTP %s); retrying with the same proof.',
                             status or 'unreachable')
            return self._backoff()
        try:
            self.finish_enrollment(self.config, activated, ask_discovery=False)
        except SystemExit as error:
            # Local enrollment failed and was rolled back. claim_state.json
            # (with the proof) is kept: the cloud returns the same credential
            # to a retry with the same proof, so nothing is lost.
            self.log.warning('Activation could not be saved on this appliance (%s); retrying.', str(error)[:200])
            return self._backoff()
        clear_claim_state(self.config)
        self.done = True
        self._ok()
        self.log.info('Appliance claimed and activated cloud_id=%s', activated.get('cloud_id'))
        return 0.0

    # ---------------------------------------------------------------- local setup page
    def link_code(self) -> str | None:
        """A current claim code for this appliance's own claim, for the local
        setup page (link_server.py) to hand to the owner's browser in a URL
        fragment -- so nobody has to see, copy or type it. Opens the claim if
        none is open, otherwise resumes it with the same device secret (the
        cloud then issues a fresh code for the same session; the old one stops
        working). None when the cloud cannot be reached, the appliance is
        already provisioned, or a confirmation is already being redeemed.
        Never raises; never logs the code."""
        if self.done:
            return None
        try:
            with self._lock:
                return self._link_code()
        except Exception as error:
            self.log.warning('Could not prepare the account link (%s).', type(error).__name__)
            return None

    def _link_code(self) -> str | None:
        origin = portal_origin(self.config.portal_url)
        device_id = self.device_id
        state = load_claim_state(self.config)
        if state and (state.get('portal_origin') != origin or not state.get('device_secret')
                      or state.get('device_id') != device_id or not state.get('claim_session_id')):
            clear_claim_state(self.config)
            state = None
        if state and state.get('claim_proof'):
            return None  # already confirmed by the owner; the step loop is finishing it
        if not state:
            self._opened_code = None
            self._begin(device_id, origin)
            code, self._opened_code = self._opened_code, None
            return code if CLAIM_CODE_PATTERN.match(str(code or '')) else None
        try:
            session = self.client.claim_begin(device_id, state['device_secret'], label_verifier=self.verifier)
        except PortalError as error:
            self.log.warning('Could not refresh the claim for the account link (HTTP %s).',
                             getattr(error, 'status_code', None) or 'unreachable')
            return None
        if session.get('claim_session_id') and session['claim_session_id'] != state['claim_session_id']:
            # The saved claim had expired; the cloud opened a new one for this
            # same device secret. Follow it so the step loop polls the right one.
            state['claim_session_id'] = session['claim_session_id']
            save_claim_state(self.config, state)
        code = session.get('claim_code')
        return code if CLAIM_CODE_PATTERN.match(str(code or '')) else None
