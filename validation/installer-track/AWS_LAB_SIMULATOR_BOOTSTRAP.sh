set -Eeuo pipefail
[[ "$(hostname)" == ip-10-253-77-16 ]] || exit 99
test ! -e /etc/wireguard/wg-install-lab.conf
sudo bash -s <<'WG'
set -Eeuo pipefail
umask 077
mkdir -p /etc/wireguard
wg genkey > /etc/wireguard/anyaicam-lab.private
wg pubkey < /etc/wireguard/anyaicam-lab.private > /etc/wireguard/anyaicam-lab.public
{
printf '[Interface]\nAddress = 192.168.253.10/24\nListenPort = 51820\nMTU = 1280\nPrivateKey = '
cat /etc/wireguard/anyaicam-lab.private
printf 'PostUp = ip link set %%i multicast on\n\n[Peer]\nPublicKey = awmFpQcNajwf+RV0PFvL23xOqETSxE2vizBEQgccEgg=\nAllowedIPs = 192.168.253.20/32, 239.255.255.250/32\n'
} > /etc/wireguard/wg-install-lab.conf
chmod 600 /etc/wireguard/wg-install-lab.conf
systemctl enable --now wg-quick@wg-install-lab
WG
sudo cat /etc/wireguard/anyaicam-lab.public
sim=/home/ubuntu/anyaicam-lab/simulator
test -f "$sim/mediamtx"
sudo tee /etc/systemd/system/anyaicam-lab-simulator-rtsp.service >/dev/null <<'UNIT'
[Unit]
Description=Temporary synthetic camera MediaMTX ONLY
After=wg-quick@wg-install-lab.service
Requires=wg-quick@wg-install-lab.service
[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/anyaicam-lab/simulator
ExecStart=/home/ubuntu/anyaicam-lab/simulator/mediamtx /home/ubuntu/anyaicam-lab/simulator/mediamtx.yml
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
[Install]
WantedBy=multi-user.target
UNIT
sudo tee /etc/systemd/system/anyaicam-lab-simulator-video.service >/dev/null <<'UNIT'
[Unit]
Description=Temporary synthetic FFmpeg test pattern ONLY
After=anyaicam-lab-simulator-rtsp.service
Requires=anyaicam-lab-simulator-rtsp.service
[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/anyaicam-lab/simulator
ExecStart=/bin/bash /home/ubuntu/anyaicam-lab/simulator/publish-test-video.sh
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
[Install]
WantedBy=multi-user.target
UNIT
sudo tee /etc/systemd/system/anyaicam-lab-simulator-onvif.service >/dev/null <<'UNIT'
[Unit]
Description=Temporary read-only synthetic ONVIF camera ONLY
After=wg-quick@wg-install-lab.service
Requires=wg-quick@wg-install-lab.service
[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/anyaicam-lab/simulator
ExecStart=/usr/bin/python3 /home/ubuntu/anyaicam-lab/simulator/onvif_simulator.py --serve --ip 192.168.253.10 --cidr 192.168.253.0/24
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now anyaicam-lab-simulator-rtsp anyaicam-lab-simulator-video anyaicam-lab-simulator-onvif
sudo systemctl is-active --quiet wg-quick@wg-install-lab
sudo systemctl is-active --quiet anyaicam-lab-simulator-rtsp
sudo systemctl is-active --quiet anyaicam-lab-simulator-video
sudo systemctl is-active --quiet anyaicam-lab-simulator-onvif
echo LAB_SIMULATOR_STARTED
