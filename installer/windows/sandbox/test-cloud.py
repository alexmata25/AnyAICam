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


def build_app(data: Path, app_dir: Path):
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

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', required=True)
    parser.add_argument('--data', required=True)
    parser.add_argument('--host', default='cloud.anyaicam.test')
    parser.add_argument('--port', type=int, default=8443)
    parser.add_argument('--certificates-only', action='store_true')
    args = parser.parse_args()
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    make_certificates(data / 'tls', args.host)
    if args.certificates_only:
        return
    app = build_app(data, Path(args.app))
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=args.port, ssl_certfile=str(data / 'tls' / 'server.pem'),
                ssl_keyfile=str(data / 'tls' / 'server.key'), log_level='info')


if __name__ == '__main__':
    main()
