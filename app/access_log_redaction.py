"""Redact secrets from HTTP access-log lines (2026-09-28).

Password-reset links (/customer-reset-password?token=..., /reset-password
?token=...) and a few other flows carry a one-time secret in the query
string, and uvicorn's access log printed the full path -- so every reset
token landed in plain text in every container's log (recorded in
docs/customer-appliance-readiness-blockers.md). This filter rewrites the
values of sensitive query parameters to "<redacted>" before the line is
formatted; the request itself is untouched.
"""
from __future__ import annotations

import logging
import re

SENSITIVE_PARAMS = frozenset({
    "token", "reset_token", "access_token", "refresh_token", "id_token", "code", "password", "passwd",
    "secret", "client_secret", "key", "api_key", "apikey", "signature", "sig", "credential", "otp",
})
_QUERY_PAIR = re.compile(r"(?P<sep>[?&;])(?P<name>[^=&;#\s]+)=(?P<value>[^&;#\s]*)")


def redact_url(text: str) -> str:
    if not isinstance(text, str) or "=" not in text:
        return text

    def replace(match: re.Match) -> str:
        name = match.group("name")
        if name.lower() in SENSITIVE_PARAMS and match.group("value"):
            return f"{match.group('sep')}{name}=<redacted>"
        return match.group(0)

    return _QUERY_PAIR.sub(replace, text)


class RedactQuerySecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(redact_url(arg) if isinstance(arg, str) else arg for arg in record.args)
        elif isinstance(record.msg, str):
            record.msg = redact_url(record.msg)
        return True


_FILTER = RedactQuerySecretsFilter()


def install(logger_names=("uvicorn.access", "uvicorn.error", "gunicorn.access")) -> None:
    """Idempotent: attach the filter to the access loggers and their handlers."""
    for name in logger_names:
        target = logging.getLogger(name)
        if _FILTER not in target.filters:
            target.addFilter(_FILTER)
        for handler in target.handlers:
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)
