"""Usable-account lookup by email for webhook reconciliation (VMS 1.2.7, Codex review 2026-10-16).

A neutral module so camera-plan (customer_entitlements), hardware-order (hardware_orders) and analytics-add-on
(analytics_entitlements) reconciliation share one rule without importing each other: those modules are kept
deliberately separate (see their separation-contract tests).

The rule: when a Stripe event carries no authoritative customer id, the purchase may only be matched by email to a
usable account. A rejected registration's account is 'suspended' (customer_registration.reject_registration), and the
test-account cleanup (Requirement #11) tombstones accounts as 'closed_test'. Such accounts never match; the caller then
records a pending link that resolves only when a verified account for that email exists.
"""
from __future__ import annotations

from typing import Optional

from partner_db import row

INACTIVE_CUSTOMER_STATUSES = ("suspended", "closed", "closed_test", "rejected", "deleted")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def find_usable_customer_by_email(email: str) -> Optional[dict]:
    """The usable customer account for an email, or None. Inactive accounts never match."""
    normalized = normalize_email(email)
    if not normalized:
        return None
    placeholders = ",".join("?" * len(INACTIVE_CUSTOMER_STATUSES))
    return row(f"SELECT * FROM customers WHERE lower(email)=? AND COALESCE(lower(status),'active') NOT IN ({placeholders})",
               (normalized, *INACTIVE_CUSTOMER_STATUSES))
