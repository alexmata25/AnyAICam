#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(hostname)" == ip-10-253-77-16 ]] || { echo WRONG_HELPER; exit 99; }
lab=/home/ubuntu/anyaicam-lab
test "$(cat "$lab/runtime-dependencies.exit")" = 0
test ! -e /etc/anyaicam-installer-lab/portal.env
test ! -e /var/lib/anyaicam-installer-lab/portal.db
test ! -e /app
sudo mkdir -p /app /var/lib/anyaicam-installer-lab /etc/anyaicam-installer-lab/tls
sudo cp -a "$lab/installer/payload/vms/app/." /app/
sudo chown -R ubuntu:ubuntu /app /var/lib/anyaicam-installer-lab
sudo chmod 700 /var/lib/anyaicam-installer-lab /etc/anyaicam-installer-lab /etc/anyaicam-installer-lab/tls
sudo openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out /etc/anyaicam-installer-lab/tls/lab-ca.key 2>/dev/null
sudo openssl req -x509 -new -sha256 -days 14 -key /etc/anyaicam-installer-lab/tls/lab-ca.key -out /etc/anyaicam-installer-lab/tls/lab-ca.crt -subj '/CN=AnyAiCam Disposable Installer Lab CA' -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign'
sudo openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out /etc/anyaicam-installer-lab/tls/portal.key 2>/dev/null
sudo openssl req -new -key /etc/anyaicam-installer-lab/tls/portal.key -out /etc/anyaicam-installer-lab/tls/portal.csr -subj '/CN=installer-test.test'
printf '%s\n' 'subjectAltName=DNS:installer-test.test' 'basicConstraints=critical,CA:FALSE' 'keyUsage=critical,digitalSignature,keyEncipherment' 'extendedKeyUsage=serverAuth' | sudo tee /etc/anyaicam-installer-lab/tls/portal.extensions >/dev/null
sudo openssl x509 -req -in /etc/anyaicam-installer-lab/tls/portal.csr -CA /etc/anyaicam-installer-lab/tls/lab-ca.crt -CAkey /etc/anyaicam-installer-lab/tls/lab-ca.key -CAcreateserial -days 10 -sha256 -extfile /etc/anyaicam-installer-lab/tls/portal.extensions -out /etc/anyaicam-installer-lab/tls/portal.crt
sudo chmod 600 /etc/anyaicam-installer-lab/tls/lab-ca.key /etc/anyaicam-installer-lab/tls/portal.key
sudo "$lab/runtime/bin/python" - <<'PY'
import os,pathlib,secrets
from cryptography.fernet import Fernet
base='https://installer-test.test'
config={
'ANYAICAM_ENV':'production','ANYAICAM_RUNTIME_ROLE':'cloud',
'ANYAICAM_DATABASE_BACKEND':'sqlite','ANYAICAM_PARTNER_DB':'/var/lib/anyaicam-installer-lab/portal.db',
'ANYAICAM_PUBLIC_PORTAL_URL':base,'ANYAICAM_PORTAL_URL':base,'ANYAICAM_PUBLIC_URL':base,
'ANYAICAM_VERSION':'1.1.1','ANYAICAM_BUILD_ID':'a1c81e0f492633a80198503e721fc6c277e6a8b3',
'ANYAICAM_API_BASE_URL':base+'/api/v1','ANYAICAM_APPLIANCE_API_URL':base,
'ANYAICAM_APPLIANCE_ACTIVATION_URL':base+'/api/appliance/activate',
'ANYAICAM_PUBLIC_WEBSITE_URL':base,
'ANYAICAM_PRODUCTION_PARTNER_URL':base+'/partner.html',
'ANYAICAM_PRODUCTION_CUSTOMER_URL':base+'/customer-login.html',
'ANYAICAM_PASSWORD_RESET_URL':base+'/reset-password','ANYAICAM_INVITATION_URL':base+'/partner.html',
'ANYAICAM_ALLOWED_ORIGINS':base,'ANYAICAM_TRUSTED_HOSTS':'installer-test.test',
'ANYAICAM_CSRF_ENABLED':'true','ANYAICAM_HTTPS_ONLY':'true','ANYAICAM_SECURE_COOKIES':'true',
'ANYAICAM_APP_SECRETS':secrets.token_urlsafe(48),'ANYAICAM_PORTAL_SECRET':secrets.token_urlsafe(48),
'ANYAICAM_CLAIM_FLOW_SECRET_KEY':Fernet.generate_key().decode(),
'ANYAICAM_STORAGE_BACKEND':'local','ANYAICAM_LOCAL_STORAGE_ROOT':'/var/lib/anyaicam-installer-lab/storage',
'ANYAICAM_EMAIL_BACKEND':'preview','ANYAICAM_EMAIL_PREVIEW_DIR':'/var/lib/anyaicam-installer-lab/email-preview',
'ANYAICAM_SMS_BACKEND':'preview','AWS_EC2_METADATA_DISABLED':'true',
}
path=pathlib.Path('/etc/anyaicam-installer-lab/portal.env')
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as output:
    output.write(''.join(k+'='+v+'\n' for k,v in config.items()))
PY
sudo bash -c "set -a; source /etc/anyaicam-installer-lab/portal.env; set +a; cd /app; /home/ubuntu/anyaicam-lab/runtime/bin/python /home/ubuntu/anyaicam-lab/seed-portal.py"
sudo chown -R ubuntu:ubuntu /var/lib/anyaicam-installer-lab
sudo tee /etc/systemd/system/anyaicam-installer-lab-portal.service >/dev/null <<'UNIT'
[Unit]
Description=Disposable AnyAiCam installer validation portal ONLY
After=network-online.target
[Service]
User=ubuntu
WorkingDirectory=/app
EnvironmentFile=/etc/anyaicam-installer-lab/portal.env
ExecStart=/home/ubuntu/anyaicam-lab/runtime/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/app /var/lib/anyaicam-installer-lab /home/ubuntu/anyaicam-lab
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT
sudo tee /etc/nginx/sites-available/anyaicam-installer-lab >/dev/null <<'NGINX'
server {
    listen 443 ssl;
    server_name installer-test.test;
    ssl_certificate /etc/anyaicam-installer-lab/tls/portal.crt;
    ssl_certificate_key /etc/anyaicam-installer-lab/tls/portal.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    access_log off;
    client_max_body_size 50m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
NGINX
sudo ln -s /etc/nginx/sites-available/anyaicam-installer-lab /etc/nginx/sites-enabled/anyaicam-installer-lab
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable --now anyaicam-installer-lab-portal
sudo systemctl reload nginx
sudo cp /etc/anyaicam-installer-lab/tls/lab-ca.crt "$lab/lab-ca.crt"
sudo chown ubuntu:ubuntu "$lab/lab-ca.crt"
openssl x509 -in "$lab/lab-ca.crt" -noout -fingerprint -sha256
echo LAB_PORTAL_BOOTSTRAP_FINISHED
