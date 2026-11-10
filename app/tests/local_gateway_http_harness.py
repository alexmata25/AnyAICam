"""Building blocks for isolated ASGI acceptance; no app or migration runs on import.

The application factory must wire the real routes to these fixtures. Execution
requires separate owner approval; this module does not enable checkout.
"""
from contextlib import asynccontextmanager, ExitStack
from dataclasses import dataclass, field
from unittest.mock import patch
import _sqlite3
import os
import socket
import sqlite3


@dataclass
class SyntheticStripe:
    """An explicit in-memory provider; unknown operations fail closed."""
    responses: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def get(self, path, purpose=None):
        self.calls.append((path, purpose))
        if path not in self.responses:
            raise RuntimeError("Unexpected synthetic Stripe request")
        return self.responses[path]


@dataclass
class Fixtures:
    db: sqlite3.Connection
    stripe: SyntheticStripe
    private_key: object
    customers: tuple = ("synthetic-customer-a", "synthetic-customer-b")
    appliances: tuple = ("synthetic-appliance-a", "synthetic-appliance-b")
    admin: str = "synthetic-global-admin"
    stack: object = None
    database_path: object = None
    cookies: dict = field(default_factory=dict)
    subscriptions: dict = field(default_factory=dict)
    app_module: object = None


def _deny_network(*args, **kwargs):
    raise RuntimeError("Network access is disabled in the acceptance harness")


@asynccontextmanager
async def isolated_client(app_factory):
    """Yield an in-process loopback-origin client and fixtures.

    The factory is responsible for wiring the real ASGI routes, test-only
    identity overrides, fixture schema and mocked provider. It must not call
    database initialization or migrations. No service socket is opened.
    Socket patches are a test guard, not a substitute for OS network isolation.
    """
    if os.environ.get("ANYAICAM_LOCAL_GATEWAY_CHECKOUT_ENABLED", "").strip().lower() == "true":
        raise RuntimeError("Checkout must remain disabled for this harness")
    with ExitStack() as stack:
        import tempfile
        from pathlib import Path
        from app.tests.gateway_http_guards import make_sqlite_connect_guard, make_sqlite_connection_class, sqlite_audit_scope
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="aac-gateway-http-"))).resolve()
        db_path = root / "disposable.sqlite3"
        stack.enter_context(sqlite_audit_scope())
        original_connect, original_class = sqlite3.connect, sqlite3.Connection
        confined_class = make_sqlite_connection_class(db_path, original_class)
        confined_connect = make_sqlite_connect_guard(db_path, original_connect, confined_class)
        for module in (sqlite3, _sqlite3):
            stack.enter_context(patch.object(module, "connect", confined_connect))
            stack.enter_context(patch.object(module, "Connection", confined_class))
        for name in ("connect", "connect_ex", "sendto"):
            stack.enter_context(patch.object(socket.socket, name, _deny_network))
        stack.enter_context(patch.object(socket, "create_connection", _deny_network))
        stack.enter_context(patch.object(socket, "getaddrinfo", _deny_network))
        import httpx
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        db = sqlite3.connect(db_path)
        stack.callback(db.close)
        db.row_factory = sqlite3.Row
        fixtures = Fixtures(db=db, stripe=SyntheticStripe(), private_key=Ed25519PrivateKey.generate(), stack=stack,
                            database_path=db_path)
        app = app_factory(fixtures)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            yield client, fixtures


def full_vms_app(fixtures):
    """Register the actual VMS ASGI app against disposable, synthetic state.

    This deliberately uses the project's database initializer, which runs
    migrations. The independent migration approval gate is mandatory. Invoke
    only in a dedicated approved test process with OS-level network isolation.
    """
    import base64
    import sys
    from pathlib import Path
    from datetime import datetime, timedelta
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
    if os.environ.get("ANYAICAM_APPROVED_DISPOSABLE_MIGRATIONS") != "1":
        raise RuntimeError("Separate owner approval for disposable migrations is required")
    if any(name in sys.modules for name in ("main", "cloud_config", "runtime_paths", "partner_db",
                                            "database_backend", "stripe_state", "checkout_guard",
                                            "per_camera_billing", "customer_entitlements")):
        raise RuntimeError("Run the full VMS fixture in a fresh dedicated interpreter")
    stack = fixtures.stack
    db_path = fixtures.database_path
    root = db_path.parent
    os_base = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP") if key in os.environ}
    os_base.update({"ANYAICAM_DATABASE_BACKEND": "sqlite", "ANYAICAM_PARTNER_DB": str(db_path),
                    "ANYAICAM_RECORDINGS_FOLDER": str(root / "recordings"),
                    "ANYAICAM_STATIC_FOLDER": str(Path(__file__).resolve().parents[1] / "static"),
                    "ANYAICAM_ENV": "development", "ANYAICAM_RUNTIME_ROLE": "edge",
                    "ANYAICAM_STORAGE_BACKEND": "local", "ANYAICAM_EMAIL_BACKEND": "preview",
                    "ANYAICAM_STRIPE_WEBHOOK_SECRET": "synthetic-webhook-secret",
                    "ANYAICAM_STRIPE_PRICE_LOCAL_GATEWAY_REMOTE": "price_synthetic_999",
                    "ANYAICAM_LOCAL_GATEWAY_CHECKOUT_ENABLED": "true",
                    "USERPROFILE": str(root), "APPDATA": str(root / "appdata"),
                    "LOCALAPPDATA": str(root / "localappdata"), "PROGRAMDATA": str(root / "programdata")})
    stack.enter_context(patch.dict(os.environ, os_base, clear=True))
    import subprocess
    stack.enter_context(patch.object(subprocess, "Popen", _deny_network))
    stack.enter_context(patch.object(os, "system", _deny_network))
    app_root = str(Path(__file__).resolve().parents[1])
    stack.enter_context(patch.object(sys, "path", [app_root, *sys.path]))
    # dotenv files can contain live configuration and are never loaded.
    try:
        import dotenv
        stack.enter_context(patch.object(dotenv, "load_dotenv", return_value=False))
    except ImportError:
        pass
    from database_backend import override_target
    stack.enter_context(override_target(sqlite_path=db_path))
    import partner_db
    import stripe_state
    stack.enter_context(patch.object(stripe_state, "stripe_get", side_effect=fixtures.stripe.get))
    stack.enter_context(patch.object(stripe_state, "current_subscription", side_effect=lambda sub_id: fixtures.subscriptions.get(sub_id)))
    import checkout_guard
    stack.enter_context(patch.object(checkout_guard, "canonical_stripe_customer", side_effect=lambda customer_id, **kwargs: "cus_" + customer_id))
    stack.enter_context(patch.object(checkout_guard, "create_session", return_value={"url": "https://checkout.example.test/synthetic"}))
    import per_camera_billing
    stack.enter_context(patch.object(per_camera_billing, "own_pc_licence_reversed", return_value=False))
    partner_db.initialize_database()  # disposable schema; only after the explicit gate above
    import main
    import partner_portal
    import appliance_identity
    fixtures.app_module = main
    main.PUBLIC_BASE_URL = "https://gateway.example.test"
    stack.enter_context(patch.object(main, "STRIPE_WEBHOOK_SECRET", "synthetic-webhook-secret"))
    stack.enter_context(patch.object(main, "require_stripe_price_matches_catalog", return_value=None))
    raw_private = fixtures.private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    raw_public = fixtures.private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    public_keys = {"synthetic-signing-key": base64.b64encode(raw_public).decode()}
    stack.enter_context(patch.object(appliance_identity, "ensure_signing_key", return_value={
        "key_id": "synthetic-signing-key", "private_key_b64": base64.b64encode(raw_private).decode()}))
    stack.enter_context(patch.object(appliance_identity, "active_public_keys", return_value=public_keys))
    now = datetime.now().isoformat()
    with partner_db.connection() as db:
        db.execute("INSERT INTO partners(id,name,created_at) VALUES(?,?,?)", ("synthetic-partner", "Synthetic", now))
        for suffix in ("a", "b"):
            customer, appliance = "synthetic-customer-" + suffix, "synthetic-appliance-" + suffix
            site, email = "site-" + suffix, "owner-" + suffix + "@example.test"
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                       (customer, "synthetic-partner", customer, email, "active", now))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site, customer, site, now))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at,activation_status,state,gateway_edition) VALUES(?,?,?,?,?,?,?,?)",
                       (appliance, customer, site, "AIC-" + suffix.upper(), now, "activated", "online", "local_gateway"))
            db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)",
                       ("credential-" + suffix, appliance, partner_db.password_hash("synthetic-credential-" + suffix), now))
            db.execute("INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       ("license-" + suffix, customer, "vms_license", 4, "active", now, now))
            db.execute("INSERT INTO partner_users(id,email,name,role,password_hash,approved,customer_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       ("owner-" + suffix, email, email, "customer_owner", "synthetic", 1, customer, now))
            fixtures.cookies[suffix] = partner_portal._token(email, "customer_owner", customer_id=customer, ttl_seconds=3600)
        db.execute("INSERT INTO partner_users(id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?)",
                   ("synthetic-admin", "admin@example.test", "Synthetic admin", "administrator", "synthetic", 1, now))
        appliance_identity.create_grant(db, user_id="synthetic-admin", role="administrator", scope_type="global", scope_id=None, granted_by="synthetic", now=now)
        account = db.execute("SELECT authorization_version FROM partner_users WHERE id=?", ("synthetic-admin",)).fetchone()
        db.execute("INSERT INTO user_sessions(id,user_id,email,role,created_at,expires_at,authorization_version_at_login,auth_source,mfa_state) VALUES(?,?,?,?,?,?,?,?,?)",
                   ("synthetic-admin-session", "synthetic-admin", "admin@example.test", "administrator", now,
                    (datetime.now() + timedelta(hours=1)).isoformat(), account["authorization_version"], "mfa", "verified"))
        fixtures.cookies["admin"] = partner_portal._token("admin@example.test", "administrator", session_id="synthetic-admin-session", ttl_seconds=3600)
    return main.app
