"""Coordinated appliance identity replacement with complete rollback."""
from __future__ import annotations
import json, logging, os, time, uuid
from dataclasses import asdict
from pathlib import Path
from typing import Callable
from .config import AgentConfig

FIELDS={"appliance_id","cloud_id","credential","credential_id","customer_id","site_id","partner_id"}
class ReenrollmentError(RuntimeError): pass

def ensure_identity_files_exist(config:AgentConfig,vms_identity_path:str|Path) -> None:
    """First-run bootstrap for coordinated_reenroll(), which requires
    every one of the three identity files to already exist ("Every
    existing identity file must be present before re-enrollment.") --
    correct for its real job of replacing an EXISTING identity, but a
    genuinely fresh appliance (first-ever anyaicam-setup run) has none
    of the three yet. Confirmed: calling coordinated_reenroll() on a
    fresh install raises that exact ValueError immediately, before any
    real activation is even attempted -- every test in
    test_reenrollment.py pre-seeds all three files for exactly this
    reason, and none covers a genuinely fresh device.

    Creates each missing file with the minimal, safe "not yet
    activated" placeholder coordinated_reenroll() already knows how to
    treat as activation_version 0 (previous.get("activation_version",0)
    defaults to int 0 when the key is absent, satisfying its own
    validation). Never touches a file that already exists -- a device
    with a real prior identity is completely unaffected, and
    re-enrollment there behaves exactly as it always has. Must be
    called before coordinated_reenroll(), as this box's own anyaicam
    user (same as the caller), so ownership of anything created here
    matches every other agent-written file."""
    agent_path=Path(config.config_dir)/"agent.json"
    credential_path=config.credential_file
    vms_path=Path(vms_identity_path)
    for path,placeholder in ((agent_path,asdict(config)),(credential_path,{}),(vms_path,{})):
        if path.exists(): continue
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(placeholder,indent=2),encoding="utf-8")
        try: os.chmod(path,0o600)
        except OSError: pass

def validate_activation_response(value,expected_cloud_id):
    if not isinstance(value,dict) or not FIELDS.issubset(value): raise ValueError("Activation response is incomplete.")
    for field in FIELDS-{"partner_id"}:
        if not isinstance(value[field],str) or not value[field].strip(): raise ValueError("Activation response contains an invalid identity field.")
    cloud_id=value["cloud_id"].strip().upper()
    if cloud_id!=expected_cloud_id.strip().upper(): raise ValueError("Activation response Cloud ID does not match the requested Cloud ID.")
    if value["partner_id"] is not None and not isinstance(value["partner_id"],str): raise ValueError("Activation response contains an invalid partner identity.")
    return {**value,"cloud_id":cloud_id}

def _stage(path,value,prefix="reenroll"):
    path.parent.mkdir(parents=True,exist_ok=True); temp=path.with_name(f".{path.name}.{prefix}-{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value,indent=2),encoding="utf-8"); os.chmod(temp,0o600); return temp

def _read(path):
    value=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value,dict): raise ValueError(f"{path.name} must contain a JSON object.")
    return value

def _restore(path,content,replace_file,metadata):
    temp=path.with_name(f".{path.name}.rollback-{uuid.uuid4().hex}.tmp"); temp.write_bytes(content); os.chmod(temp,0o600)
    if hasattr(os,"chown"): os.chown(temp,metadata.st_uid,metadata.st_gid)
    replace_file(temp,path); os.chmod(path,0o600)

def coordinated_reenroll(config:AgentConfig,activation_response:dict,*,expected_cloud_id:str,vms_identity_path:str|Path,restart_service:Callable[[],None],verify_authentication:Callable[[dict],bool],backup_root:str|Path|None=None,replace_file:Callable=os.replace,logger=None):
    log=logger or logging.getLogger("anyaicam.reenrollment"); activation=validate_activation_response(activation_response,expected_cloud_id)
    agent_path=Path(config.config_dir)/"agent.json"; credential_path=config.credential_file; vms_path=Path(vms_identity_path); paths=(agent_path,credential_path,vms_path)
    if any(not p.is_file() for p in paths): raise ValueError("Every existing identity file must be present before re-enrollment.")
    originals={p:p.read_bytes() for p in paths}; metadata={p:p.stat() for p in paths}; previous=_read(vms_path); version=previous.get("activation_version",0)
    if not isinstance(version,int) or version<0: raise ValueError("Existing VMS activation version is invalid.")
    # Camera-binding lifecycle fix (2026-09-13): camera_bindings.json
    # (camera_binding.CameraBindingStore) records which physical MAC
    # address is approved for which cloud camera_id -- a mapping that is
    # only ever meaningful for the identity that created it. Confirmed
    # live: an appliance re-enrolled under a brand-new customer/cloud_id
    # kept its PRIOR identity's bindings forever (nothing ever cleared
    # them), permanently blocking a new customer's camera from ever
    # binding to the same physical hardware -- CameraBindingStore.bind()'s
    # own MAC/camera_number collision check has no way to know the old
    # binding's cloud_camera_id no longer exists anywhere. Reset here,
    # atomically, alongside the rest of the identity swap this function
    # already performs -- not on every call (a bare credential refresh
    # for the SAME cloud_id keeps bindings that are still completely
    # valid), only when the cloud_id is genuinely changing. Absence of
    # this file is not an error (a box that has never discovered/bound a
    # camera simply doesn't have one yet) -- nothing to back up or reset
    # in that case, and none is invented.
    bindings_path=Path(config.camera_bindings_file); bindings_existed=bindings_path.is_file()
    if bindings_existed:
        originals[bindings_path]=bindings_path.read_bytes(); metadata[bindings_path]=bindings_path.stat()
    reset_bindings=previous.get("cloud_id")!=activation["cloud_id"]
    agent=asdict(config); agent["cloud_id"]=activation["cloud_id"]
    credential={"appliance_id":activation["appliance_id"],"credential_id":activation["credential_id"],"credential":activation["credential"]}
    vms={"appliance_id":activation["appliance_id"],"cloud_id":activation["cloud_id"],"credential":activation["credential"],"customer_id":activation["customer_id"],"site_id":activation["site_id"],"partner_id":activation["partner_id"],"activated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"activation_version":version+1}
    staged={}; backup=Path(backup_root or Path(config.state_dir)/"identity-backups"/time.strftime("%Y%m%dT%H%M%SZ",time.gmtime())); touched=False
    try:
        for p,value in ((agent_path,agent),(credential_path,credential),(vms_path,vms)): staged[p]=_stage(p,value)
        a,c,v=(_read(staged[p]) for p in paths)
        if {(a.get("cloud_id"),c.get("appliance_id")),(v.get("cloud_id"),v.get("appliance_id")),(activation["cloud_id"],activation["appliance_id"])}.__len__()!=1 or c.get("credential")!=v.get("credential"): raise ValueError("Staged identity documents do not agree.")
        if reset_bindings: staged[bindings_path]=_stage(bindings_path,{"version":1,"bindings":[]},prefix="reenroll-bindings")
        backup.mkdir(parents=True,exist_ok=False); os.chmod(backup,0o700)
        for p in paths: q=backup/p.name; q.write_bytes(originals[p]); os.chmod(q,0o600)
        if bindings_existed: q=backup/bindings_path.name; q.write_bytes(originals[bindings_path]); os.chmod(q,0o600)
        for p in paths:
            if hasattr(os,"chown"): os.chown(staged[p],metadata[p].st_uid,metadata[p].st_gid)
            replace_file(staged[p],p); os.chmod(p,0o600); touched=True
        if reset_bindings:
            if bindings_existed and hasattr(os,"chown"): os.chown(staged[bindings_path],metadata[bindings_path].st_uid,metadata[bindings_path].st_gid)
            replace_file(staged[bindings_path],bindings_path); os.chmod(bindings_path,0o600)
        restart_service()
        if not verify_authentication(activation): raise RuntimeError("Control-plane authentication failed after re-enrollment.")
    except Exception as error:
        binding_touched=bindings_path.exists() and (not bindings_existed or bindings_path.read_bytes()!=originals.get(bindings_path))
        if touched or binding_touched or any(p.exists() and p.read_bytes()!=originals[p] for p in paths):
            failures=[]
            for p in paths:
                try:_restore(p,originals[p],replace_file,metadata[p])
                except Exception as e:failures.append(f"{p.name}: {type(e).__name__}")
            if bindings_existed:
                try:_restore(bindings_path,originals[bindings_path],replace_file,metadata[bindings_path])
                except Exception as e:failures.append(f"{bindings_path.name}: {type(e).__name__}")
            elif bindings_path.exists():
                # Never existed before this attempt -- remove what got
                # staged/replaced rather than "restore" it to nothing.
                try: bindings_path.unlink()
                except FileNotFoundError: pass
                except Exception as e: failures.append(f"{bindings_path.name}: {type(e).__name__}")
            try:restart_service()
            except Exception as e:failures.append(f"service: {type(e).__name__}")
            if failures: raise ReenrollmentError("Re-enrollment failed and rollback was incomplete: "+", ".join(failures)) from error
        log.error("Appliance re-enrollment failed; all prior identity files were restored (%s).",type(error).__name__)
        raise ReenrollmentError("Appliance re-enrollment failed; prior identity was restored.") from error
    finally:
        for p in staged.values():
            try:p.unlink()
            except FileNotFoundError:pass
    log.info("Appliance re-enrollment completed and control-plane authentication succeeded.")
    return {"cloud_id":activation["cloud_id"],"appliance_id":activation["appliance_id"],"backup_dir":str(backup),"camera_bindings_reset":reset_bindings}

def first_enroll(config:AgentConfig,activation_response:dict,*,expected_cloud_id:str,vms_identity_path:str|Path,restart_service:Callable[[],None],verify_authentication:Callable[[dict],bool],replace_file:Callable=os.replace,logger=None):
    """First-ever activation of a fresh appliance: agent.json, credential.json,
    and the VMS's own appliance_identity.json do not exist yet, so
    coordinated_reenroll()'s precondition (all three already present, so a
    failure has a prior identity to roll back to) can never be satisfied --
    calling it on a truly fresh box always raises ValueError before any
    identity is written. This is the missing first-time counterpart: same
    stage-then-replace-then-verify shape and the same staged-agreement check,
    just with nothing to preserve or restore. On any failure, whatever was
    staged or already replaced is removed rather than "restored" (there is no
    prior identity to restore to), leaving a fresh appliance exactly as
    unactivated as before the attempt, safe to retry."""
    log=logger or logging.getLogger("anyaicam.reenrollment"); activation=validate_activation_response(activation_response,expected_cloud_id)
    agent_path=Path(config.config_dir)/"agent.json"; credential_path=config.credential_file; vms_path=Path(vms_identity_path); paths=(agent_path,credential_path,vms_path)
    existing=[p for p in paths if p.is_file()]
    if existing: raise ValueError("Cannot perform first-time enrollment: identity file(s) already exist: "+", ".join(str(p) for p in existing)+". Use coordinated_reenroll() to replace an existing activation instead.")
    agent=asdict(config); agent["cloud_id"]=activation["cloud_id"]
    credential={"appliance_id":activation["appliance_id"],"credential_id":activation["credential_id"],"credential":activation["credential"]}
    vms={"appliance_id":activation["appliance_id"],"cloud_id":activation["cloud_id"],"credential":activation["credential"],"customer_id":activation["customer_id"],"site_id":activation["site_id"],"partner_id":activation["partner_id"],"activated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"activation_version":1}
    staged={}; touched=[]
    try:
        for p,value in ((agent_path,agent),(credential_path,credential),(vms_path,vms)): staged[p]=_stage(p,value,prefix="enroll")
        a,c,v=(_read(staged[p]) for p in paths)
        if {(a.get("cloud_id"),c.get("appliance_id")),(v.get("cloud_id"),v.get("appliance_id")),(activation["cloud_id"],activation["appliance_id"])}.__len__()!=1 or c.get("credential")!=v.get("credential"): raise ValueError("Staged identity documents do not agree.")
        for p in paths: replace_file(staged[p],p); os.chmod(p,0o600); touched.append(p)
        restart_service()
        if not verify_authentication(activation): raise RuntimeError("Control-plane authentication failed after first-time enrollment.")
    except Exception as error:
        for p in touched:
            try:p.unlink()
            except FileNotFoundError:pass
        log.error("First-time appliance enrollment failed; no identity was left behind (%s).",type(error).__name__)
        raise ReenrollmentError("First-time appliance enrollment failed; the appliance remains unactivated.") from error
    finally:
        for p in staged.values():
            try:p.unlink()
            except FileNotFoundError:pass
    log.info("First-time appliance enrollment completed and control-plane authentication succeeded.")
    return {"cloud_id":activation["cloud_id"],"appliance_id":activation["appliance_id"]}
