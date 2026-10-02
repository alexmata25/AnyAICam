"""Is this Stripe payment / order TEST mode? (2026-09-28)

Decided only from trusted server-side state, never from anything a
browser sends:
  1. the Stripe event's `livemode` (the webhook route verifies Stripe's
     signature before any of this runs);
  2. Stripe's own object ids stored on the order: Checkout Session ids are
     `cs_test_...` in test mode and `cs_live_...` in live mode;
  3. the server's configured secret key (`sk_test_`/`rk_test_` vs live).
Any trusted signal saying "test" makes it test -- a test payment must
never be presented as a real one. A live key with a live session is live.
"""
from __future__ import annotations

import os

TEST_SUBJECT_PREFIX = "[TEST] "
TEST_BANNER_TEXT = "TEST MODE — NO REAL CHARGE. This is a Stripe test-mode transaction; no money was taken and nothing will ship."


def server_key_is_test() -> bool | None:
    key = os.environ.get("ANYAICAM_STRIPE_SECRET_KEY", "").strip()
    if key.startswith(("sk_test_", "rk_test_")):
        return True
    if key.startswith(("sk_live_", "rk_live_")):
        return False
    return None


def _session_id(event: dict | None, order: dict | None, session_id: str | None) -> str:
    if session_id:
        return str(session_id)
    if order and order.get("stripe_checkout_session_id"):
        return str(order["stripe_checkout_session_id"])
    obj = ((event or {}).get("data") or {}).get("object") or {}
    candidate = str(obj.get("id") or "")
    return candidate if candidate.startswith("cs_") else ""


def is_test_mode(*, event: dict | None = None, order: dict | None = None, session_id: str | None = None) -> bool:
    if event is not None and event.get("livemode") is False:
        return True
    sid = _session_id(event, order, session_id)
    if sid.startswith("cs_test_"):
        return True
    if server_key_is_test() is True:
        return True
    return False


def decorate_email(subject: str, text: str, html: str | None) -> tuple[str, str, str | None]:
    """Unmistakable test-mode email: [TEST] subject and a banner on top."""
    if not subject.startswith(TEST_SUBJECT_PREFIX):
        subject = TEST_SUBJECT_PREFIX + subject
    text = f"*** {TEST_BANNER_TEXT} ***\n\n{text}"
    if html is not None:
        banner = ('<div style="background:#b42318;color:#ffffff;font-weight:700;font-size:16px;padding:12px 16px;'
                  'border-radius:6px;margin:0 0 16px 0;text-align:center">TEST MODE — NO REAL CHARGE'
                  '<div style="font-weight:400;font-size:13px;margin-top:4px">This is a Stripe test-mode transaction; '
                  'no money was taken and nothing will ship.</div></div>')
        html = banner + html
    return subject, text, html
