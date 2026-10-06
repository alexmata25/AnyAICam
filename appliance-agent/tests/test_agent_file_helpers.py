"""Root writes into agent-owned folders (2026-10-06): the routines the
installer, the agent installer and rollback.sh use for files in
/etc/anyaicam (system/apply_release.py agent_file_main and friends)."""
import io
import os

import pytest

from test_software_update_root_applier import ar


def request(*lines, content=None):
    text = "\n".join(lines) + "\n"
    if content is not None:
        text += "content\n" + content
    return io.BytesIO(text.encode())


def symlink_or_skip(target, link):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable here")


def test_update_env_adds_defaults_only_when_absent_and_overrides_in_place(tmp_path):
    env = tmp_path / "vms.env"
    env.write_text("# comment\nKEEP=1\nANYAICAM_VMS_COMMIT=old\nANYAICAM_VMS_COMMIT=dup\nGONE=x\n")
    code = ar.agent_file_main(request("op update_env", f"path {env}", "mode 0640", "owner anyaicam",
                                      "default KEEP=2", "default NEW=3", "override ANYAICAM_VMS_COMMIT=abc", "remove GONE"))
    assert code == 0
    assert env.read_text().splitlines() == ["# comment", "KEEP=1", "ANYAICAM_VMS_COMMIT=abc", "NEW=3"]


def test_a_missing_env_file_starts_from_the_template(tmp_path):
    template = tmp_path / "vms.env.template"
    template.write_text("FROM_TEMPLATE=yes\n")
    env = tmp_path / "vms.env"
    assert ar.agent_file_main(request("op update_env", f"path {env}", f"template {template}", "default A=1")) == 0
    assert env.read_text() == "FROM_TEMPLATE=yes\nA=1\n"


def test_write_if_missing_never_replaces_an_existing_file(tmp_path):
    identity = tmp_path / "appliance_identity.json"
    assert ar.agent_file_main(request("op write", f"path {identity}", "mode 0600", "if_missing", content='{"a": 1}\n')) == 0
    assert ar.agent_file_main(request("op write", f"path {identity}", "mode 0600", "if_missing", content='{"b": 2}\n')) == 0
    assert identity.read_text() == '{"a": 1}\n'
    if os.name == "posix":
        assert oct(identity.stat().st_mode & 0o777) == "0o600"


def test_content_is_written_exactly_and_atomically(tmp_path):
    marker = tmp_path / "vms_release.json"
    assert ar.agent_file_main(request("op write", f"path {marker}", "mode 0644", "owner root", content="line1\nline2\n")) == 0
    assert marker.read_text() == "line1\nline2\n"
    assert [p.name for p in tmp_path.iterdir()] == ["vms_release.json"]  # no temp file left behind


@pytest.mark.parametrize("lines", [
    ("op update_env", "path relative/vms.env"),
    ("op update_env", "path /x", "default not-a-key=1"),
    ("op nonsense", "path /x"),
    ("op update_env", "path /x", "bogus line"),
])
def test_malformed_requests_are_refused(lines):
    assert ar.agent_file_main(request(*lines)) != 0


def test_a_symlinked_file_is_never_read_or_written_through(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("root-only-secret\n")
    env = tmp_path / "vms.env"
    symlink_or_skip(victim, env)
    assert ar.agent_file_main(request("op update_env", f"path {env}", "default A=1")) != 0
    assert victim.read_text() == "root-only-secret\n"


def test_a_symlinked_marker_is_replaced_not_followed(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("root-only-secret\n")
    marker = tmp_path / "installed_version"
    symlink_or_skip(victim, marker)
    assert ar.agent_file_main(request("op write", f"path {marker}", "owner root", content="1.2.0\n")) == 0
    assert victim.read_text() == "root-only-secret\n"
    assert not marker.is_symlink() and marker.read_text() == "1.2.0\n"


def test_if_missing_refuses_a_planted_symlink(tmp_path):
    identity = tmp_path / "appliance_identity.json"
    symlink_or_skip(tmp_path / "does-not-exist", identity)
    assert ar.agent_file_main(request("op write", f"path {identity}", "if_missing", content="{}")) != 0
    assert not (tmp_path / "does-not-exist").exists()


def test_a_copy_never_reads_through_a_symlinked_source(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("root-only-secret\n")
    source = tmp_path / "legacy.env"
    symlink_or_skip(victim, source)
    target = tmp_path / "vms.env"
    assert ar.agent_file_main(request("op copy", f"source {source}", f"path {target}")) != 0
    assert not target.exists()


def test_ownership_is_set_only_when_running_as_root():
    if os.name == "posix" and os.geteuid() == 0:
        assert ar._named_owner("root") == (0, 0)
    else:
        assert ar._named_owner("anyaicam") is None and ar._named_owner("root") is None
