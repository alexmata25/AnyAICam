#!/usr/bin/env bash
set -Eeuo pipefail
work=/home/alejandro-mata/anyaicam-clean-validation
[[ "$(hostname)" == alejandro-mata-930MBE ]] || exit 99
[[ "$(cat /sys/class/dmi/id/product_name)" == 930MBE ]] || exit 99
[[ "$(lsblk -dn -o SERIAL /dev/nvme0n1 | xargs)" == 193077800308 ]] || exit 99
chmod 700 "$work"
chmod 600 "$work/owner-login.txt"
ca="$work/lab-ca.crt"
expected=49:F5:94:DC:00:3B:DF:8C:23:11:A8:52:0F:6A:EE:B2:E8:8F:33:D8:7B:E2:1E:29:1B:C6:EC:03:C1:A1:55:12
actual="$(openssl x509 -in "$ca" -noout -fingerprint -sha256 | cut -d= -f2)"
[[ "$actual" == "$expected" ]] || { echo "Wrong lab CA"; exit 99; }
echo "Samsung verified. Preparing only this fresh test machine's private lab access."
echo "Enter the Samsung administrator password HERE. Never send it in chat."
sudo -v
sudo install -m 644 "$ca" /usr/local/share/ca-certificates/anyaicam-installer-lab-ca.crt
sudo update-ca-certificates
sudo python3 - <<'PY'
import json,pathlib
hosts=pathlib.Path('/etc/hosts')
lines=hosts.read_text().splitlines()
for line in lines:
    fields=line.split('#',1)[0].split()
    if len(fields)>1 and 'installer-test.test' in fields[1:]:
        assert fields[0]=='100.27.41.127','Conflicting lab DNS mapping'
        break
else:
    with hosts.open('a') as out: out.write('\n100.27.41.127 installer-test.test # disposable AnyAiCam installer lab\n')
policy=pathlib.Path('/etc/firefox/policies/policies.json')
policy.parent.mkdir(parents=True,exist_ok=True)
data=json.loads(policy.read_text()) if policy.exists() else {}
certs=data.setdefault('policies',{}).setdefault('Certificates',{}).setdefault('Install',[])
cert='/home/alejandro-mata/anyaicam-clean-validation/lab-ca.crt'
if cert not in certs: certs.append(cert)
policy.write_text(json.dumps(data,indent=2)+'\n')
policy.chmod(0o644)
PY
curl --fail --silent --show-error https://installer-test.test/customer-login.html -o "$work/lab-login-page.html"
printf 'HOST=%s\nCA_FINGERPRINT=%s\nHTTPS_LOGIN_FETCH=PASS\n' "$(hostname)" "$actual" > "$work/lab-network-ready.txt"
echo "Private HTTPS portal verified with normal certificate validation."
echo "The test account is in the local owner-login.txt file being opened."
echo "Close/reopen Firefox if it was already running, so it loads the test CA policy."
echo "Sign in to the isolated portal; go to My Subscription > Install AnyAiCam."
echo "Click Download AnyAiCam. Do not open public/staging/production sites."
xdg-open "$work/owner-login.txt" >/dev/null 2>&1 &
firefox https://installer-test.test/customer-login.html >/dev/null 2>&1 &
read -r -p "Press Enter to close this preparation terminal. "
