# Ubuntu validation checklist -- installer Bash harness

Neither this session's own test runs nor Codex's isolated-checkout report
qualify this installer work for release: both ran `installer/tests/run_tests.sh`
on Windows Git Bash, where 11 of 103 tests fail and 2 more are skipped purely
because the host lacks Unix tooling/semantics the harness assumes (`rsync`,
POSIX execute bits, real filesystem permissions) -- not because of any defect
in the code under test. This checklist is what still needs to run on a real,
disposable Ubuntu box before this (or any future installer change) ships.

**Target environment**: one fresh, disposable Ubuntu 24.04 instance (matches
this repo's own standing validation target elsewhere in this README) --
synthetic/local mocks only. Production EC2, Ryzen, and Samsung remain out of
scope for this checklist; do not run any of this against them.

## 1. Re-run the exact suite that failed on Windows

```sh
cd installer/tests
bash run_tests.sh
```

Expect **103 passed, 0 failed, 0 skipped** on Ubuntu (vs. 92 passed / 11
failed / 2 skipped(no-op) here). Specifically confirm these move from FAIL to
PASS:

- `migrate_legacy_persistent_data()` / `migrate_legacy_persistent_file()` --
  every sub-case (migrated file exists at new location, content preserved by
  hash, legacy directory removed after verified migration, second-run
  idempotency, legacy directory still absent after a second run). These
  depend on `rsync` and real Unix file semantics unavailable in Git Bash.
- The two `SKIP`-marked MediaMTX execute-bit cases (`P2P enabled + MediaMTX
  present + checksum matches -> pass`, `--no-mediamtx-release prior-binary
  pass case`) -- these need a filesystem that actually honors the execute
  bit, which Windows does not.

Also re-run the Python side on the same box:

```sh
python3 -m unittest discover -s installer/tests -v
```

Expect the 1 test currently skipped here (a POSIX-executable-bit assertion,
per Codex's own handoff) to actually run and pass on Ubuntu, for **26 passed,
0 skipped**.

## 2. Product-mode-specific smoke tests (new in this reconciliation)

These exercise real bash execution of `03-product-mode.sh` + `06-deploy-vms.sh`
together -- already covered by `installer/tests/test_product_mode.py`'s 9
tests, but re-run them specifically on Ubuntu to rule out any remaining
Windows/Linux bash behavioral difference (e.g. `read -r -p` interactive
prompt handling, `mktemp -d` semantics, `grep -c`/`sed` GNU-vs-BSD variants):

```sh
python3 -m unittest installer.tests.test_product_mode -v
```

## 3. Real fresh-install smoke test -- Local

On the disposable Ubuntu box, with no prior AnyAiCam installation:

```sh
sudo ./install.sh --product-mode=local
```

Verify:
- Install completes successfully end to end (docker/package/user/directory
  provisioning all succeed, matching this README's existing fresh-install
  validation target).
- `grep ANYAICAM_PRODUCT_MODE /etc/anyaicam/vms.env` shows exactly
  `ANYAICAM_PRODUCT_MODE=local`.
- `grep _ENABLED /etc/anyaicam/vms.env` shows **no** line this installer
  itself added (any present line must trace to `ensure_vms_env()`'s own
  pre-existing, mode-independent defaults, e.g. `ANYAICAM_LIVE_RELAY_ENABLED`
  -- confirm by diffing against a `--product-mode=hybrid` install's own
  output, per step 4; the two should show identical `_ENABLED=` lines).
- Inside the running `anyaicam-vms` container:
  ```sh
  docker exec anyaicam-vms python3 -c "import product_mode; print(product_mode.current_mode())"
  ```
  prints `local`.
- Every governed flag resolves `False`:
  ```sh
  docker exec anyaicam-vms python3 -c "
  import analytics_sync, event_media_uploader, facial_embedding_sync, live_relay_uploader, recording_uploader, webrtc_publisher
  print(analytics_sync.ANALYTICS_SYNC_ENABLED, event_media_uploader.EVENT_MEDIA_UPLOAD_ENABLED, facial_embedding_sync.FACIAL_EMBEDDING_SYNC_ENABLED, live_relay_uploader.LIVE_RELAY_ENABLED, recording_uploader.RECORDING_UPLOAD_ENABLED, webrtc_publisher.LIVE_P2P_ENABLED)
  "
  ```
  prints six `False` values.

## 4. Real fresh-install smoke test -- Hybrid

Repeat step 3 on a second disposable instance (or after a full uninstall/
reinstall cycle on the same one) with `--product-mode=hybrid` instead.
Verify the same checks, expecting `hybrid` and six `True` values instead.

## 5. Repair/reinstall idempotency, on real Ubuntu

```sh
sudo ./install.sh --repair
```

against each of the two installs from steps 3-4. Verify:
- The saved `ANYAICAM_PRODUCT_MODE` is reused unchanged (repair must never
  silently switch it).
- Re-running repair a second time in a row makes no further changes to
  `vms.env` (byte-identical file, or only the installer-owned
  `ANYAICAM_VMS_COMMIT`/`ANYAICAM_BUILD_ID` lines differ if the release
  build changed).

## 6. Legacy-install migration, on real Ubuntu

Simulate a pre-product-mode install (checkout the commit immediately before
this reconciliation, run a fresh install, confirm no `ANYAICAM_PRODUCT_MODE`
key exists anywhere), then upgrade in place to this branch's installer and
run `--repair`. Verify:
- `ANYAICAM_PRODUCT_MODE` is **not** added to `vms.env` by the repair (stays
  genuinely absent -- this is the corrected behavior; the original Codex
  commit would have written `ANYAICAM_PRODUCT_MODE=hybrid` here instead).
- Every pre-existing explicit flag value from the old install survives the
  repair untouched.
- The running container's `product_mode.current_mode()` reports `""` (empty
  string), and every governed flag resolves to whatever it resolved to
  *before* the upgrade (i.e., completely unaffected by this feature existing
  in the code now).

## 7. Explicit-override precedence, on real Ubuntu

With a release template or a hand-edited `vms.env` that sets
`ANYAICAM_ANALYTICS_SYNC_ENABLED=false` explicitly, run
`sudo ./install.sh --product-mode=hybrid`. Verify that explicit value
survives untouched (`grep` shows `false`, not `true`) even though Hybrid
mode's own default for that flag is `true` -- confirms explicit-override
precedence holds under real bash/coreutils, not just the temp-directory unit
tests.

## What's already covered and does NOT need Ubuntu-specific re-validation

- The mode-selection state machine itself (explicit CLI/env var, saved-value
  reuse, invalid/blank/duplicate rejection, unattended-missing-mode failure)
  -- these are pure bash string/file logic with no OS-specific dependency,
  already exercised by `test_product_mode.py`'s 9 tests passing identically
  on this Windows host.
- The Python-side `product_mode.py` resolver itself (`resolve_cloud_flag()`,
  `describe_transition()`, `persist_mode()`, the entitlement-driven auto-
  restart in `appliance_cloud.py`) -- already covered by 39 passing pytest
  tests in `app/tests/test_product_mode*.py`, none of which depend on any
  Windows-vs-Linux behavior difference (pure Python, no shell/filesystem
  permission dependency).
