import hashlib
import hmac
import time
from base64 import urlsafe_b64decode,urlsafe_b64encode

from cloud_config import settings


def sign(value: str,ttl_seconds=3600) -> str:
    payload=f'{int(time.time())+ttl_seconds}:{value}'; signature=hmac.new(settings.app_secrets[0].encode(),payload.encode(),hashlib.sha256).hexdigest(); return urlsafe_b64encode(f'{payload}:{signature}'.encode()).decode()


def unsign(token: str):
    try:
        decoded=urlsafe_b64decode(token.encode()).decode(); expires,value,signature=decoded.split(':',2); payload=f'{expires}:{value}'
        if int(expires)<time.time(): return None
        return value if any(hmac.compare_digest(signature,hmac.new(secret.encode(),payload.encode(),hashlib.sha256).hexdigest()) for secret in settings.app_secrets) else None
    except Exception: return None
