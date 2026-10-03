# Offline isolated camera-simulator bundle — unpublished

For the future disposable Ubuntu 24.04 amd64 helper VM only. This bundle contains
MediaMTX v1.21.0, FFmpeg 7:6.1.1-3ubuntu5, Python 3.12, all 251 resolved Debian
package dependencies, the stdlib ONVIF simulator and video/configuration scripts.
It is not the VMS appliance installer's Docker/Python/model dependency bundle.
No service is installed or started merely by extraction. No real camera is used.

## Integrity and offline package installation (future helper VM only)

Authenticate the outer archive checksum from the candidate report, then extract
into a new directory. Before running any binary:

    sha256sum -c SHA256SUMS

The package lock has versions, paths, source URLs and SHA-256 digests. The bundled
Ubuntu InRelease signatures and package metadata were verified against Ubuntu's
archive keyring. MediaMTX matches the upstream release checksum and asset digest.
Individual .deb files include upstream/Debian copyright and license notices.

On the approved clean helper VM only, review the apt plan, then install locally:

    sudo apt-get --simulate --no-download --no-install-recommends install ./debs/*.deb
    sudo apt-get --no-download --no-install-recommends install ./debs/*.deb

Do not run apt update for this offline installation. Do not allow downloads,
unauthenticated packages or automatic downgrades. If the chosen Ubuntu image does
not accept the plan, stop and resolve the baseline in the lab. Never run these
installation commands on Dell, existing WSL or an appliance. Empty-status offline
apt resolution and extracted-runtime tests passed; real dpkg maintainer-script
installation and guest lifecycle still require the helper VM.

## Start synthetic camera later, after private-network approval

Set helper VM's isolated NIC to 192.168.253.10/24 only after verifying the subnet.
The appliance guest is 192.168.253.20/24 on the same private switch. Keep this helper
off customer networks. Its RTSP configuration allows loopback publishing and only
private test-subnet reads. API, HLS, WebRTC and other MediaMTX interfaces are off.
Run in separate guest terminals:

    ./mediamtx mediamtx.yml
    bash publish-test-video.sh
    python3 onvif_simulator.py --serve --ip 192.168.253.10 --cidr 192.168.253.0/24

Without --serve, the ONVIF script only prints the plan. Discovery responds only to
private-subnet requests; SOAP functions are read-only, and mutation is refused.
RTSP: rtsp://192.168.253.10:8554/testcamera (H.264 baseline, 1280x720, 15 fps).
ONVIF HTTP: http://192.168.253.10:8080/onvif/device_service; discovery UDP 3702.
Actual multicast/RTSP interoperability and VMS recording/playback/analytics are
pending real isolated VM validation. Stop the guest processes to stop simulation.
No listeners or services were started during bundle preparation.
