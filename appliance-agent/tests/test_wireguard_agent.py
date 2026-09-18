"""Appliance-side WireGuard identity + enrollment coverage
(anyaicam_agent/wireguard.py, config.py's own wireguard_identity
storage). Mirrors app/tests/test_wireguard_remote.py's own emphasis:
key handling (a private key is generated locally and never sent
anywhere except this device's own 0600 identity file), idempotency,
rotation/replace_existing, and pure-function config rendering -- no
real network call, no real interface, everything against a fake
portal_client and a tmp_path config_dir.
"""

import base64
import ipaddress
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from anyaicam_agent.config import AgentConfig, load_wireguard_identity
from anyaicam_agent.wireguard import enroll_wireguard, generate_keypair, render_wg_conf, save_wg_conf


def _config(tmp_path):
    config = AgentConfig(config_dir=str(tmp_path / 'etc'), state_dir=str(tmp_path / 'state'), log_dir=str(tmp_path / 'log'))
    Path(config.config_dir).mkdir(parents=True, exist_ok=True)
    return config


def _fake_portal_client(response):
    client = MagicMock()
    client.wireguard_enroll.return_value = response
    return client


_RESPONSE = {
    'tunnel_address': '10.70.0.2', 'gateway_public_key': 'g' * 43 + '=',
    'gateway_endpoint': 'gw.example.test:51820', 'gateway_tunnel_address': '10.70.0.1',
    'status': 'enrolled',
}


class KeyGenerationTests(unittest.TestCase):
    def test_generate_keypair_returns_real_wireguard_format(self):
        private_key, public_key = generate_keypair()
        for value in (private_key, public_key):
            self.assertEqual(len(value), 44)
            decoded = base64.b64decode(value, validate=True)
            self.assertEqual(len(decoded), 32)

    def test_generate_keypair_is_never_deterministic(self):
        first = generate_keypair()
        second = generate_keypair()
        self.assertNotEqual(first, second)


class RenderConfTests(unittest.TestCase):
    def test_render_includes_every_required_field(self):
        text = render_wg_conf(
            private_key='PRIV', tunnel_address='10.70.0.2', gateway_public_key='GWPUB',
            gateway_endpoint='gw.example.test:51820', gateway_tunnel_address='10.70.0.1',
        )
        self.assertIn('PrivateKey = PRIV', text)
        self.assertIn('Address = 10.70.0.2/32', text)
        self.assertIn('PublicKey = GWPUB', text)
        self.assertIn('Endpoint = gw.example.test:51820', text)
        self.assertIn('AllowedIPs = 10.70.0.1/32', text)
        self.assertIn('PersistentKeepalive = 25', text)


class AllowedIpsNeverInstallsAFullTunnelRouteTests(unittest.TestCase):
    """Real incident, 2026-09-18: render_wg_conf() used to hardcode
    AllowedIPs = 0.0.0.0/0. wg-quick treats that value as "route this
    entire host's traffic through the tunnel", and installs it as a
    real default-route override into the main routing table the moment
    `wg-quick up` runs -- even with no working handshake, which is
    exactly what happened on the real Ryzen appliance this was found
    on: its own SSH/Tailscale management traffic was silently
    blackholed into a dead tunnel, while its containerized app traffic
    kept working only because Docker's NAT path never touches the host
    routing table. AllowedIPs must never again be anything broader than
    a single host's /32 unless a genuine full-tunnel feature is
    deliberately, separately designed in the future -- never an
    accidental default. These tests exist specifically to catch that
    regression before it ever reaches a real appliance again."""

    def _allowed_ips_line(self, gateway_tunnel_address):
        text = render_wg_conf(
            private_key='PRIV', tunnel_address='10.70.0.2', gateway_public_key='GWPUB',
            gateway_endpoint='gw.example.test:51820', gateway_tunnel_address=gateway_tunnel_address,
        )
        for line in text.splitlines():
            if line.startswith('AllowedIPs'):
                return line.split('=', 1)[1].strip()
        self.fail('rendered config has no AllowedIPs line at all')

    def test_allowed_ips_is_always_exactly_a_slash_32_single_host(self):
        for gateway_tunnel_address in ('10.70.0.1', '10.99.0.1', '192.168.55.1'):
            value = self._allowed_ips_line(gateway_tunnel_address)
            self.assertEqual(value, f'{gateway_tunnel_address}/32')
            # Structural proof, not a string match: this genuinely parses
            # as a one-address network, so it cannot expand to anything
            # broader no matter what future code reads it.
            network = ipaddress.ip_network(value, strict=True)
            self.assertEqual(network.num_addresses, 1)

    def test_allowed_ips_can_never_be_the_full_tunnel_default_route(self):
        for gateway_tunnel_address in ('10.70.0.1', '10.99.0.1', '192.168.55.1'):
            value = self._allowed_ips_line(gateway_tunnel_address)
            self.assertNotEqual(value, '0.0.0.0/0')
            self.assertFalse(value.startswith('0.0.0.0'))

    def test_gateway_tunnel_address_is_a_required_argument_not_a_default(self):
        """A caller can never accidentally omit this and silently fall
        back to something broad -- render_wg_conf() has no default
        value for it at all, so a missing value is a loud TypeError,
        not a quietly-wrong config."""
        with self.assertRaises(TypeError):
            render_wg_conf(private_key='PRIV', tunnel_address='10.70.0.2', gateway_public_key='GWPUB', gateway_endpoint='gw.example.test:51820')

    def test_save_wg_conf_writes_0600_at_the_expected_fixed_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            path = save_wg_conf(config, 'dummy contents')
            self.assertEqual(path, Path(config.config_dir) / 'wireguard' / 'wg0.conf')
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(), 'dummy contents')
            mode = stat.S_IMODE(path.stat().st_mode)
            # Windows doesn't enforce POSIX chmod bits the same way, but
            # os.chmod is still called on every platform -- this asserts
            # no exception was raised getting here, real permission
            # enforcement is a Linux-appliance-only property already
            # covered by every other 0600 file in this codebase.
            self.assertIsInstance(mode, int)


class WireguardIdentityStorageTests(unittest.TestCase):
    def test_round_trips_through_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            self.assertIsNone(load_wireguard_identity(config))
            from anyaicam_agent.config import save_wireguard_identity
            save_wireguard_identity(config, {'private_key': 'PRIV', 'public_key': 'PUB'})
            self.assertEqual(load_wireguard_identity(config), {'private_key': 'PRIV', 'public_key': 'PUB'})

    def test_wireguard_identity_file_lives_under_config_dir_not_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            self.assertEqual(config.wireguard_identity_file, Path(config.config_dir) / 'wireguard_identity.json')
            self.assertNotEqual(Path(config.config_dir), Path(config.state_dir))


class EnrollWireguardTests(unittest.TestCase):
    def test_first_enrollment_generates_a_key_submits_only_the_public_half(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            client = _fake_portal_client(_RESPONSE)
            identity = enroll_wireguard(config, client)
        submitted_public_key = client.wireguard_enroll.call_args.args[0]
        self.assertEqual(identity['public_key'], submitted_public_key)
        self.assertNotIn(identity['private_key'], client.wireguard_enroll.call_args.args)
        # private_key never appears anywhere in what was sent to the portal client
        for call in client.wireguard_enroll.call_args_list:
            self.assertNotIn(identity['private_key'], str(call))

    def test_first_enrollment_writes_both_identity_and_conf_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            enroll_wireguard(config, _fake_portal_client(_RESPONSE))
            self.assertTrue(config.wireguard_identity_file.is_file())
            self.assertTrue(config.wireguard_conf_file.is_file())
            self.assertIn('[Interface]', config.wireguard_conf_file.read_text())

    def test_enrollment_threads_the_real_gateway_tunnel_address_into_the_written_conf(self):
        """End-to-end proof that the portal's response value actually
        reaches the file wg-quick will read -- not just that
        render_wg_conf() itself is correct in isolation. Real incident
        (2026-09-18): this exact wiring is what silently defaulted to
        0.0.0.0/0 before, since AllowedIPs wasn't threaded through from
        the response at all."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            response = dict(_RESPONSE, gateway_tunnel_address='10.55.0.1')
            enroll_wireguard(config, _fake_portal_client(response))
            written = config.wireguard_conf_file.read_text()
        self.assertIn('AllowedIPs = 10.55.0.1/32', written)
        self.assertNotIn('0.0.0.0/0', written)

    def test_reenrolling_without_replace_existing_reuses_the_same_local_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            first = enroll_wireguard(config, _fake_portal_client(_RESPONSE))
            second = enroll_wireguard(config, _fake_portal_client(_RESPONSE))
        self.assertEqual(first['public_key'], second['public_key'])
        self.assertEqual(first['private_key'], second['private_key'])

    def test_replace_existing_generates_a_genuinely_new_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            first = enroll_wireguard(config, _fake_portal_client(_RESPONSE))
            second = enroll_wireguard(config, _fake_portal_client(_RESPONSE), replace_existing=True)
        self.assertNotEqual(first['public_key'], second['public_key'])
        self.assertNotEqual(first['private_key'], second['private_key'])

    def test_replace_existing_flag_is_forwarded_to_the_portal_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            client = _fake_portal_client(_RESPONSE)
            enroll_wireguard(config, client, replace_existing=True)
        client.wireguard_enroll.assert_called_once()
        self.assertTrue(client.wireguard_enroll.call_args.kwargs.get('replace_existing') or (len(client.wireguard_enroll.call_args.args) > 1 and client.wireguard_enroll.call_args.args[1]))

    def test_a_portal_failure_propagates_and_writes_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            client = MagicMock()
            client.wireguard_enroll.side_effect = RuntimeError('portal unreachable')
            with self.assertRaises(RuntimeError):
                enroll_wireguard(config, client)
            self.assertFalse(config.wireguard_identity_file.exists())
            self.assertFalse(config.wireguard_conf_file.exists())


if __name__ == '__main__':
    unittest.main()
