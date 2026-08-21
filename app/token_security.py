import hashlib
import hmac
import time
from base64 import urlsafe_b64decode,urlsafe_b64encode

from cloud_config import settings


def sign(value: str,ttl_seconds=3600) -> str:
    # Trailing '=' base64 padding makes http.cookies (used by Starlette's
    # Response.set_cookie) wrap the whole Set-Cookie value in double quotes.
    # Browsers/curl store and resend that value verbatim, quotes included,
    # but Starlette's Cookie-header parser unquotes it back on read -- so a
    # double-submit token read from document.cookie would carry quotes the
    # server-parsed cookie never has, and comparison would never match.
    # Padding is recoverable at verify time, so strip it here.
    payload=f'{int(time.time())+ttl_seconds}:{value}'; signature=hmac.new(settings.app_secrets[0].encode(),payload.encode(),hashlib.sha256).hexdigest(); return urlsafe_b64encode(f'{payload}:{signature}'.encode()).decode().rstrip('=')


def unsign(token: str):
    try:
        # Re-add padding before decoding. A legacy, already-padded token
        # (issued before this fix, still sitting in a browser's cookie
        # store) is already a multiple of 4 chars, so this is a no-op for
        # it -- both old and new tokens verify cryptographically.
        padded=token+'='*(-len(token)%4)
        decoded=urlsafe_b64decode(padded.encode()).decode(); expires,value,signature=decoded.split(':',2); payload=f'{expires}:{value}'
        if int(expires)<time.time(): return None
        return value if any(hmac.compare_digest(signature,hmac.new(secret.encode(),payload.encode(),hashlib.sha256).hexdigest()) for secret in settings.app_secrets) else None
    except Exception: return None
