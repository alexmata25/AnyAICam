"""RDM-2 Group 2H (Tier 1): same-process end-to-end pipeline tests --
publish -> cloud-serve -> device-pull -> verify -> install -> restart ->
resume -> health-check -> report -> cloud-persist, as ONE continuous
chain of REAL producer/consumer functions.

Deliberately different from every prior group's test file: those each
fake the value on BOTH sides of one module boundary (a hand-authored
manifest dict fed to ManifestSource; a hand-authored FakeS3Client object
fed to the cloud endpoint; a hand-authored UpdateResult fed to
update_result()). This file hand-authors NOTHING in between -- the only
values ever constructed directly by this file are the inputs to
publish_update.publish() (a real tar package + an in-memory RSA
keypair) and the two intentionally-faked seams approved for RDM-2 Group
2H Tier 1:

  1. The actual TCP socket -- urllib.request.urlopen() is patched to
     route to the REAL app/appliance_cloud.py route functions (the
     manifest endpoint) or the REAL bytes tools/publish_update.py wrote
     to a hand-rolled FakeS3Client (the presigned package fetch).
     PortalClient.request()'s own real JSON-decode code and
     ManifestSource.download_package()'s own real streaming/atomic-write
     code both run unmodified against this transport.
  2. The health-check cloud probe (GET /api/appliance/commands) -- a
     small dispatcher stands in for PortalClient.request() here (the
     resolved Group 2H open decision: real make_health_check() wired
     over a fake PortalClient.request(), the exact pattern already
     established by test_service.py's own RealHealthCheckWiringTests).
     The SAME dispatcher forwards every update-result POST to the REAL
     app/appliance_cloud.py update_result() route, so a device's
     concluded report is durably persisted in a real temporary SQLite
     database, not merely asserted against a mock call.

No real AWS, no real network socket, no real device/VM, no real OS-level
service restart, no sleeps. "Restart" is this project's own established
convention (see appliance-agent/tests/test_updater_state_machine.py's
own docstring): a FRESH ApplianceAgent instance constructed against the
same on-disk config/state files.

Per the approved RDM-2 Group 2H scope, this covers exactly two flows:
one full happy path (publish -> ... -> healthy -> reported/persisted)
and one full rollback path (same chain, but the first post-restart
health check fails) -- no further failure-mode combinatorics.
"""

import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

APPLIANCE_AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = APPLIANCE_AGENT_DIR.parent
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
APP_DIR = REPO_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# Must be set before app/appliance_cloud.py (and its own partner_db
# import) is ever imported -- matches the established convention in
# app/tests/test_appliance_update_history.py / test_appliance_updates_latest.py.
os.environ.setdefault("ANYAICAM_DATABASE_BACKEND", "sqlite")
os.environ.setdefault(
    "ANYAICAM_PARTNER_DB",
    str(Path(tempfile.gettempdir()) / "anyaicam-rdm2-2h-e2e-test.db"),
)
os.environ.setdefault("ANYAICAM_ENV", "development")
os.environ.setdefault(
    "ANYAICAM_LIVE_MANIFEST_FILE",
    str(Path(tempfile.gettempdir()) / "anyaicam-rdm2-2h-live-manifest-import-guard.json"),
)

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from fastapi import FastAPI  # noqa: E402

import appliance_cloud  # noqa: E402
import updates_storage  # noqa: E402
from partner_db import connection, rows  # noqa: E402

import publish_update  # noqa: E402

from anyaicam_agent.config import AgentConfig, save_credential  # noqa: E402
from anyaicam_agent.portal import PortalError  # noqa: E402
from anyaicam_agent.service import ApplianceAgent  # noqa: E402
from anyaicam_agent.updater import installer  # noqa: E402
from anyaicam_agent.updater.models import UpdateState  # noqa: E402

DEVICE_TARGET = "anyaicam-appliance"
DEVICE_CHANNEL = "stable"


# -- cloud route wiring (matches test_appliance_update_history.py's /
# test_appliance_updates_latest.py's own established convention: pull
# the real route function directly off a freshly-registered FastAPI app
# and call it as a plain Python function -- no TestClient/ASGI, per the
# approved Group 2H open-decision resolution) ---------------------------

def _endpoint(app: FastAPI, path: str):
    for candidate_route in app.routes:
        if getattr(candidate_route, "path", None) == path:
            return candidate_route.endpoint
    raise AssertionError(f"route not registered: {path}")


_ROUTE_APP = FastAPI()
appliance_cloud.register_appliance_cloud_routes(_ROUTE_APP, lambda *_args, **_kwargs: "")
updates_latest_route = _endpoint(_ROUTE_APP, "/api/appliance/updates/latest")
update_result_route = _endpoint(_ROUTE_APP, "/api/appliance/updates/{update_id}/result")


class FakeCloudRequest:
    """authenticate_appliance() is mocked for every test below, matching
    every prior Group 2D/2G cloud test -- header values here are never
    actually read."""

    def __init__(self):
        self.headers = {}
        self.client = None


FAKE_APPLIANCE = {"id": "appliance-1", "cloud_id": "TESTCLOUD1", "customer_id": "customer-1", "site_id": "site-1"}


def setUpModule():
    # Mirrors app/tests/test_appliance_update_history.py's own
    # setUpModule(): partner_db.initialize_database() only runs once per
    # process/db-path, and appliance_update_history has a real FOREIGN
    # KEY(appliance_id) REFERENCES appliances(id) enforced by PRAGMA
    # foreign_keys=ON -- this seeds the real partners -> customers ->
    # sites -> appliances chain rather than working around the
    # constraint.
    import partner_db
    partner_db.initialize_database()
    now = "2026-01-01T00:00:00"
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('partner-1','Test Partner','approved','real',?)", (now,))
        db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('customer-1','partner-1','Customer One','customer-one@example.invalid','active','real',?)", (now,))
        db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','customer-1','Site One',?)", (now,))
        db.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appliance-1','customer-1','site-1','TESTCLOUD1',?)", (now,))


# -- hand-rolled S3/HTTP test doubles (the only two approved fake seams) --

class _FakeNoSuchKey(Exception):
    pass


class _FakeExceptions:
    NoSuchKey = _FakeNoSuchKey


class FakeS3Client:
    """Identical shape to the FakeS3Client already established in
    tools/tests/test_publish_update.py and
    app/tests/test_appliance_updates_latest.py -- kept as its own local
    copy per the approved Group 2H scope (no test-helper consolidation
    in this group). ONE instance is shared across the publisher call and
    the cloud-side reads below, so every object the device ever sees was
    actually written by the real publisher, never seeded directly by
    this test."""

    def __init__(self):
        self.objects = {}
        self.exceptions = _FakeExceptions()

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey("not found")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey("not found")
        return {}

    def put_object(self, Bucket, Key, Body, ContentType=None, CacheControl=None):
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.encode("utf-8")

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f"https://fake-s3.example/{Params['Bucket']}/{Params['Key']}?ttl={ExpiresIn}"


class FakeHTTPResponse:
    """Stands in for urllib.request.urlopen()'s return value: a real
    context manager with a real staged read()/read(n) implementation, so
    PortalClient.request()'s and ManifestSource.download_package()'s OWN
    real decode/streaming code runs completely unmodified against these
    bytes. The only thing faked is the actual TCP socket underneath it."""

    def __init__(self, body: bytes):
        self._body = body
        self._pos = 0

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeSocketTransport:
    """Replaces urllib.request.urlopen() for the duration of a test.
    Routes EVERY call PortalClient.request() and
    ManifestSource.download_package() make to either the REAL cloud
    manifest route function, or the REAL bytes the publisher wrote to
    the shared FakeS3Client -- never to a value this test authored
    directly. Raises AssertionError for anything unexpected, so a
    schema/URL drift anywhere in the chain fails loudly instead of
    silently returning a plausible-looking fixture."""

    def __init__(self, fake_s3):
        self.fake_s3 = fake_s3

    def __call__(self, request, timeout=None):
        url = request.get_full_url()
        parsed = urllib.parse.urlparse(url)
        if parsed.path == "/api/appliance/updates/latest":
            query = urllib.parse.parse_qs(parsed.query)
            target = query["target"][0]
            channel = query["channel"][0]
            response_dict = updates_latest_route(FakeCloudRequest(), target, channel)
            return FakeHTTPResponse(json.dumps(response_dict).encode("utf-8"))
        if parsed.netloc == "fake-s3.example":
            _, bucket, *key_parts = parsed.path.split("/")
            key = "/".join(key_parts)
            if (bucket, key) not in self.fake_s3.objects:
                raise AssertionError(f"RDM-2 Group 2H test: no object published at {bucket}/{key}")
            return FakeHTTPResponse(self.fake_s3.objects[(bucket, key)])
        raise AssertionError(f"unexpected urlopen() call in RDM-2 Group 2H test: {url}")


# -- shared fixture base --------------------------------------------------

class Rdm2E2EPipelineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.device_state_dir = self.root / "device-state"
        self.device_config_dir = self.root / "device-config"
        self.device_config_dir.mkdir(parents=True, exist_ok=True)

        self.fake_s3 = FakeS3Client()
        self.bucket = "anyaicam-updates-e2e-test"

        private_pem, public_pem = self._keypair()
        self.private_key_path = self.root / "operator-key.pem"
        self.private_key_path.write_bytes(private_pem)
        self.trusted_key_path = self.device_config_dir / "trusted_signing_key.pem"
        self.trusted_key_path.write_bytes(public_pem)

        with connection() as db:
            db.execute("DELETE FROM appliance_update_history")

        self._patches = [
            patch.object(appliance_cloud, "authenticate_appliance", return_value=FAKE_APPLIANCE),
            patch.object(appliance_cloud, "UPDATES_SOURCE_ENABLED", True),
            patch.object(appliance_cloud, "REMOTE_UPDATE_ENABLED", True),
            patch.object(appliance_cloud, "audit"),
            patch.object(updates_storage, "_client", return_value=self.fake_s3),
            patch.object(updates_storage, "UPDATES_S3_BUCKET", self.bucket),
            patch("urllib.request.urlopen", new=FakeSocketTransport(self.fake_s3)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    @staticmethod
    def _keypair():
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return private_pem, public_pem

    def _make_config(self):
        config = AgentConfig(
            state_dir=str(self.device_state_dir), config_dir=str(self.device_config_dir),
            log_dir=str(self.root / "logs"), update_target=DEVICE_TARGET, update_channel=DEVICE_CHANNEL,
        )
        save_credential(config, {"appliance_id": "TESTCLOUD1", "credential": "test-credential-token"})
        return config

    def _make_tar(self, path: Path, version: str) -> Path:
        with tarfile.open(path, mode="w") as tar:
            data = version.encode()
            info = tarfile.TarInfo(name="VERSION")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        return path

    def _seed_installed_version(self, config, version: str) -> None:
        """Sets up "version X is already installed and active", using
        the real installer module directly -- a realistic starting
        point (an appliance already running some version before its
        first remote update), and the version the rollback path rolls
        back TO."""
        tar_path = self._make_tar(self.root / f"seed-{version}.tar", version)
        installer.install_candidate(tar_path, version, config.update_versions_dir, config.update_staging_dir)
        installer.activate(version, config.update_versions_dir, config.current_version_pointer_file)

    def _publish(self, version: str, update_id: str) -> dict:
        """The ONLY place a manifest/envelope is produced anywhere in
        this file -- every later step consumes what this real producer
        already emitted, never a value hand-authored by a test method."""
        package_path = self._make_tar(self.root / f"package-{version}.tar", version)
        return publish_update.publish(
            s3_client=self.fake_s3, bucket=self.bucket, target=DEVICE_TARGET, channel=DEVICE_CHANNEL,
            version=version, update_id=update_id, package_path=package_path,
            platform="linux", architecture="x86_64", private_key_path=self.private_key_path,
        )

    def _dispatch(self, health_ok: bool):
        """Stands in for PortalClient.request() during a simulated
        restart's health-check/report phase -- the approved Group 2H
        open-decision resolution: real make_health_check() wired over a
        fake PortalClient.request(), matching test_service.py's own
        established RealHealthCheckWiringTests pattern. Every
        update-result POST is forwarded to the REAL cloud update_result()
        route, so a device's concluded report is durably persisted, not
        merely asserted against a mock call."""

        def handler(method, path, payload=None):
            if method == "GET" and path == "/api/appliance/commands":
                if health_ok:
                    return {"commands": []}
                raise PortalError("simulated cloud probe failure", status_code=None)
            if method == "POST" and path.startswith("/api/appliance/updates/") and path.endswith("/result"):
                # path is "/api/appliance/updates/{update_id}/result" ->
                # ['', 'api', 'appliance', 'updates', update_id, 'result']
                update_id = path.split("/")[4]
                return update_result_route(FakeCloudRequest(), update_id, payload)
            raise AssertionError(f"unexpected agent.client.request() call in RDM-2 Group 2H test: {method} {path}")

        return handler

    def _cloud_rows(self, update_id: str):
        return rows(
            "SELECT * FROM appliance_update_history WHERE appliance_id=? AND update_id=?",
            ("appliance-1", update_id),
        )


# -- happy path -------------------------------------------------------------

class HappyPathTests(Rdm2E2EPipelineTestCase):
    def test_full_publish_to_report_chain_reaches_healthy(self):
        config = self._make_config()
        self._seed_installed_version(config, "1.0.0")
        self._publish("1.1.0", "upd-e2e-happy")

        # Device pulls via the real Group 2G periodic-pull entry point.
        agent = ApplianceAgent(config)
        pull_result = agent.state_machine.check_and_install()
        self.assertEqual(pull_result.state, UpdateState.RESTARTING)
        self.assertEqual(installer.current_version(config.current_version_pointer_file), "1.1.0")
        self.assertTrue(agent.stop_event.is_set())  # real restart_signal wiring fired

        # Simulated restart: a FRESH ApplianceAgent instance, same
        # on-disk config/state -- this project's established convention.
        restarted_agent = ApplianceAgent(config)
        restarted_agent.client.request = self._dispatch(health_ok=True)
        restarted_agent.resolve_update_state()  # real resume_if_pending() + real report_update_result()

        device_row = restarted_agent.state_machine.history.get("upd-e2e-happy")
        self.assertEqual(device_row["state"], "healthy")
        self.assertFalse(config.pending_validation_file.exists())
        self.assertFalse(restarted_agent.update_resume_failed)

        cloud_rows = self._cloud_rows("upd-e2e-happy")
        self.assertEqual(len(cloud_rows), 1)
        self.assertEqual(cloud_rows[0]["state"], "healthy")
        self.assertEqual(cloud_rows[0]["from_version"], "1.0.0")
        self.assertEqual(cloud_rows[0]["to_version"], "1.1.0")

    def test_installed_package_contents_are_the_real_published_bytes(self):
        # Proves the downloaded/installed version directory actually
        # contains what the publisher published -- not merely that the
        # pipeline reached a "healthy" label.
        config = self._make_config()
        self._seed_installed_version(config, "1.0.0")
        self._publish("1.1.0", "upd-e2e-happy-contents")

        agent = ApplianceAgent(config)
        agent.state_machine.check_and_install()

        installed_file = config.update_versions_dir / "1.1.0" / "VERSION"
        self.assertEqual(installed_file.read_bytes(), b"1.1.0")


# -- rollback path ------------------------------------------------------

class RollbackPathTests(Rdm2E2EPipelineTestCase):
    def test_full_publish_to_report_chain_reaches_rolled_back_after_a_failed_health_check(self):
        config = self._make_config()
        self._seed_installed_version(config, "1.0.0")
        self._publish("1.1.0", "upd-e2e-rollback")

        # Device pulls via the real Group 2G periodic-pull entry point.
        agent = ApplianceAgent(config)
        pull_result = agent.state_machine.check_and_install()
        self.assertEqual(pull_result.state, UpdateState.RESTARTING)
        self.assertEqual(installer.current_version(config.current_version_pointer_file), "1.1.0")

        # First simulated restart: validates 1.1.0 -- health check FAILS.
        restarted_agent = ApplianceAgent(config)
        restarted_agent.client.request = self._dispatch(health_ok=False)
        restarted_agent.resolve_update_state()

        # Rollback was entered through the SAME real resume_if_pending()
        # path a healthy validation would have used -- the pointer is
        # already restored to the prior version, and restart was
        # signaled again on THIS instance for the rollback's own
        # re-activation.
        self.assertEqual(installer.current_version(config.current_version_pointer_file), "1.0.0")
        self.assertTrue(restarted_agent.stop_event.is_set())
        interim_row = restarted_agent.state_machine.history.get("upd-e2e-rollback")
        self.assertEqual(interim_row["state"], "restarting")  # not yet concluded -- rollback's own restart pending

        interim_cloud_rows = self._cloud_rows("upd-e2e-rollback")
        self.assertEqual({row["state"] for row in interim_cloud_rows}, {"restarting"})

        # Second simulated restart: validates the rollback's own
        # re-activation of 1.0.0 -- health check now SUCCEEDS.
        recovered_agent = ApplianceAgent(config)
        recovered_agent.client.request = self._dispatch(health_ok=True)
        recovered_agent.resolve_update_state()

        final_device_row = recovered_agent.state_machine.history.get("upd-e2e-rollback")
        self.assertEqual(final_device_row["state"], "rolled_back")
        self.assertFalse(config.pending_validation_file.exists())
        self.assertFalse(recovered_agent.update_resume_failed)

        final_cloud_rows = self._cloud_rows("upd-e2e-rollback")
        states = {row["state"] for row in final_cloud_rows}
        self.assertEqual(states, {"restarting", "rolled_back"})
        rolled_back_row = next(row for row in final_cloud_rows if row["state"] == "rolled_back")
        # from_version/to_version in the FINAL row still describe the
        # update's ORIGINAL (install) direction -- record_transition()
        # never rewrites the row's own from_version/to_version fields,
        # only its state -- see state_machine.py's own
        # _reconcile_orphaned_post_activation_row() docstring for the
        # same invariant stated explicitly. The device's own pointer
        # file is what actually shows 1.0.0 running again (asserted
        # above), which is the durable fact this row's state describes.
        self.assertEqual(rolled_back_row["from_version"], "1.0.0")
        self.assertEqual(rolled_back_row["to_version"], "1.1.0")
        # RDM-2 Group 2H finding (not a bug fixed by this group -- see
        # the accompanying report): UpdateStateMachine never populates
        # UpdateResult.rollback_from anywhere in state_machine.py, for
        # ANY conclusion, including a genuine ROLLED_BACK one. Every
        # existing test that exercises a non-null rollback_from value
        # hand-authors it directly into an isolated fixture/payload; this
        # is the first test where the value flows from a REAL device
        # conclusion into the REAL cloud report, and it is None -- this
        # assertion documents that ACTUAL behavior, not an assumption.
        self.assertIsNone(rolled_back_row["rollback_from"])


if __name__ == "__main__":
    unittest.main()
