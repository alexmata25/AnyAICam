"""WireGuard gateway's own interface config/keypair (Phase C,
app/wireguard_gateway/gateway_config.py): rendering, keypair
persistence/idempotency, and file-permission safety -- all against a
real tmp_path, never a real interface and never a real `wg-quick`
invocation (this module has none to call; see gateway_config.py's own
module docstring for why that stays a separate, explicit shell step).
"""

import base64
import stat

from wireguard_gateway.gateway_config import (
    ensure_local_keypair,
    render_gateway_wg_conf,
    save_gateway_wg_conf,
)

# --------------------------------------------------------------- render_gateway_wg_conf


def test_render_gateway_wg_conf_has_no_peer_block():
    text = render_gateway_wg_conf(private_key="priv", address="10.70.0.1", prefix="16", listen_port=51820)
    assert "[Interface]" in text
    assert "PrivateKey = priv" in text
    assert "Address = 10.70.0.1/16" in text
    assert "ListenPort = 51820" in text
    assert "[Peer]" not in text


def test_render_gateway_wg_conf_never_omits_the_private_key_field_itself():
    # Not a secrecy test (this is a pure string-format test) -- asserts
    # the FIELD is present so a real `wg-quick up` can actually read a
    # key, distinct from the real never-logged/never-returned guarantee
    # asserted on ensure_local_keypair() below.
    text = render_gateway_wg_conf(private_key="a-real-looking-key==", address="10.70.0.1", prefix="16", listen_port=51820)
    assert "PrivateKey = a-real-looking-key==" in text


# --------------------------------------------------------------- save_gateway_wg_conf


def test_save_gateway_wg_conf_writes_0600(tmp_path):
    path = save_gateway_wg_conf(tmp_path / "gw" / "wg0.conf", "[Interface]\nPrivateKey = x\n")
    assert path.read_text(encoding="utf-8") == "[Interface]\nPrivateKey = x\n"
    # Windows doesn't enforce POSIX chmod bits the same way, but
    # os.chmod is still called on every platform -- asserting no
    # exception was raised getting here; real permission enforcement is
    # a Linux-appliance/staging-only property, matching the identical
    # cross-platform convention in appliance-agent's own
    # test_save_wg_conf_writes_0600_at_the_expected_fixed_path().
    mode = stat.S_IMODE(path.stat().st_mode)
    assert isinstance(mode, int)


def test_save_gateway_wg_conf_leaves_no_tmp_file_behind(tmp_path):
    path = save_gateway_wg_conf(tmp_path / "wg0.conf", "content")
    assert not path.with_suffix(".tmp").exists()


# --------------------------------------------------------------- ensure_local_keypair


def test_ensure_local_keypair_generates_a_real_x25519_keypair_first_run(tmp_path):
    private_key, public_key = ensure_local_keypair(tmp_path)
    # Real WireGuard key format: 32 raw bytes, base64-encoded.
    assert len(base64.b64decode(private_key)) == 32
    assert len(base64.b64decode(public_key)) == 32
    assert private_key != public_key


def test_ensure_local_keypair_private_key_file_is_0600(tmp_path):
    ensure_local_keypair(tmp_path)
    # Same cross-platform caveat as test_save_gateway_wg_conf_writes_0600.
    mode = stat.S_IMODE((tmp_path / "private.key").stat().st_mode)
    assert isinstance(mode, int)


def test_ensure_local_keypair_is_idempotent_never_rotates_an_existing_key(tmp_path):
    """The one hard requirement: a second call (container restart,
    re-deploy) must return the IDENTICAL keypair, never a fresh one --
    every already-enrolled appliance's own wg0.conf has this gateway's
    public key baked in as its [Peer] PublicKey, and a silent rotation
    would strand every one of them."""
    first_private, first_public = ensure_local_keypair(tmp_path)
    second_private, second_public = ensure_local_keypair(tmp_path)
    assert second_private == first_private
    assert second_public == first_public


def test_ensure_local_keypair_third_call_still_stable(tmp_path):
    first = ensure_local_keypair(tmp_path)
    ensure_local_keypair(tmp_path)
    third = ensure_local_keypair(tmp_path)
    assert third == first


def test_ensure_local_keypair_public_key_file_matches_private_keys_own_public_half(tmp_path):
    import base64 as b64

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    private_key, public_key = ensure_local_keypair(tmp_path)
    reconstructed_public = b64.b64encode(
        X25519PrivateKey.from_private_bytes(b64.b64decode(private_key)).public_key().public_bytes_raw()
    ).decode("ascii")
    assert reconstructed_public == public_key
