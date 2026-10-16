"""Which Stripe mode (test or live) each stored Stripe customer belongs to (2026-10-15).

The cloud has one Stripe secret key, and Stripe customer ids (cus_...) look the
same in test and live mode. Accounts that bought while the cloud ran a test key
keep test customer ids; once the key is live, Stripe answers "No such customer"
for them, and an account holding a test and a live id looks like "billing
records need attention". This module lets checkout and Manage billing use only
the customers of the key's own mode, without changing any stored record:

* The mode of a customer is learned once and cached in stripe_customer_modes:
  the ``livemode`` field of a successful ``GET /v1/customers/{id}`` is the
  source of truth; Stripe's "No such customer ... a similar object exists in
  test/live mode" answer is the only fallback signal. Any other answer
  (network error, 5xx, an unknown customer) leaves the mode UNKNOWN and is not
  cached, so a transient error is retried later and never decides anything.
* Page views use the cache only (``lookup=False``); the routes that create a
  checkout or open Manage billing look up uncached customers.
* UNKNOWN fails closed: an unknown customer is kept (never silently dropped)
  and checkout never creates a new Stripe customer while a stored one's mode is
  unknown -- so a transient error can never produce a duplicate customer.
* Mode-specific canonical customers live in stripe_customer_mode_bindings;
  the original stripe_customer_bindings rows are never rewritten.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from partner_db import connection, row

logger = logging.getLogger("anyaicam.stripe_customer_mode")
LIVE, TEST = True, False
_OTHER_MODE_HINT = {"a similar object exists in live mode": LIVE, "a similar object exists in test mode": TEST}


def current_livemode() -> bool | None:
    """True for a live key, False for a test key, None when no key is set."""
    import stripe_mode
    is_test = stripe_mode.server_key_is_test()
    return None if is_test is None else not is_test


def cached_livemode(stripe_customer_id: str) -> bool | None:
    try:
        found = row("SELECT livemode FROM stripe_customer_modes WHERE stripe_customer_id=?", (stripe_customer_id,))
    except Exception:  # table not migrated yet
        return None
    return None if not found else bool(found["livemode"])


def _remember(stripe_customer_id: str, livemode: bool, source: str) -> None:
    with connection() as db:
        db.execute("INSERT INTO stripe_customer_modes(stripe_customer_id,livemode,source,verified_at) VALUES(?,?,?,?) "
                   "ON CONFLICT(stripe_customer_id) DO NOTHING",
                   (stripe_customer_id, 1 if livemode else 0, source, datetime.now().isoformat()))


def _get_customer(stripe_customer_id: str) -> tuple[int, dict]:
    """(HTTP status, body) for GET /v1/customers/{id}; (0, {}) on a network error.
    The key is sent only to Stripe and never logged."""
    main = sys.modules["main"]
    request = Request(f"{main.STRIPE_API_BASE}/v1/customers/{stripe_customer_id}", method="GET",
                      headers={"Authorization": f"Bearer {main.STRIPE_SECRET_KEY}", "User-Agent": f"AnyAiCam-VMS/{main.APP_VERSION}"})
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode("utf-8") or "{}")
        except ValueError:
            return error.code, {}
    except (URLError, TimeoutError, ValueError, OSError):
        return 0, {}


def customer_livemode(stripe_customer_id: str | None, *, lookup: bool) -> bool | None:
    """The customer's mode: cached, or (lookup=True) asked once from Stripe.
    None when it cannot be determined right now."""
    if not stripe_customer_id or not str(stripe_customer_id).startswith("cus_"):
        return None
    cached = cached_livemode(stripe_customer_id)
    if cached is not None or not lookup:
        return cached
    main = sys.modules.get("main")
    if main is None or not getattr(main, "STRIPE_SECRET_KEY", ""):
        return None
    status, body = _get_customer(stripe_customer_id)
    if status == 200 and isinstance(body.get("livemode"), bool):
        _remember(stripe_customer_id, body["livemode"], "customer")
        return body["livemode"]
    if status == 404:
        message = str((body.get("error") or {}).get("message") or "")
        for hint, mode in _OTHER_MODE_HINT.items():
            if hint in message:
                _remember(stripe_customer_id, mode, "other_mode_hint")
                return mode
    logger.info("stripe_customer_mode.unknown status=%s", status)
    return None


def in_current_mode(stripe_customer_id: str | None, *, lookup: bool) -> bool | None:
    """True / False when both modes are known, None when either is unknown."""
    current = current_livemode()
    mode = customer_livemode(stripe_customer_id, lookup=lookup)
    if current is None or mode is None:
        return None
    return mode == current


def keep_for_current_mode(stripe_customer_ids, *, lookup: bool) -> set[str]:
    """Drops only ids known to belong to the OTHER mode; unknown ids stay
    (fail closed: they still block a second customer and need attention)."""
    return {cid for cid in stripe_customer_ids if in_current_mode(cid, lookup=lookup) is not False}


def mode_binding(customer_id: str) -> str | None:
    current = current_livemode()
    if current is None:
        return None
    try:
        found = row("SELECT stripe_customer_id FROM stripe_customer_mode_bindings WHERE customer_id=? AND livemode=?",
                    (customer_id, 1 if current else 0))
    except Exception:  # table not migrated yet
        return None
    return found["stripe_customer_id"] if found else None


def bind_for_current_mode(customer_id: str, stripe_customer_id: str) -> bool:
    """Bind once per (account, mode); never rebinds. True if bound to this id."""
    current = current_livemode()
    if current is None or not customer_id or not stripe_customer_id:
        return False
    with connection() as db:
        db.execute("INSERT INTO stripe_customer_mode_bindings(customer_id,livemode,stripe_customer_id,created_at) VALUES(?,?,?,?) "
                   "ON CONFLICT DO NOTHING", (customer_id, 1 if current else 0, stripe_customer_id, datetime.now().isoformat()))
    _remember(stripe_customer_id, current, "created_with_current_key")
    return mode_binding(customer_id) == stripe_customer_id
