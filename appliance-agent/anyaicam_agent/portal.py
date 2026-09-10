import json
import secrets
import time
import urllib.error
import urllib.request

FORBIDDEN={'username','password','camera_username','camera_password','rtsp_url','rtsp_urls','stream_url','stream_urls','credentials','secret','ip','ip_address','host','mac','mac_address','onvif_xaddrs'}


def sanitize(value):
    if isinstance(value,dict): return {key:sanitize(item) for key,item in value.items() if key.lower() not in FORBIDDEN}
    if isinstance(value,list): return [sanitize(item) for item in value]
    return value


class PortalError(RuntimeError):
    # RDM-2 Group 2E: status_code is additive and backward-compatible --
    # every existing raise site/caller that only ever passed a message
    # positionally keeps working unchanged (defaults to None). It lets a
    # caller distinguish HTTP response classes (e.g. 404 vs 409 vs a
    # generic network failure) without this module growing any new
    # exception types -- still exactly one error type for "the portal
    # request failed", just with an optional extra fact attached. None
    # means "no HTTP response was ever received" (a network-level
    # failure, or the pre-flight 'not activated' check below), not
    # "unknown HTTP status".
    def __init__(self,message,status_code=None):
        super().__init__(message)
        self.status_code=status_code


class PortalClient:
    def __init__(self,base_url,appliance_id=None,credential=None,timeout=20): self.base_url=base_url.rstrip('/'); self.appliance_id=appliance_id; self.credential=credential; self.timeout=timeout
    def request(self,method,path,payload=None,authenticated=True):
        body=json.dumps(sanitize(payload or {})).encode(); headers={'Content-Type':'application/json','User-Agent':'AnyAiCam-Agent/0.1'}
        if authenticated:
            if not self.appliance_id or not self.credential: raise PortalError('Appliance is not activated.')
            headers.update({'Authorization':'Bearer '+self.credential,'X-Appliance-ID':self.appliance_id,'X-Request-Timestamp':str(int(time.time())),'X-Request-Nonce':secrets.token_urlsafe(18)})
        request=urllib.request.Request(self.base_url+path,data=body if method!='GET' else None,headers=headers,method=method)
        try:
            with urllib.request.urlopen(request,timeout=self.timeout) as response: return json.loads(response.read().decode() or '{}')
        except urllib.error.HTTPError as error:
            try: detail=json.loads(error.read().decode()).get('detail',str(error))
            except Exception: detail=str(error)
            raise PortalError(detail,status_code=error.code) from error
        except (urllib.error.URLError,TimeoutError,OSError,json.JSONDecodeError) as error: raise PortalError(str(error)) from error
    def test(self): return self.request('GET','/api/appliance/config',authenticated=False)
    def activate(self,cloud_id,token): return self.request('POST','/api/appliance/activate',{'cloud_id':cloud_id,'activation_token':token},authenticated=False)
    # Phase 1 of the non-interactive/self-service claim flow (see
    # docs/non-interactive-activation-phase1-plan.md and
    # app/appliance_claims.py's own module docstring for the cloud-side
    # half these three methods talk to). Unauthenticated, exactly like
    # activate() above -- the device has no credential yet at any point
    # during this exchange, by definition. claim_code/claim_session_id/
    # claim_proof are bearer-equivalent secrets and, like credential and
    # activation_token above, are never added to FORBIDDEN: sanitize()
    # exists to strip camera-credential-shaped keys that should never
    # reach the wire through this generic path at all (see provisioning.py's
    # own comment on it being a second, independent layer of defense),
    # not to scrub values this exchange legitimately has to send. "No
    # secrets in logs" is satisfied the same way activate() already
    # satisfies it: nothing in this module logs a payload or a response,
    # and no caller of these three methods exists yet -- wiring the
    # interactive wizard (the only current call site pattern to follow)
    # to actually use this flow is explicitly deferred to a later phase,
    # so there is no logging call site to get wrong in this one.
    def claim_begin(self,device_id): return self.request('POST','/api/appliance/claim/begin',{'device_id':device_id},authenticated=False)
    def claim_status(self,claim_session_id): return self.request('POST','/api/appliance/claim/status',{'claim_session_id':claim_session_id},authenticated=False)
    def claim_complete(self,claim_session_id,claim_proof): return self.request('POST','/api/appliance/claim/complete',{'claim_session_id':claim_session_id,'claim_proof':claim_proof},authenticated=False)
