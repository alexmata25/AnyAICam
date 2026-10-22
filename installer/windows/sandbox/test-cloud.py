"""A disposable AnyAiCam cloud for the Windows Sandbox cloud-linking validation
(2026-10-07). NEVER for production: it stands in for app.anyaicam.com inside
a throwaway VM so the installed agent can be claimed end to end.

It serves the REAL cloud routes from the installed AnyAiCam app code -- the
device claim (begin/status/complete), the portal claim confirmation, and the
appliance API (heartbeat, cameras, commands) -- on a fresh SQLite database,
over HTTPS with a certificate from its own throwaway CA (the validation trusts
that CA inside the Sandbox only). Test-only additions, under /test/:
  * an owner sign-in shortcut: a request with header X-Test-Owner: yes is the
    seeded customer owner (the real portal session check otherwise);
  * GET  /test/state    claims, appliances and commands, without secrets;
  * POST /test/command  queue a confirmed command for the linked appliance.
  * POST /test/entitlement  (2026-10-18) add a SYNTHETIC entitlement row for the test customer
                            {"product": vms_license|camera_slots_local|camera_slots_hybrid, "quantity": N};
  * GET  /test/capacity     (2026-10-18) the cloud's own capacity_breakdown() for the test customer.
With --signing-key (2026-10-18) the cloud signs with the SYNTHETIC Sandbox key (make-synthetic-keyset.py, key id
sandbox-test-*) instead of a random one, so a TEST-ONLY build carrying that public keyset accepts it end to end.
No production key, customer or database is ever used.
With --functional (2026-10-21, Blue; the F-L/F-H functional scenarios only) the cloud ALSO serves the real customer
camera routes (partner_workspace: scan, provision) for the test owner, and these test-only routes:
  * POST /test/owner               seed the test owner as a real cloud account (synthetic password, customer_owner grant)
  * POST /test/expire-pending-claims  make every pending link code expired (the expired-link checks)
  * POST /test/revoke-appliance    revoke the linked appliance exactly as /api/admin/appliances/{id}/revoke does
  * POST /test/entitlement-status  set one synthetic entitlement row active/cancelled (Hybrid -> Local)
  * GET  /test/cameras             the cloud's camera rows for the test customer (no credentials)
--functional is refused unless the cloud signs with a synthetic sandbox-test key (--signing-key) and runs as the
Windows Sandbox account (WDAGUtilityAccount): it can never be switched on next to a real cloud or a real customer.

    python test-cloud.py --app "C:\\Program Files\\AnyAiCam\\app" --data C:\\TestCloud
        [--host cloud.anyaicam.test] [--port 8443] [--certificates-only]
"""
import argparse
import datetime
import ipaddress
import json
import os
import sys
import uuid
from pathlib import Path

CUSTOMER_ID, SITE_ID, PARTNER_ID = 'test-customer', 'test-site', 'test-partner'
OWNER = {'id': 'test-owner', 'email': 'owner@example.test', 'role': 'customer_owner',
         'customer_id': CUSTOMER_ID, 'partner_id': PARTNER_ID, 'approved': 1}


def make_certificates(folder: Path, host: str) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    if (folder / 'server.pem').is_file():
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    valid = dict(not_valid_before=now - datetime.timedelta(hours=1), not_valid_after=now + datetime.timedelta(days=2))
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'AnyAiCam Sandbox Test CA (disposable)')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(valid['not_valid_before']).not_valid_after(valid['not_valid_after'])
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    server = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
              .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(valid['not_valid_before']).not_valid_after(valid['not_valid_after'])
              .add_extension(x509.SubjectAlternativeName([x509.DNSName(host), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
              .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
              .sign(ca_key, hashes.SHA256()))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (folder / 'ca.cer').write_bytes(ca.public_bytes(serialization.Encoding.DER))
    (folder / 'server.key').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (folder / 'server.pem').write_bytes(server.public_bytes(serialization.Encoding.PEM))


def build_app(data: Path, app_dir: Path, signing_key: Path | None = None, functional: bool = False):
    from cryptography.fernet import Fernet
    key_file = data / 'claim_flow_key'
    if not key_file.is_file():
        key_file.write_bytes(Fernet.generate_key())
    os.environ.update({
        'ANYAICAM_PARTNER_DB': str(data / 'cloud.db'),
        'ANYAICAM_RECORDINGS_FOLDER': str(data / 'recordings'),
        'ANYAICAM_CLAIM_FLOW_SECRET_KEY': key_file.read_text().strip(),
        'ANYAICAM_RUNTIME_ROLE': 'cloud',
    })
    (data / 'recordings').mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(app_dir))
    os.chdir(app_dir)
    import partner_db
    partner_db.initialize_database()
    now = datetime.datetime.now().isoformat()
    with partner_db.connection() as db:
        db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,created_at) VALUES(?,?,?,?)", (PARTNER_ID, 'Test Partner', 'approved', now))
        db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                   (CUSTOMER_ID, PARTNER_ID, 'Test Customer', OWNER['email'], 'active', now))
        db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (SITE_ID, CUSTOMER_ID, 'Test Site', now))
        if signing_key is not None:
            key = json.loads(signing_key.read_text(encoding='ascii'))
            if not str(key['key_id']).startswith('sandbox-test-'):
                raise SystemExit('refusing a signing key that is not a synthetic sandbox-test key')
            # Seeded before any request, so appliance_identity.ensure_signing_key() returns it (newest active key).
            db.execute("INSERT OR IGNORE INTO identity_signing_keys(key_id,public_key_b64,private_key_b64,created_at) VALUES(?,?,?,?)",
                       (key['key_id'], key['public_key_b64'], key['private_key_b64'], '2999-01-01T00:00:00'))

    import appliance_claims
    import appliance_cloud
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse
    real_identity = appliance_claims.partner_identity
    appliance_claims.partner_identity = lambda request: dict(OWNER) if request.headers.get('X-Test-Owner') == 'yes' else real_identity(request)

    app = FastAPI(title='AnyAiCam Sandbox test cloud')
    appliance_claims.register_appliance_claim_routes(app)
    appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *args, **kwargs: HTMLResponse(''))

    @app.get('/test/state')
    def test_state() -> dict:
        with partner_db.connection() as db:
            claims = [dict(r) for r in db.execute('SELECT device_id,status,customer_id,site_id FROM appliance_claims ORDER BY created_at').fetchall()]
            appliances = [dict(r) for r in db.execute('SELECT id,cloud_id,customer_id,site_id,software_version,last_check_in,online_status,camera_capacity FROM appliances').fetchall()]
            commands = [dict(r) for r in db.execute('SELECT id,command,status,error FROM appliance_commands ORDER BY created_at').fetchall()]
        return {'claims': claims, 'appliances': appliances, 'commands': commands}

    @app.post('/test/command')
    def test_command(request: Request, payload: dict) -> dict:
        if request.headers.get('X-Test-Owner') != 'yes':
            raise HTTPException(status_code=403, detail='test owner only')
        with partner_db.connection() as db:
            appliance = db.execute('SELECT id FROM appliances LIMIT 1').fetchone()
            if not appliance:
                raise HTTPException(status_code=409, detail='no linked appliance')
            command_id = uuid.uuid4().hex
            created = datetime.datetime.now()
            db.execute("INSERT INTO appliance_commands(id,appliance_id,command,payload_json,status,created_at,expires_at,created_by) VALUES(?,?,?,?,?,?,?,?)",
                       (command_id, appliance['id'], str(payload['command']), json.dumps({'confirmed': True}), 'pending',
                        created.isoformat(), (created + datetime.timedelta(minutes=30)).isoformat(), OWNER['email']))
        return {'id': command_id}

    @app.post('/test/entitlement')
    def test_entitlement(request: Request, payload: dict) -> dict:
        if request.headers.get('X-Test-Owner') != 'yes':
            raise HTTPException(status_code=403, detail='test owner only')
        product, quantity = str(payload['product']), int(payload['quantity'])
        if product not in ('vms_license', 'camera_slots_local', 'camera_slots_hybrid') or not 0 <= quantity <= 64:
            raise HTTPException(status_code=400, detail='synthetic products only')
        stamp = datetime.datetime.now().isoformat()
        with partner_db.connection() as db:
            db.execute("DELETE FROM customer_entitlements WHERE customer_id=? AND product=?", (CUSTOMER_ID, product))
            db.execute("INSERT INTO customer_entitlements(id,customer_id,product,camera_slot_quantity,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, CUSTOMER_ID, product, quantity, 'active', stamp, stamp))
        return {'product': product, 'quantity': quantity}

    @app.get('/test/capacity')
    def test_capacity() -> dict:
        import customer_entitlements
        return customer_entitlements.capacity_breakdown(CUSTOMER_ID)

    if functional:
        add_functional_routes(app, partner_db, appliance_claims, HTTPException, Request, HTMLResponse)
    return app


def add_functional_routes(app, partner_db, appliance_claims, HTTPException, Request, HTMLResponse) -> None:
    """F-L / F-H only (2026-10-21). Every route below is test-owner only and exists in this disposable cloud alone."""
    import partner_workspace
    # partner_workspace imported partner_identity by name: give it the same test-owner shortcut as the claim routes.
    partner_workspace.partner_identity = appliance_claims.partner_identity
    partner_workspace.register_partner_workspace_routes(app, shell=lambda *args, **kwargs: HTMLResponse(''))

    def owner_only(request) -> None:
        if request.headers.get('X-Test-Owner') != 'yes':
            raise HTTPException(status_code=403, detail='test owner only')

    @app.post('/test/owner')
    def test_owner(request: Request, payload: dict) -> dict:
        owner_only(request)
        password = str(payload['password'])
        if len(password) < 12:
            raise HTTPException(status_code=400, detail='synthetic password of 12+ characters')
        now = datetime.datetime.now().isoformat()
        with partner_db.connection() as db:
            db.execute('DELETE FROM identity_grants WHERE user_id=?', (OWNER['id'],))
            db.execute('DELETE FROM partner_users WHERE id=?', (OWNER['id'],))
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (OWNER['id'], PARTNER_ID, OWNER['email'], 'Test Owner', OWNER['role'], partner_db.password_hash(password), 1, CUSTOMER_ID, now))
            db.execute("INSERT INTO identity_grants(id,user_id,role,scope_type,scope_id,granted_at,granted_by) VALUES(?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, OWNER['id'], OWNER['role'], 'customer', CUSTOMER_ID, now, 'sandbox-test'))
        return {'email': OWNER['email'], 'role': OWNER['role']}

    @app.post('/test/expire-pending-claims')
    def test_expire_pending_claims(request: Request) -> dict:
        owner_only(request)
        past = (datetime.datetime.now() - datetime.timedelta(minutes=5)).isoformat()
        with partner_db.connection() as db:
            count = db.execute("UPDATE appliance_claims SET expires_at=? WHERE status='pending'", (past,)).rowcount
        return {'expired': count}

    @app.post('/test/revoke-appliance')
    def test_revoke_appliance(request: Request) -> dict:
        owner_only(request)
        now = datetime.datetime.now().isoformat()
        with partner_db.connection() as db:
            appliance = db.execute('SELECT id FROM appliances LIMIT 1').fetchone()
            if not appliance:
                raise HTTPException(status_code=409, detail='no linked appliance')
            # The same two statements as appliance_cloud.revoke_appliance (/api/admin/appliances/{id}/revoke).
            db.execute('UPDATE appliance_credentials SET revoked_at=? WHERE appliance_id=? AND revoked_at IS NULL', (now, appliance['id']))
            db.execute("UPDATE appliances SET state='revoked',online_status='revoked',credential_revoked_at=? WHERE id=?", (now, appliance['id']))
        return {'revoked': appliance['id']}

    @app.post('/test/entitlement-status')
    def test_entitlement_status(request: Request, payload: dict) -> dict:
        owner_only(request)
        product, status = str(payload['product']), str(payload['status'])
        if product not in ('vms_license', 'camera_slots_local', 'camera_slots_hybrid') or status not in ('active', 'cancelled'):
            raise HTTPException(status_code=400, detail='synthetic products and active/cancelled only')
        with partner_db.connection() as db:
            count = db.execute('UPDATE customer_entitlements SET status=?, updated_at=? WHERE customer_id=? AND product=?',
                               (status, datetime.datetime.now().isoformat(), CUSTOMER_ID, product)).rowcount
        return {'product': product, 'status': status, 'rows': count}

    @app.get('/test/cameras')
    def test_cameras(request: Request) -> dict:
        owner_only(request)
        with partner_db.connection() as db:
            columns = {r['name'] for r in db.execute('PRAGMA table_info(cameras)').fetchall()}
            wanted = [c for c in ('id', 'appliance_id', 'camera_number', 'device_key', 'name', 'status', 'onvif_endpoint') if c in columns]
            cameras = [dict(r) for r in db.execute(f"SELECT {','.join(wanted)} FROM cameras WHERE customer_id=?", (CUSTOMER_ID,)).fetchall()]
        return {'cameras': cameras}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', required=True)
    parser.add_argument('--data', required=True)
    parser.add_argument('--host', default='cloud.anyaicam.test')
    parser.add_argument('--port', type=int, default=8443)
    parser.add_argument('--certificates-only', action='store_true')
    parser.add_argument('--signing-key', help='synthetic sandbox-test signing key (TEST-ONLY build acceptance)')
    parser.add_argument('--functional', action='store_true', help='F-L/F-H functional routes (Windows Sandbox + synthetic key only)')
    args = parser.parse_args()
    if args.functional:
        # Refused anywhere but the disposable Windows Sandbox with the synthetic key (never next to a real cloud).
        if not args.signing_key:
            raise SystemExit('--functional needs the synthetic --signing-key')
        if os.environ.get('USERNAME', '').lower() != 'wdagutilityaccount':
            raise SystemExit('--functional runs only inside Windows Sandbox (WDAGUtilityAccount)')
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    make_certificates(data / 'tls', args.host)
    if args.certificates_only:
        return
    app = build_app(data, Path(args.app), Path(args.signing_key) if args.signing_key else None, functional=args.functional)
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=args.port, ssl_certfile=str(data / 'tls' / 'server.pem'),
                ssl_keyfile=str(data / 'tls' / 'server.key'), log_level='info')


if __name__ == '__main__':
    main()
