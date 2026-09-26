"""Provisioning-service interface for appliance identity.

AWS-authoritative onboarding rework, Phase 1 (see docs -- this module has no
docs/ counterpart yet; the contract lives here and in
tests/test_provisioning_service.py until Phase 2/3 land).

The VMS/browser must never mint appliance identity. Before this module
existed, app/partner_workspace.py generated its own Cloud ID and activation
token locally (`'AIC-' + secrets.token_hex(4)`, `secrets.token_urlsafe(24)`)
at the moment a partner finished the "Add New Customer" wizard. That is
exactly the authority this module takes over: every Cloud ID, activation
token, provisioning QR payload, customer/site/appliance relationship, and
entitlement/license state now comes from a ProvisioningBackend, never from
`secrets`/`uuid` calls inside main.py or partner_workspace.py.

Two implementations exist:

- MockProvisioningBackend: a self-contained, in-memory-plus-JSON-file stand
  in for AWS, used for all development/testing so the full wizard can be
  exercised without any production AWS enrollment. It is the ONLY place in
  this module allowed to mint a Cloud ID or activation token -- it is
  standing in for AWS, which is what actually owns that authority.
- AwsProvisioningBackend: the real integration point. It intentionally
  raises ProvisioningBackendUnavailable unconditionally right now -- Phase 1
  does not touch production AWS. Wiring it up to a real AWS API is future
  work; get_provisioning_backend() is the single seam the rest of the app
  goes through, so that future work does not require touching
  partner_workspace.py or main.py again.

Request/response contract
--------------------------
provision(order: dict, *, idempotency_key: str) -> dict

  `order` (everything the partner-onboarding wizard already collects):
    customer_name, company, email, phone, status,
    site_name,
    appliance_type, camera_count, resolution, recording_mode,
    retention_days, analytics_addons (list[str]),
    deployment_mode ("local" | "hybrid" | "cloud"),
    order_reference (optional; pricing/order id from the quote step)

  `idempotency_key` scopes retries: calling provision() twice with the same
  key returns the SAME appliance record (same Cloud ID, same customer_id/
  site_id/appliance_id) instead of minting a second one. Callers derive it
  from something stable about the order (see partner_workspace.py's call
  site) -- this module does not guess one.

  Returns:
    {
      "appliance_id": str,
      "cloud_id": str,               # e.g. "AIC-XXXXXXXX"
      "activation_token": str,       # plaintext -- ONLY returned here, at
                                      # initial issuance. Callers must hash
                                      # it for local storage and never log
                                      # or re-return the plaintext again.
      "provisioning_qr_payload": str,  # "{cloud_id}|{activation_token}",
                                        # matches the existing BarcodeDetector
                                        # split(...) already in Wizard B.
      "customer_id": str,
      "site_id": str,
      "entitlement": {"plan": str, "camera_limit": int, "status": str},
      "provisioning_status": "provisioned",
    }

get_status(cloud_id: str) -> dict | None
  Cloud-reported state for the appliance-status step. Never includes the
  activation token (plaintext or hashed). Returns None if the backend has
  no record for this cloud_id.
    {
      "cloud_id": str, "appliance_id": str, "customer_id": str,
      "site_id": str, "provisioning_status": str,
      "online_status": "online" | "offline",
      "last_check_in": str | None, "software_version": str,
      "entitlement": {...},
    }

verify_link(cloud_id: str, activation_token: str) -> dict | None
  Used by POST /api/customer/appliances/link. Returns the same shape as
  get_status() on success (never the token), or None if the cloud_id is
  unknown or the token does not match -- deliberately one outcome for both,
  so this endpoint can't be used to enumerate valid Cloud IDs.

Errors
------
ProvisioningBackendUnavailable is the only exception this module raises for
backend-reachability failures; callers turn it into a 503, not a 500 or a
silent local fallback (AWS being down must be visible, not quietly papered
over with locally-minted identity -- that would defeat the whole point).
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4


class ProvisioningBackendUnavailable(Exception):
    """Raised when the provisioning backend (mock or real AWS) cannot be
    reached or fails. Callers must surface this as a 503, never fall back
    to minting identity locally -- see this module's docstring."""


class ProvisioningBackend:
    """Interface every backend implements. Not a Protocol/ABC on purpose --
    kept as plain duck-typing so MockProvisioningBackend and a future real
    AwsProvisioningBackend both work as drop-in replacements for each other
    and for a test double, without an import-time dependency on `abc` or
    `typing.Protocol` machinery this module doesn't otherwise need."""

    def provision(self, order: dict, *, idempotency_key: str) -> dict:
        raise NotImplementedError

    def get_status(self, cloud_id: str) -> Optional[dict]:
        raise NotImplementedError

    def verify_link(self, cloud_id: str, activation_token: str) -> Optional[dict]:
        raise NotImplementedError


class MockProvisioningBackend(ProvisioningBackend):
    """Development/test stand-in for AWS. Persists to a JSON file so
    repeated requests within the same process (and across a container
    restart, matching how every other piece of local state in this app
    persists) see a consistent, idempotent record -- not just an in-memory
    dict that resets on the next request in a different worker.

    Storage path defaults under RECORDINGS_FOLDER's real convention
    (/app/recordings inside the container) so it lands on the same
    persistent volume as users.json/cameras.json, not inside /opt/anyaicam.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else Path(
            os.environ.get("ANYAICAM_MOCK_PROVISIONING_PATH", "/app/recordings/mock_aws_provisioning.json")
        )
        self._lock = threading.Lock()

    # ---- storage -----------------------------------------------------
    def _load(self) -> dict:
        if not self.path.exists():
            return {"appliances_by_cloud_id": {}, "idempotency_index": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"appliances_by_cloud_id": {}, "idempotency_index": {}}
        data.setdefault("appliances_by_cloud_id", {})
        data.setdefault("idempotency_index", {})
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".mock-aws-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ---- ProvisioningBackend -------------------------------------------
    def provision(self, order: dict, *, idempotency_key: str) -> dict:
        if not idempotency_key:
            raise ValueError("idempotency_key is required so a retried order does not mint a second appliance.")
        with self._lock:
            data = self._load()
            existing_cloud_id = data["idempotency_index"].get(idempotency_key)
            if existing_cloud_id and existing_cloud_id in data["appliances_by_cloud_id"]:
                record = data["appliances_by_cloud_id"][existing_cloud_id]
                return self._public_provision_response(record, activation_token=record["_activation_token"])

            cloud_id = "AIC-" + secrets.token_hex(4).upper()
            while cloud_id in data["appliances_by_cloud_id"]:  # astronomically unlikely, but stay correct
                cloud_id = "AIC-" + secrets.token_hex(4).upper()
            activation_token = secrets.token_urlsafe(24)
            appliance_id = uuid4().hex[:10]
            customer_id = order.get("customer_id") or uuid4().hex[:10]
            site_id = order.get("site_id") or uuid4().hex[:10]
            camera_count = max(1, int(order.get("camera_count") or 1))
            entitlement = {
                "plan": "trial" if order.get("status") == "trial" else "standard",
                "camera_limit": camera_count,
                "status": "active",
            }
            record = {
                "cloud_id": cloud_id,
                "appliance_id": appliance_id,
                "customer_id": customer_id,
                "site_id": site_id,
                "provisioning_status": "provisioned",
                "online_status": "offline",
                "last_check_in": None,
                "software_version": "Not installed",
                "entitlement": entitlement,
                "created_at": time.time(),
                "_activation_token": activation_token,  # never returned by get_status()/verify_link()
            }
            data["appliances_by_cloud_id"][cloud_id] = record
            data["idempotency_index"][idempotency_key] = cloud_id
            self._save(data)
            return self._public_provision_response(record, activation_token=activation_token)

    def get_status(self, cloud_id: str) -> Optional[dict]:
        with self._lock:
            data = self._load()
            record = data["appliances_by_cloud_id"].get(cloud_id)
            return self._public_status_response(record) if record else None

    def verify_link(self, cloud_id: str, activation_token: str) -> Optional[dict]:
        if not cloud_id or not activation_token:
            return None
        with self._lock:
            data = self._load()
            record = data["appliances_by_cloud_id"].get(cloud_id)
            if not record or not secrets.compare_digest(record["_activation_token"], activation_token):
                return None
            return self._public_status_response(record)

    @staticmethod
    def _public_status_response(record: dict) -> dict:
        return {
            "cloud_id": record["cloud_id"],
            "appliance_id": record["appliance_id"],
            "customer_id": record["customer_id"],
            "site_id": record["site_id"],
            "provisioning_status": record["provisioning_status"],
            "online_status": record["online_status"],
            "last_check_in": record["last_check_in"],
            "software_version": record["software_version"],
            "entitlement": record["entitlement"],
        }

    def _public_provision_response(self, record: dict, *, activation_token: str) -> dict:
        response = self._public_status_response(record)
        response["activation_token"] = activation_token
        response["provisioning_qr_payload"] = f'{record["cloud_id"]}|{activation_token}'
        return response


class AwsProvisioningBackend(ProvisioningBackend):
    """Real integration point for production AWS. Deliberately unimplemented
    for Phase 1 -- this session does not touch production AWS. Selecting
    this backend (ANYAICAM_PROVISIONING_BACKEND=aws) fails closed rather
    than silently falling back to local generation, so a misconfigured
    deployment is loud, not quietly non-authoritative."""

    def provision(self, order: dict, *, idempotency_key: str) -> dict:
        raise ProvisioningBackendUnavailable(
            "AwsProvisioningBackend is not implemented yet; production AWS provisioning integration is future work."
        )

    def get_status(self, cloud_id: str) -> Optional[dict]:
        raise ProvisioningBackendUnavailable(
            "AwsProvisioningBackend is not implemented yet; production AWS provisioning integration is future work."
        )

    def verify_link(self, cloud_id: str, activation_token: str) -> Optional[dict]:
        raise ProvisioningBackendUnavailable(
            "AwsProvisioningBackend is not implemented yet; production AWS provisioning integration is future work."
        )


class UnavailableProvisioningBackend(ProvisioningBackend):
    """Always raises ProvisioningBackendUnavailable. Used by tests to
    exercise the "provisioning backend unavailable" scenario, and available
    for anyone wiring up a maintenance-mode deployment."""

    def provision(self, order: dict, *, idempotency_key: str) -> dict:
        raise ProvisioningBackendUnavailable("Provisioning backend is unavailable.")

    def get_status(self, cloud_id: str) -> Optional[dict]:
        raise ProvisioningBackendUnavailable("Provisioning backend is unavailable.")

    def verify_link(self, cloud_id: str, activation_token: str) -> Optional[dict]:
        raise ProvisioningBackendUnavailable("Provisioning backend is unavailable.")


_backend_instance: Optional[ProvisioningBackend] = None
_backend_lock = threading.Lock()


def get_provisioning_backend() -> ProvisioningBackend:
    """Single seam the rest of the app calls through. Selected by
    ANYAICAM_PROVISIONING_BACKEND (default "mock"); production AWS can
    replace the mock later by setting this to "aws" (once
    AwsProvisioningBackend is actually implemented) without any change to
    partner_workspace.py, main.py, or the browser workflow."""
    global _backend_instance
    with _backend_lock:
        if _backend_instance is None:
            kind = os.environ.get("ANYAICAM_PROVISIONING_BACKEND", "mock").strip().lower()
            if kind == "aws":
                _backend_instance = AwsProvisioningBackend()
            elif kind == "mock":
                _backend_instance = MockProvisioningBackend()
            else:
                raise ValueError(f"Unknown ANYAICAM_PROVISIONING_BACKEND={kind!r}; expected 'mock' or 'aws'.")
        return _backend_instance


def reset_provisioning_backend_for_tests() -> None:
    """Test-only: clears the cached singleton so a test can inject its own
    backend via a fresh get_provisioning_backend() call after changing the
    env var, or after monkeypatching _backend_instance directly."""
    global _backend_instance
    with _backend_lock:
        _backend_instance = None
