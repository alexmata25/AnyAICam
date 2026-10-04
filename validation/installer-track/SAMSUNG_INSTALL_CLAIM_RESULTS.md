# Samsung real installer and claim validation — 2026-10-04 UTC

## Verified target and exact artifact

Samsung hostname alejandro-mata-930MBE, model 930MBE, NVMe serial
193077800308; Ubuntu 24.04.4 LTS x86_64. SSH enforced the independently
provided ED25519 fingerprint. No Dell/Ryzen or other appliance operation.
Actual portal download: 36,467,580 bytes; SHA-256
9dd0fe3fef6b46752a74ea49b4426cf9030313b926b1795a2e50c68bc48a53ac.
Archive anyaicam-appliance-installer-1.1.1-vms-a1c81e0f4926.tar.gz.
All 710 inner manifest entries passed on Samsung; both embedded source commits
are a1c81e0f492633a80198503e721fc6c277e6a8b3. Package unchanged, unpublished.

## Actual OS installation — PASS

Normal desktop user launched setup.sh with the isolated HTTPS portal and Local
mode. Real sudo, apt, Docker build, filesystem and systemd steps executed;
no OS-install mocks or replacement installer functions. Installer preflight
reported eight vCPU, 15 GiB usable memory, 433 GiB free and required >=100 GiB.
Install began 02:45:33 UTC; validator finished 02:50:48 UTC.
All 26 post-install checks passed; zero failures. Real VMS /version reports the
exact source/build commit. VMS and agent services active; VMS startup self-test
passes. Business readiness remains false while no camera records.

The runtime product /version is 0.9.0, while the installer is 1.1.1. These are
separate version identities; do not relabel the runtime or installer artifact.

## Owner claim and manual recovery — PASS

Automatic browser opening returned failure. The normal manual fallback printed
a temporary code locally. Operator initially used the other Add appliance UI
(Cloud ID/Token); lab read-only check showed zero appliances and one pending
claim. Directed operator to /customer/claim-appliance, Claim code field,
Simulated Camera Lab site and explicit Confirm claim button.

First session expired during navigation. The installed normal anyaicam-setup
--claim CLI restarted locally as the anyaicam account, using the same device
identity and HTTPS lab portal. No state was manually cleared, no security gate
bypassed and no credentials/claim codes printed in chat. Operator entered the
new temporary code and explicitly confirmed. Read-only isolated database:
one completed claim, one expired claim, exactly one appliance scoped to
installer-test-customer and installer-test-site. Normal claim retry exited 0.
VMS and agent active after enrollment. Operator declined immediate default
network discovery; isolated camera configuration is prepared separately.

## Real Samsung simulator network — 8/8 PASS

Samsung WireGuard wg-install-lab 192.168.253.20/24 routes only the simulator
192.168.253.10/32 and WS-Discovery multicast 239.255.255.250/32. New private
key stays on Samsung; no default-route/Dell network change. Tunnel enabled
for reboot validation. Simulator remains on the dedicated disposable EC2 lab.

Three actual SOAP assertions passed: device information, mutation refusal and
correct RTSP URI. Two actual multicast probes passed. Stable EndpointReference
UUID across changing message IDs passed. Actual TCP 8080 and 8554 reachability
passed. Uses the unchanged archive's parser and default multicast sender.
Results file on Samsung: anyaicam-clean-validation/samsung-simulator-results.json.

## Lab-only configuration and next gates

Camera-validation terminal completed after local administrator entry. It did
restrict agent discovery_networks to ['192.168.253.10/32']; add a lab-only Docker
Compose override for installer-test.test DNS and read-only trusted CA bundle;
then verify real container HTTPS (HTTP 200) and decode 45 synthetic frames, all with distinct frame hashes. H.264 1280x720 at 15 fps confirmed. TLS verification
stays enabled; exact candidate application/package files remain unchanged.
The authenticated owner requested discovery through the normal API: one actual camera found. Normal provisioning was accepted and completed by the agent. Exactly one configured camera numbered 1 now exists, with its RTSP URI resolved through real ONVIF. No direct camera database writes or preseeded camera rows. Protected recording-file and service-log inspection awaits local administrator input in AnyAiCam-VMS-Inspection.

Still pending: recording-worker confirmation in the real VMS; actual browser live
view; recording; browser playback/seek; basic analytics; reboot persistence;
uninstall/retained-state reinstall; clean-install recovery; remaining real
negative security assertions. No complete physical acceptance pass is claimed.

## Findings

- Browser handoff fails on this real desktop invocation. Manual fallback and
  normal expired-code recovery worked, preserving possession/owner protections.
  Root cause and automatic-browser recovery are not yet established.
- Two Add appliance paths confuse the operator: Cloud ID/Token vs Claim code.
  Correct direct claim page exists, but its discoverability needs improvement.
- Installer 1.1.1 and runtime 0.9.0 identities need clear release documentation.
- Private lab DNS/CA must also be supplied to the container; host trust alone
  does not supply a container's DNS/CA configuration. This is lab preparation,
  not proof of a defect for publicly resolvable/trusted production endpoints.

No staging, production, Billing, Household, Stripe, public website, Ryzen,
Software Update or LPR source changes. AWS cleanup inventory remains in
AWS_LAB_RESOURCES.json; helper is still running for acceptance.
