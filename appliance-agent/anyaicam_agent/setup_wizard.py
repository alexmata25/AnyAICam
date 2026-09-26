import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .commands import _queue_privileged_action
from .config import AgentConfig,clear_claim_state,load_claim_state,load_credential,load_wireguard_identity,save_claim_state
from .discovery import scan
from .portal import PortalClient,PortalError
from .reenrollment import ReenrollmentError,coordinated_reenroll,first_enroll
from .wireguard import enroll_wireguard


def _upsert_vms_env_key(config:AgentConfig,key:str,value:str) -> None:
    """Python-side mirror of installer/06-deploy-vms.sh's own
    upsert_env_key(): replace an existing `key=` line in the VMS env file
    or append one, then rewrite atomically. Used for ANYAICAM_CLOUD_URL,
    which -- like ANYAICAM_VMS_COMMIT/ANYAICAM_BUILD_ID in that script,
    and unlike the generate-once secrets there -- reflects this
    activation's own current portal_url and is meant to be refreshed on
    every (re-)activation, not preserved from an earlier install."""
    path=Path(config.config_dir)/'vms.env'
    lines=[line for line in path.read_text(encoding='utf-8').splitlines() if not line.startswith(key+'=')] if path.is_file() else []
    lines.append(f'{key}={value}')
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    temporary.replace(path)


def qr_payload():
    value=input('Paste/scan provisioning QR value, or press Enter for manual entry: ').strip()
    if value: return (value.split('|',1)+[''])[:2]
    image=input('Optional QR image path (requires zbarimg), or press Enter: ').strip()
    if image and shutil.which('zbarimg'):
        output=subprocess.run(['zbarimg','--quiet','--raw',image],capture_output=True,text=True,timeout=15,check=True).stdout.strip(); return (output.split('|',1)+[''])[:2]
    return input('Cloud ID: ').strip(),getpass.getpass('Activation token: ').strip()


def _finish_enrollment(config:AgentConfig,activated:dict) -> None:
    """Everything after a successful activation -- identical regardless
    of whether `activated` came from the interactive admin-driven flow
    (POST /api/appliance/activate) or the non-interactive claim flow
    (POST /api/appliance/claim/complete): both return the exact same
    response shape by design (see app/appliance_claims.py's own
    docstring on this), so this one function serves both entry points
    rather than duplicating it. Extracted unchanged from the original
    single-flow main() -- first_enroll()/coordinated_reenroll() and
    everything else here is exactly as it was, not redesigned.

    restart_service() no longer runs `systemctl restart` directly. That
    called it as the unprivileged `anyaicam` system user this tool is
    documented to run as (see appliance-agent/scripts/install.sh's own
    "Run: sudo -u anyaicam ...anyaicam-setup"), which has no authority
    to restart a system unit and no interactive desktop session for
    polkit to prompt through -- confirmed live on Ryzen (2026-09-12):
    the restart hung/failed with a CalledProcessError, which
    first_enroll() correctly treated as fatal and rolled back, even
    though the credential itself had already been issued and could
    never be recovered afterward (see claim_main()'s own comment on
    clear_claim_state() for that half of the incident). It now queues
    the restart through the same root-owned privileged-watcher marker
    mechanism already used a few lines below for restart_vms --
    appliance-agent/system/privileged_watcher.py's fixed DISPATCH table,
    the one thing on this device actually allowed to touch systemd/
    Docker -- rather than inventing a new sudoers or polkit rule. And
    unlike a raised exception, a failure here is only a warning, never
    fatal: RC4's own _await_activation() already makes anyaicam-agent.
    service pick up a freshly written credential.json on its own within
    ANYAICAM_ACTIVATION_POLL_INTERVAL_SECONDS (10s) without needing a
    restart at all, and verify_authentication() below proves the new
    credential actually works against the portal directly -- neither
    depends on this restart succeeding, so failing enrollment over it
    would reject a genuinely successful activation for no reason."""
    def restart_service():
        status,_,error=_queue_privileged_action(config,'restart_agent',{'confirmed':True})
        if status!='completed':
            print(f'WARNING: could not queue an anyaicam-agent restart automatically ({error}); the service will pick up the new credential on its own within about 10 seconds -- no manual action needed.')
    def verify_authentication(identity):
        check=PortalClient(config.portal_url,identity['appliance_id'],identity['credential'])
        check.request('GET','/api/appliance/commands')
        return True
    vms_identity_path=Path(config.vms_recordings_path)/'appliance_identity.json'
    # A truly fresh appliance has none of agent.json/credential.json/the VMS's
    # own appliance_identity.json yet -- coordinated_reenroll() requires all
    # three to already exist (it exists to REPLACE a prior identity, with
    # something to roll back to). first_enroll() is the counterpart for that
    # case; picking the wrong one here would either fail closed for a fresh
    # box (coordinated_reenroll) or refuse to touch an already-activated one
    # (first_enroll) -- both fail loud, never silently do the wrong thing.
    already_enrolled=all(p.is_file() for p in (Path(config.config_dir)/'agent.json',config.credential_file,vms_identity_path))
    enroll=coordinated_reenroll if already_enrolled else first_enroll
    try:
        enroll(config,activated,expected_cloud_id=activated['cloud_id'],
            vms_identity_path=vms_identity_path,
            restart_service=restart_service,verify_authentication=verify_authentication)
    except ReenrollmentError as error: raise SystemExit(str(error)) from error
    # WireGuard direct remote connectivity (docs/wireguard-remote-
    # connectivity-plan.md Sec 5): gated behind ANYAICAM_WIREGUARD_ENABLED,
    # unset (feature off) everywhere today -- Phase D's own explicit
    # go/no-go, not this pass, is what would ever set it, matching this
    # codebase's established staged-rollout precedent (ANYAICAM_LIVE_
    # P2P_ENABLED, relay_control.py's own ANYAICAM_FACIAL_ACCESS_CONTROL_
    # ENABLED). already_enrolled (computed above) doubles as replace_
    # existing here: a re-enrollment (hardware replacement/re-claim)
    # should rotate this device's WireGuard identity and revoke its
    # prior peer row the same way it already rotates every other
    # identity field; a genuinely first-time enrollment has nothing to
    # replace, so replace_existing=False there is simply a no-op on the
    # cloud side (see wireguard_remote.py's own enroll route).
    #
    # Failure here is ALWAYS only a warning, never fatal -- WireGuard is
    # fully additive (plan doc Sec 18): the identity this function just
    # committed above is already real and complete without it, and the
    # existing WebRTC P2P / AWS relay live-view paths are completely
    # unaffected either way. This also deliberately never queues
    # wireguard_interface_up on a failure -- only on a real, confirmed
    # enrollment success, so a failed enrollment can never leave a
    # privileged action queued for a config that doesn't exist yet.
    if os.environ.get('ANYAICAM_WIREGUARD_ENABLED','').strip().lower()=='true':
        try:
            client=PortalClient(config.portal_url,activated['appliance_id'],activated['credential'])
            enroll_wireguard(config,client,replace_existing=already_enrolled)
            status,_,error=_queue_privileged_action(config,'wireguard_interface_up',{'confirmed':True})
            if status!='completed': print(f'WARNING: could not queue WireGuard interface bring-up automatically ({error}); direct remote connectivity will not be available until this is retried -- existing WebRTC P2P and AWS relay paths are unaffected.')
        except Exception as error:
            print(f'WARNING: WireGuard enrollment failed ({error}); direct remote connectivity will not be available -- existing WebRTC P2P and AWS relay paths are unaffected.')
    # The VMS container reads portal_url from its own ANYAICAM_CLOUD_URL env
    # var at process start (recording_uploader.py/live_relay_uploader.py/
    # analytics_sync.py/event_media_uploader.py all read it the same way) --
    # an env var only takes effect on container (re)start, unlike the
    # identity file above, which those same modules read fresh off disk via
    # the shared /var/lib/anyaicam mount on every call. Written here, after
    # enrollment succeeds, so a fresh or re-pointed appliance's VMS container
    # always ends up configured with the SAME portal this agent just
    # authenticated against -- never a stale or guessed value.
    try:
        _upsert_vms_env_key(config,'ANYAICAM_CLOUD_URL',config.portal_url)
        status,_,error=_queue_privileged_action(config,'restart_vms',{'confirmed':True})
        if status!='completed': print(f'WARNING: could not queue a VMS restart automatically ({error}); restart the anyaicam-vms container manually to apply the new cloud URL.')
    except OSError as error:
        print(f'WARNING: could not update the VMS environment file ({error}); set ANYAICAM_CLOUD_URL={config.portal_url} in it manually and restart anyaicam-vms.')
    if input('Run camera discovery now? [Y/n]: ').strip().lower()!='n':
        cameras=scan(config.discovery_networks); config.cameras_file.parent.mkdir(parents=True,exist_ok=True); config.cameras_file.write_text(json.dumps(cameras,indent=2),encoding='utf-8'); print(f'Discovered {len(cameras)} compatible endpoints.')
    print('Configuration saved securely.')
    print('AnyAiCam service restarted and authenticated during identity commit.')


def interactive_main():
    print('\nAnyAiCam first-run appliance setup\n')
    config=AgentConfig.load(); config.portal_url=input(f'Portal URL [{config.portal_url}]: ').strip() or config.portal_url; config.mode=input(f'Mode (development/production) [{config.mode}]: ').strip() or config.mode
    cloud_id,token=qr_payload(); config.cloud_id=cloud_id.upper()
    client=PortalClient(config.portal_url)
    try: info=client.test(); print('Portal connectivity: OK',info.get('mode'))
    except PortalError as error: raise SystemExit(f'Portal connectivity failed: {error}')
    try: activated=client.activate(config.cloud_id,token)
    except PortalError as error: raise SystemExit(f'Activation failed: {error}')
    print('Assigned customer:',activated.get('customer_id')); print('Assigned site:',activated.get('site_id'))
    _finish_enrollment(config,activated)


# --------------------------------------------------------- Phase 2A: non-
# interactive claim flow (anyaicam-setup --claim). Reuses the Phase 1
# PortalClient.claim_begin/claim_status/claim_complete methods
# unchanged -- see appliance-agent/anyaicam_agent/portal.py's own
# comment on why those three methods never needed to change to reach
# this point. Does not build any QR display or local web UI (out of
# scope for Phase 2A); the claim code is printed to the terminal for a
# technician to relay, or read directly if a console is attached.


def _installer_device_id(config:AgentConfig) -> str:
    """The appliance's own UUIDv4 identity, generated once at install
    time by installer/09-identity.sh (`cat /proc/sys/kernel/random/uuid`)
    and preserved across reinstalls -- the one real identifier this
    repository's own installer produces, and the exact value claim/
    begin's device_id now requires (Phase 1 security-hardening
    checkpoint; see app/appliance_claims.py's own DEVICE_ID_PATTERN
    comment). Never generated here -- if it is missing, the installer
    did not run correctly, and this fails loud rather than inventing a
    substitute identity that would not match what was printed on the
    unit at manufacturing/imaging time."""
    path=config.installer_identity_file
    try: data=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,json.JSONDecodeError) as error:
        raise SystemExit(f'Could not read the installer identity file at {path}: {error}. Re-run the installer before claiming this appliance.') from error
    device_id=str(data.get('appliance_id','')).strip()
    if not device_id:
        raise SystemExit(f'{path} does not contain an appliance_id. Re-run the installer before claiming this appliance.')
    return device_id


def _open_or_resume_claim(client:PortalClient,config:AgentConfig,device_id:str) -> dict:
    """Never calls claim/begin while a locally-recorded claim session
    for this exact device_id might still be live on the cloud side --
    resumes it instead, matching the state-machine doc's own
    resume-after-restart rule ("never call claim/begin while a
    non-expired claim-state file exists on disk"). Covers this
    process being interrupted (killed, rebooted, crashed) at any point
    between opening a claim and completing it."""
    state=load_claim_state(config)
    if state and state.get('device_id')==device_id and state.get('claim_session_id'):
        print('Resuming a previously-opened claim session for this appliance.')
        return state
    try: session=client.claim_begin(device_id)
    except PortalError as error: raise SystemExit(f'Could not start the claim: {error}') from error
    state={'device_id':device_id,'claim_session_id':session['claim_session_id'],'opened_at':datetime.now().isoformat()}
    save_claim_state(config,state)
    if session.get('resumed'):
        print('A claim session was already pending for this appliance on the cloud side; resuming it.')
    elif session.get('claim_code'):
        print(f"\n  Claim code:  {session['claim_code']}\n")
        print(f"This code expires at {session['expires_at']}.")
        print("On another device, sign in to the AnyAiCam customer portal, open 'Claim an appliance', enter this code, choose the site, and confirm.\n")
    return state


def _wait_for_claim_proof(client:PortalClient,config:AgentConfig,state:dict,sleep_fn=time.sleep) -> str:
    """Polls claim/status until a claim_proof is available, persisting
    it into the local claim-state file the moment it is learned -- not
    just claim_session_id -- so a restart between confirmation and
    completion can retry claim/complete with the identical value
    afterward. This is what makes the appliance side of a lost-
    completion-response retry actually possible: the cloud side (Phase
    1 hardening item 3) is retry-safe for a given claim_session_id+
    claim_proof pair, but only if this process still has that exact
    pair to present again."""
    if state.get('claim_proof'):
        return state['claim_proof']
    print('Waiting for a customer to confirm this claim in the portal...')
    while True:
        try: status=client.claim_status(state['claim_session_id'])
        except PortalError as error:
            print(f'WARNING: could not poll claim status ({error}); retrying...')
            sleep_fn(5); continue
        if status.get('status')=='expired':
            clear_claim_state(config)
            raise SystemExit('This claim session expired before it was confirmed. Run anyaicam-setup --claim again to start over.')
        if status.get('status')=='claimed' and status.get('claim_proof'):
            state['claim_proof']=status['claim_proof']
            save_claim_state(config,state)
            print('Claim confirmed by customer.')
            return state['claim_proof']
        sleep_fn(5)


def _complete_claim_with_retry(client:PortalClient,config:AgentConfig,state:dict,attempts:int=3,sleep_fn=time.sleep) -> dict:
    """Retries claim/complete on a transient network failure -- never
    on an application-level rejection (PortalError with a status_code
    still raises through the last attempt, exactly like a single
    unretried call would). The cloud side is retry-safe for the exact
    same claim_session_id+claim_proof pair (Phase 1 hardening item 3),
    so a retry here can only ever recover the original result, never
    mint a second credential."""
    last_error=None
    for attempt in range(1,attempts+1):
        try: return client.claim_complete(state['claim_session_id'],state['claim_proof'])
        except PortalError as error:
            last_error=error
            if attempt<attempts:
                print(f'WARNING: claim completion attempt {attempt} failed ({error}); retrying...')
                sleep_fn(2)
    raise SystemExit(f'Claim completion failed after {attempts} attempts: {last_error}. Run anyaicam-setup --claim again to retry -- it is safe to retry the same claim.')


def claim_main():
    print('\nAnyAiCam appliance claim\n')
    config=AgentConfig.load(); config.portal_url=input(f'Portal URL [{config.portal_url}]: ').strip() or config.portal_url; config.mode=input(f'Mode (development/production) [{config.mode}]: ').strip() or config.mode
    device_id=_installer_device_id(config)
    client=PortalClient(config.portal_url)
    try: info=client.test(); print('Portal connectivity: OK',info.get('mode'))
    except PortalError as error: raise SystemExit(f'Portal connectivity failed: {error}')
    state=_open_or_resume_claim(client,config,device_id)
    _wait_for_claim_proof(client,config,state)
    activated=_complete_claim_with_retry(client,config,state)
    print('Assigned customer:',activated.get('customer_id')); print('Assigned site:',activated.get('site_id'))
    _finish_enrollment(config,activated)
    # Only cleared here, after local enrollment has actually succeeded --
    # never right after claim_complete() returns. claim_state.json is the
    # ONLY place the plaintext claim_proof this device would need to
    # retry claim/complete ever lives (the server only ever stores its
    # one-way hash). Clearing it before enrollment is confirmed durable
    # strands an already-issued, already-paid-for credential the instant
    # _finish_enrollment() fails for any local reason -- confirmed live
    # on Ryzen (2026-09-12): claim/complete succeeded and minted a real
    # credential, but the CLI's own restart_service() step then failed,
    # and this file was already gone by then. The server's own retry-
    # safety recovery path (claim_complete()'s status=='completed'
    # branch, bounded by CREDENTIAL_RECOVERY_TTL_MINUTES) requires
    # presenting that exact plaintext claim_proof, so it was unreachable
    # even though its recovery window was still open. If _finish_
    # enrollment() now raises SystemExit, this line is simply never
    # reached and claim_state.json (mode 0600, holding only this one
    # claim's session id + proof) stays on disk -- rerunning
    # anyaicam-setup --claim then correctly RESUMES via
    # _open_or_resume_claim()/_wait_for_claim_proof()'s own
    # already-have-a-claim_proof short-circuit and recovers the exact
    # same credential through claim_complete()'s existing retry path --
    # no new claim, no second credential, no second appliance record.
    clear_claim_state(config)


# --------------------------------------------------------- WireGuard
# standalone enrollment trigger (anyaicam-setup --wireguard-enroll).
#
# _finish_enrollment()'s own WireGuard hook (above) only ever runs
# inside interactive_main()/claim_main() -- i.e. only at the moment an
# appliance is FIRST activated or RE-claimed. service.py's long-running
# daemon never calls it again afterward, so an appliance that was
# already active before ANYAICAM_WIREGUARD_ENABLED existed (every real
# appliance in the field today) has no path that ever reaches
# enroll_wireguard() -- flipping the env var and restarting the service
# is a genuine no-op there. This command is that missing path: a small,
# additive, separately-invocable trigger for an ALREADY-activated
# appliance, deliberately NOT routed through first_enroll()/
# coordinated_reenroll() (see wireguard_enroll_main() below) -- turning
# on one additive feature must never re-run identity replacement.
def wireguard_enroll_main():
    print('\nAnyAiCam WireGuard direct-connectivity enrollment\n')
    config=AgentConfig.load()
    credential=load_credential(config)
    if not credential:
        raise SystemExit('This appliance has not been activated yet. Run anyaicam-setup (or anyaicam-setup --claim) first, then retry --wireguard-enroll.')
    # Deliberately load_credential()/PortalClient() only -- never
    # first_enroll()/coordinated_reenroll()/reenrollment.py at all. This
    # command's entire contract is "reuse the existing appliance
    # identity, touch nothing about it" -- agent.json, credential.json,
    # and the VMS's own appliance_identity.json are never read, staged,
    # or written anywhere in this function.
    client=PortalClient(config.portal_url,credential['appliance_id'],credential['credential'])
    # Read BEFORE enrolling, purely to decide afterward whether the
    # tunnel's own address/gateway assignment actually changed -- never
    # used to skip calling enroll_wireguard() itself. enroll_wireguard()
    # is always called; its own already-tested reuse-existing-key logic
    # (wireguard.py) is what makes a repeat call idempotent at the
    # keypair layer, and enroll_peer()'s own same-public-key short
    # circuit (app/wireguard_remote.py) is what makes it idempotent at
    # the cloud row layer -- this function adds a third, purely
    # cosmetic/operational layer on top: deciding whether the real
    # interface needs re-queuing, not whether enrollment should happen.
    previous=load_wireguard_identity(config)
    try:
        # replace_existing is always False here -- that flag exists
        # exclusively for the hardware-replacement path inside
        # coordinated_reenroll() (see wireguard.py's own docstring on
        # this call site), which this command deliberately never
        # touches. A routine (re-)run of this command is always a
        # reconnect/reconcile, never a replacement.
        identity=enroll_wireguard(config,client,replace_existing=False)
    except PortalError as error:
        raise SystemExit(f'WireGuard enrollment failed: {error}. Nothing was changed locally -- this command is safe to re-run once the problem above is resolved.') from error
    changed=(previous is None or any(previous.get(field)!=identity.get(field) for field in ('public_key','tunnel_address','gateway_public_key','gateway_endpoint')))
    print(f"Tunnel address: {identity['tunnel_address']}")
    print(f"Gateway endpoint: {identity['gateway_endpoint']}")
    print(f"Status: {identity['status']}")
    if changed:
        status,_,error=_queue_privileged_action(config,'wireguard_interface_up',{'confirmed':True})
        if status!='completed':
            print(f'WARNING: could not queue WireGuard interface bring-up automatically ({error}); direct remote connectivity will not be available until this command is re-run -- existing WebRTC P2P and AWS relay paths are unaffected.')
        else:
            print('WireGuard interface bring-up has been queued for the privileged watcher to apply.')
    else:
        print('This appliance is already enrolled with this exact tunnel configuration -- no interface change is needed.')
    print('Existing appliance identity (cloud_id, customer/site assignment, credential) and camera configuration were not modified by this command.')


def main():
    args=sys.argv[1:]
    if '--wireguard-enroll' in args: wireguard_enroll_main()
    elif '--claim' in args: claim_main()
    else: interactive_main()


if __name__=='__main__': main()
