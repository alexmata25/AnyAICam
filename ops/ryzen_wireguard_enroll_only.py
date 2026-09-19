"""One-off, enrollment-ONLY invocation of the already-installed agent's
own wireguard.enroll_wireguard() -- for the one specific gated rollout
step where WireGuard identity/config must be created on a real,
already-activated appliance WITHOUT also queuing interface bring-up in
the same action.

Why this script exists instead of `anyaicam-setup --wireguard-enroll`:
that CLI flag (setup_wizard.wireguard_enroll_main(), commit c332aa9) is
newer than the release currently installed on the real Ryzen appliance
(f07e1d0, which already carries wireguard.py's enroll_wireguard() and
portal.py's PortalClient.wireguard_enroll() -- both used unmodified
below -- but not yet setup_wizard.py's --wireguard-enroll entry point).
Redeploying just to get that flag would be the wrong fix here anyway:
wireguard_enroll_main() itself queues wireguard_interface_up the moment
enrollment changes anything, which is exactly the coupling this specific
rollout step needs to avoid. This script calls the same underlying
enroll_wireguard() function directly and stops there.

What this script does: loads this appliance's EXISTING identity/
credential exactly the way wireguard_enroll_main() does (AgentConfig.
load() + config.config.load_credential()) -- never first_enroll()/
coordinated_reenroll()/reenrollment.py, never touches agent.json,
credential.json, or the VMS's own appliance_identity.json -- builds a
PortalClient from that existing credential, and calls
wireguard.enroll_wireguard(config, client, replace_existing=False).
That call generates a local WireGuard keypair (once; reused on any
later re-run per enroll_wireguard()'s own idempotency), submits ONLY
the public key to the cloud enroll route, and on success writes two new
files under the existing config_dir: wireguard_identity.json and
wireguard/wg0.conf (both 0600, per enroll_wireguard()'s own
save_wireguard_identity()/save_wg_conf()).

What this script deliberately does NOT do: it never calls
_queue_privileged_action() for wireguard_interface_up (or anything
else). No real network interface, route, or firewall rule is created,
modified, or even queued for creation by this script. `wg0` does not
exist on this appliance after this script runs -- only its config file
does, ready for a SEPARATE, later, explicitly-approved step to apply.
It never prints a private key, the raw credential, or any other secret
-- only the non-secret fields enroll_wireguard() itself returns
(tunnel_address, gateway_endpoint, status).

Run via the appliance's own installed venv, as the existing
unprivileged anyaicam user (matching every anyaicam-setup invocation's
own convention) -- no sudo, no root, no network/interface changes:

    sudo -u anyaicam /opt/anyaicam-agent/venv/bin/python \\
        ryzen_wireguard_enroll_only.py --yes

The --yes flag is a deliberate, required confirmation (not a real
safety mechanism on its own -- this script still writes real files the
instant it's actually run) so that this specific one-time action is
never triggered by an accidental bare invocation.
"""

import sys

from anyaicam_agent.config import AgentConfig, load_credential
from anyaicam_agent.portal import PortalClient, PortalError
from anyaicam_agent.wireguard import enroll_wireguard


def main() -> int:
    if "--yes" not in sys.argv[1:]:
        print(
            "This writes a new WireGuard identity (keypair) and wg0.conf to this "
            "appliance's existing config directory. It does NOT bring up any "
            "interface or change any network/routing state. Re-run with --yes to "
            "proceed."
        )
        return 1

    print("\nAnyAiCam WireGuard enrollment-only (no interface bring-up)\n")
    config = AgentConfig.load()
    credential = load_credential(config)
    if not credential:
        print(
            "This appliance has not been activated yet -- credential.json is "
            "missing or unreadable. Nothing was changed."
        )
        return 1

    # Deliberately load_credential()/PortalClient() only -- the exact
    # same restriction wireguard_enroll_main() documents on itself:
    # this script's entire contract is "reuse the existing appliance
    # identity, touch nothing about it."
    client = PortalClient(config.portal_url, credential["appliance_id"], credential["credential"])
    try:
        identity = enroll_wireguard(config, client, replace_existing=False)
    except PortalError as error:
        print(
            f"WireGuard enrollment failed: {error}. Nothing was changed locally "
            "-- this script is safe to re-run once the problem above is resolved."
        )
        return 1

    print(f"Tunnel address: {identity['tunnel_address']}")
    print(f"Gateway endpoint: {identity['gateway_endpoint']}")
    print(f"Status: {identity['status']}")
    print(
        "\nEnrollment complete. WireGuard identity and wg0.conf have been written "
        "locally. No interface was brought up, no privileged action was queued, "
        "and no existing appliance identity, camera configuration, or network "
        "state was modified by this script."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
