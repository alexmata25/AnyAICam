from urllib.parse import urlsplit


def safe_redirect(destination: str,default: str='/') -> str:
    if not destination or '\\' in destination or destination.startswith('//'): return default
    parsed=urlsplit(destination)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith('/'): return default
    return destination
