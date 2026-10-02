import base64
import binascii
import json
import os
import platform
import subprocess
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .camera_binding import atomic_write_json
from .discovery import scan
from .metrics import _cpu_percent, _memory_percent, disk_summary

ALLOWED={'restart_service','refresh_cameras','run_diagnostics','install_update','start_live_relay','stop_live_relay','reboot_appliance','restart_vms'}

# RDM4 diagnostics: only these keys ever leave the appliance in the
# per-camera summary -- an explicit allowlist, not a "strip the bad
# keys" denylist, so a future field added to cameras.json (which
# already went through camera_binding.py's own LOCAL_ONLY_KEYS
# redaction once) can never leak here just by existing. No id/name/
# mac/ip is included; camera_number is the local FFmpeg slot number,
# not a physical identifier.
_CAMERA_SUMMARY_KEYS={'camera_number','online','recording','analytics','last_error'}


def _vms_health(config,timeout=3):
    # RDM4: reads the VMS app's own /health signal directly over the
    # loopback interface -- no Docker socket access, none needed, and
    # none available to this sandboxed process (see pending_actions_dir's
    # docstring in config.py). Verified reachable from the real agent
    # systemd sandbox (NoNewPrivileges/ProtectSystem=strict/
    # CapabilityBoundingSet=CAP_NET_RAW) before this was wired in.
    try:
        with urllib.request.urlopen(config.vms_local_health_url,timeout=timeout) as response:
            return json.loads(response.read().decode() or '{}')
    except (urllib.error.URLError,TimeoutError,OSError,json.JSONDecodeError,ValueError) as error:
        return {'status':'unreachable','error':error.__class__.__name__}


def _camera_summary(config):
    try:
        cameras=json.loads(config.cameras_file.read_text(encoding='utf-8'))
    except (OSError,json.JSONDecodeError):
        return []
    if not isinstance(cameras,list): return []
    return [{key:camera.get(key) for key in _CAMERA_SUMMARY_KEYS if key in camera} for camera in cameras if isinstance(camera,dict)]


def diagnostics(config,queue_count=0):
    return {'timestamp':datetime.now().isoformat(),'hostname':platform.node(),'platform':platform.platform(),'python':platform.python_version(),'portal_url':config.portal_url,'mode':config.mode,'queue_depth':queue_count,'config_file_exists':(Path(config.config_dir)/'agent.json').exists(),'credential_exists':config.credential_file.exists(),
            # RDM4 additions below -- agent health is implicit (this code
            # is running); everything else reuses existing, already-tested
            # logic (metrics.py's CPU/memory/disk, camera_binding.py's
            # already-redacted cameras.json) rather than a second copy of
            # it. restart_count/upload_pending_count/upload_quarantined_count
            # are intentionally NOT duplicated here -- they're already
            # computed/reported through the separate, existing RDM3
            # heartbeat/upload-worker pipeline (see app/appliance_cloud.py),
            # and re-deriving a second, possibly-stale copy of the same
            # facts on every on-demand diagnostics call would just be a
            # second source of truth to keep in sync for no benefit.
            'agent_service':'active','vms_health':_vms_health(config),'cpu':_cpu_percent(),'memory':_memory_percent(),**disk_summary(config),'cameras':_camera_summary(config)}


def _queue_privileged_action(config,action_type,payload):
    # RDM4 (reboot_appliance/restart_vms): the ONLY thing this process
    # ever does for either command -- write a small atomic marker naming
    # a known action TYPE plus a correlation id, nothing executable.
    # This agent has no privilege to reboot the host or touch Docker and
    # is not attempting to acquire any (NoNewPrivileges=true on its own
    # systemd unit makes that structurally impossible regardless); a
    # separate, root-owned watcher -- outside this process entirely --
    # is what actually acts, via a fixed, hardcoded dispatch table that
    # never reads a command, path, or container name out of this marker.
    #
    # confirmed=true is already enforced by the cloud's queue_command()
    # before this command is ever queued for delivery -- this check is
    # defense in depth, not the only gate, matching every other
    # confirmation-required command's own local re-check pattern.
    if not isinstance(payload,dict) or not payload.get('confirmed'):
        return 'failed',{},'Confirmation is required for this action.'
    marker_id=uuid.uuid4().hex
    marker={'type':action_type,'command_id':marker_id,'requested_at':datetime.now(timezone.utc).isoformat()}
    try:
        atomic_write_json(config.pending_actions_dir/f'{action_type}.json',marker)
    except OSError as error:
        return 'failed',{},f'Could not record the {action_type} request due to a local storage error: {error}'
    return 'completed',{'requested':True,'marker_id':marker_id},''


def _validate_relay_camera_number(value):
    # Phase 4 (docs/AI_HANDOFF.md §8): bool is a subclass of int in Python --
    # explicitly reject True/False so a stray boolean can never masquerade as
    # camera_number 0/1. appliance-agent has no access to the real
    # CAMERA_COUNT (that config lives only in the separate app/main.py
    # process), so this is a generic sanity bound, not an authoritative range.
    if isinstance(value,bool): return None
    try: number=int(value)
    except (TypeError,ValueError): return None
    return number if 1<=number<=256 else None


def _validate_relay_camera_id(value):
    if not isinstance(value,str): return None
    trimmed=value.strip()
    return trimmed if trimmed and len(trimmed)<=128 else None


def _set_relay_command(config,payload,active):
    # Phase 4: durably records desired relay state for the VMS app process (a
    # separate process -- see app/live_relay_uploader.py) to reconcile on its
    # own next tick. This function never calls set_relay_active() itself --
    # it cannot, it runs in a different process. Never raises: any problem is
    # reported back as a normal 'failed' result, matching every other command
    # here. Never includes camera_id, credentials, or secrets in error text.
    if not isinstance(payload,dict): payload={}
    camera_number=_validate_relay_camera_number(payload.get('camera_number'))
    camera_id=_validate_relay_camera_id(payload.get('camera_id'))
    if camera_number is None or camera_id is None:
        return 'failed',{},'camera_number and camera_id are required and must be valid.'
    path=config.live_relay_commands_file
    try:
        commands=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(commands,dict): commands={}
    except (OSError,json.JSONDecodeError):
        commands={}
    commands[str(camera_number)]={'camera_id':camera_id,'active':bool(active)}
    try:
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(commands),encoding='utf-8')
        os.chmod(temporary,0o600)
        temporary.replace(path)
        os.chmod(path,0o600)
    except OSError:
        return 'failed',{},'Could not record live relay command due to a local storage error.'
    return 'completed',{'camera_number':camera_number,'active':bool(active)},''


def _parse_install_update_payload(payload):
    """RDM-2 Group 2C: validates the install_update wire payload's
    structural shape only -- {"manifest": {...}, "signature": "<base64>"}.
    Returns (manifest_dict, signature_bytes, None) on success, or (None,
    None, error_message) on any structural problem. Never raises -- every
    failure is reported the same way every other command here reports a
    bad payload: a normal 'failed' result, not an exception. Semantic/
    cryptographic verification of the decoded signature bytes happens
    later, inside UpdateStateMachine.process_install_update() -- this
    function's only job is getting the wire payload into the (dict,
    bytes) shape that method expects.

    Strict base64 (base64.b64decode(..., validate=True)) is used
    deliberately: lenient decoding silently ignores non-alphabet
    characters rather than rejecting them, which could let a corrupted
    or truncated signature decode into different bytes than the sender
    intended instead of failing cleanly here.
    """
    if not isinstance(payload,dict):
        return None,None,'install_update payload must be a JSON object.'
    manifest_dict=payload.get('manifest')
    if not isinstance(manifest_dict,dict):
        return None,None,'install_update payload is missing a required "manifest" object.'
    raw_signature=payload.get('signature')
    if not isinstance(raw_signature,str) or not raw_signature:
        return None,None,'install_update payload is missing a required "signature" (base64) string.'
    try:
        signature=base64.b64decode(raw_signature,validate=True)
    except (binascii.Error,ValueError) as error:
        return None,None,f'install_update payload "signature" is not valid base64: {error}'
    return manifest_dict,signature,None


# UpdateResult.state (an updater.models.UpdateState value) -> the
# 'completed'/'failed' status this command channel reports, per the
# approved RDM-2 Group 2C mapping. Covers every UpdateState reachable
# from a LIVE process_install_update() call (rejected, download_failed,
# verify_failed, install_failed, activation_failed, restarting,
# restart_failed) AND from an idempotent replay of an already-terminal
# update_id -- which can additionally surface healthy/rolled_back/
# rollback_failed, states only ever concluded by a LATER restart's
# resume_if_pending() call, never by this live command path itself.
# Deliberately no default/fallback entry: an UpdateState this table does
# not know how to map is a programming error to surface loudly (KeyError),
# not a state to silently guess a status for.
_INSTALL_UPDATE_STATUS_BY_STATE={
    'rejected':'failed',
    'download_failed':'failed',
    'verify_failed':'failed',
    'install_failed':'failed',
    'activation_failed':'failed',
    'restarting':'completed',   # provisional -- see _install_update_result()
    'restart_failed':'failed',  # activated, but restart_signal() itself failed
    'healthy':'completed',      # only reachable via idempotent replay
    'rolled_back':'failed',     # requested version did not end up running
    'rollback_failed':'failed',
}


def _install_update_result(result):
    """Maps one UpdateResult (from process_install_update(), whether a
    live pipeline run or an idempotent-replay _result_from_history()) to
    this command channel's (status, result_dict, error) shape."""
    state_value=result.state.value
    status=_INSTALL_UPDATE_STATUS_BY_STATE[state_value]
    payload=result.as_dict()
    if state_value=='restarting':
        # RESTARTING means activation succeeded and a restart was just
        # signaled -- NOT a final outcome. The real healthy/rolled_back/
        # rollback_failed conclusion is only knowable after this
        # device's NEXT restart runs resume_if_pending(), and reporting
        # that conclusion back to the cloud is a later group's job, not
        # this one's. health_confirmed=False makes the provisional
        # nature of this 'completed' explicit in the payload itself,
        # rather than letting it be mistaken for a final answer.
        payload['health_confirmed']=False
    return status,payload,result.error


def execute(command,payload,config,stop_event=None,*,state_machine=None,update_resume_failed=False):
    if command not in ALLOWED: return 'failed',{},'Unsupported command; arbitrary shell execution is disabled.'
    if command=='refresh_cameras': return 'completed',{'cameras':scan(config.discovery_networks)},''
    if command=='run_diagnostics': return 'completed',diagnostics(config),''
    if command=='start_live_relay': return _set_relay_command(config,payload,True)
    if command=='stop_live_relay': return _set_relay_command(config,payload,False)
    if command=='restart_service':
        if stop_event: stop_event.set()
        return 'completed',{'restart_requested':True},''
    if command=='reboot_appliance': return _queue_privileged_action(config,'reboot',payload)
    if command=='restart_vms': return _queue_privileged_action(config,'restart_vms',payload)
    if command=='install_update':
        # RDM-2 Group 2C: the startup/update interlock is checked BEFORE
        # any payload parsing -- cheaper, and it means a malformed
        # payload delivered while an interlock condition is active
        # always reports the interlock's own reason, never a
        # payload-parsing error that would be misleading about why the
        # update was actually refused.
        if state_machine is None:
            return 'failed',{},'Secure updater is not configured for this agent version.'
        if update_resume_failed:
            return 'failed',{},'Update resume did not complete cleanly after the last restart; new updates are blocked until the next successful restart.'
        try:
            unresolved=state_machine.has_unresolved_activation()
        except OSError as error:
            return 'failed',{},f'Could not determine update state due to a local storage error: {error}; new updates are blocked until this is resolved.'
        if unresolved:
            return 'failed',{},'A previous update is still awaiting restart/health confirmation; new updates are blocked until it resolves.'
        manifest_dict,signature,parse_error=_parse_install_update_payload(payload)
        if parse_error:
            return 'failed',{},parse_error
        result=state_machine.process_install_update(manifest_dict,signature)
        return _install_update_result(result)
    return 'failed',{},'Secure updater is not configured for this agent version.'
