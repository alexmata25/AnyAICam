# Samsung installation runbook

For the next controlled, explicitly-approved phase, when physical/console
access to the Samsung appliance is available. **Nothing in this document
has been executed against Samsung.** Every command below has been
validated against the same installer package on two fresh disposable
EC2 instances this session (`docs/phase1-edge-validation-report.md`,
`docs/phase1-privileged-watcher-e2e-validation-report.md`) — Samsung
itself has not been touched.

Build/validate at commit: see `git log -1` on
`staging/cloud-integration-repair` at the time this runbook is used —
do not assume the commit hashes below stay current; rebuild the
installer package fresh before shipping it to Samsung.

## 0. Before you touch Samsung at all

1. **Back up first.** Samsung already has real customer/camera state.
   Before running anything:
   - `sudo docker exec anyaicam-vms sqlite3 /app/recordings/partner_portal.db ".backup '/app/recordings/pre-install-backup.db'"` (or copy the whole `/var/lib/anyaicam/vms/recordings` tree to external storage) -- this is the actual customer/camera database, not just config.
   - Copy `/opt/anyaicam/docker-compose.yml` and `/etc/anyaicam/vms.env` somewhere off-box.
   - Note the currently-running image: `sudo docker inspect anyaicam-vms --format '{{.Image}}'`.
   - Note Samsung's current `talk_isapi_diagnostic` setting and confirm it stays enabled throughout (do not change it at any point in this runbook).
2. **Confirm Tailscale connectivity before starting**, as a remote-access
   path independent of Samsung's LAN/local network -- the original
   suspend/hibernate incident this session's `disable_system_suspend()`
   fix addresses took down LAN, SSH, *and* Tailscale simultaneously by
   putting the whole machine to sleep, so this is not a substitute for
   physical/console access being available, but it is the right
   secondary path for everything short of that:
   - `tailscale status` (from your own machine) -- confirm Samsung shows
     up and is reachable.
   - `ssh <tailscale-ip-or-hostname>` -- confirm a working SSH session
     over Tailscale specifically, not just LAN.
   - Record the Tailscale IP/hostname you'll use for the rest of this
     runbook.
3. Confirm plain LAN SSH also works, independently of Tailscale (two
   independent paths, not one path checked twice).
4. Confirm you have physical/console access as the fallback of last
   resort if both networking paths fail during install.

## 1. Build the sanitized installer package

From a clean checkout of the approved commit, on the machine building
the package (not Samsung):

```bash
python installer/build_release_installer.py \
  --vms-commit <exact 40-char approved commit> \
  --vms-repo <path to that checkout> \
  --output-dir dist
```

Confirm the printed `shell_lf=PASS` and `shell_executable=PASS`, and
record the printed `artifact_sha256`. Verify the SHA-256 again after
transferring the archive to Samsung (`sha256sum <file>`) before
extracting it.

## 2. Detect current install state (informational, before deciding clean vs. repair)

```bash
cd <extracted-installer-dir>
sudo bash -c 'source 03-detect-install.sh; source install.sh 2>/dev/null; detect_install_state; echo "$INSTALL_STATE"'
```

- `clean` -- nothing installed. Proceed to §3 (clean).
- `existing` -- a complete prior install. Proceed to §3 (repair) --
  `install.sh` detects this automatically and preserves persistent
  state; this is the same repair path validated twice this session.
- `partial` -- an incomplete/failed prior attempt. **Do not repair
  over a partial state.** Go to §2a first.

### 2a. Recovering from an incomplete/failed install

```bash
sudo ./uninstall.sh --purge-all
```

This removes all replaceable software, the `anyaicam` system user, and
(as of this session's fix) the privileged-watcher systemd units --
returning the box to a genuinely `clean` state so the next install run
gets the strict clean-install validation path, not the looser
repair-path thresholds. **This deletes recordings and configuration
under `/var/lib/anyaicam`, `/etc/anyaicam`, and `/var/log/anyaicam`.**
Only run this if §0's backup is already done and you have confirmed via
§2 that the prior state is genuinely `partial`, not `existing`.

Re-run §2's detection command afterward and confirm it now reports
`clean` before proceeding.

## 3. Install (clean or repair -- `install.sh` detects which automatically)

```bash
sudo ./install.sh
```

Watch for, in order: preflight OK line, Docker install/verify, VMS
image build (`docker compose build`) succeeding, agent package install,
**`Created symlink .../anyaicam-privileged-watcher.path`** (confirms the
watcher packaging fix landed), `anyaicam-vms.service` enabled+started,
suspend/hibernate masked, identity file preserved-or-generated, and the
final `Install complete (mode=..., detected state=..., VMS=...)` line.

If it fails partway: do not immediately retry. Go to §7 (rollback) or
§2a, per whichever applies.

## 4. Post-install health checks

```bash
sudo ./validate.sh
```

Expect **exactly one known, pre-existing false failure**:
`FAIL: VMS local ready endpoint responds`. This is `GET /ready`'s own
`startup_self_test()` requiring full AWS-production configuration
(region/S3/Secrets-Manager/HTTPS-enforcement) that a plain edge
appliance never has by design -- confirmed on both disposable EC2
validations this session, not something Samsung-specific and not a
sign anything is actually wrong. Every other line must say `PASS`. If
anything else fails, stop and go to §7.

Supplement with:

```bash
sudo docker inspect anyaicam-vms --format 'Status={{.State.Status}} Health={{.State.Health.Status}} Restarts={{.RestartCount}}'
curl -fsS http://127.0.0.1:8000/health   # must return {"status":"ok",...}
curl -fsS http://127.0.0.1:8000/version  # build_id must match the commit you built from
```

## 5. Docker / container health

```bash
sudo docker ps                       # anyaicam-vms present, "Up", "(healthy)"
sudo docker logs anyaicam-vms --tail 50   # no tracebacks
```

## 6. Privileged watcher health (the mechanism this session validated end to end on EC2)

```bash
sudo systemctl status anyaicam-privileged-watcher.path      # active (waiting), enabled
sudo systemctl is-enabled anyaicam-privileged-watcher.service   # must print "static" (never independently enabled)
sudo stat -c '%a %U:%G' /opt/anyaicam-agent/privileged/watcher.py   # 700 root:root
```

**Do not manually queue a real `restart_vms`/`reboot_appliance` action
against Samsung as part of this runbook** -- that was validated end to
end on disposable EC2 instances specifically so it would not need
exercising for real on the one physical appliance in scope. The checks
above (unit state, enablement, file permissions) are sufficient
evidence the mechanism is correctly installed; only exercise it for
real on Samsung if something later genuinely requires a remote
restart/reboot, as its own separate, explicitly-approved action.

## 7. Cloud URL / configuration verification

After running `sudo -u anyaicam /opt/anyaicam-agent/venv/bin/anyaicam-setup`
(the interactive activation wizard -- **requires a terminal session on
Samsung itself**, see the "customer appliance" section below for why
this matters):

```bash
grep '^ANYAICAM_CLOUD_URL=' /etc/anyaicam/vms.env
sudo docker exec anyaicam-vms printenv ANYAICAM_CLOUD_URL
sudo docker exec anyaicam-vms cat /var/lib/anyaicam/credential.json   # must succeed (read-only mount)
sudo docker exec anyaicam-vms sh -c 'echo x > /var/lib/anyaicam/credential.json'  # must fail: Read-only file system
```

Confirm the `vms.env` value and the container's live value match, and
match the portal Samsung is actually meant to activate against.

## 8. Reboot persistence -- not yet live-tested this session, do this on Samsung

Neither disposable EC2 validation this session included an actual
`sudo reboot`. This is the one item in this runbook without prior live
confirmation from this session's own work -- treat it as the first
genuinely new check, not a formality:

```bash
sudo reboot
# wait, reconnect via Tailscale AND LAN separately (§0.2/§0.3)
sudo docker ps                                    # anyaicam-vms auto-started
sudo systemctl is-active anyaicam-agent.service anyaicam-vms.service anyaicam-privileged-watcher.path
sudo ./validate.sh                                # same one expected /ready failure, nothing else
```

If the appliance does not come back up cleanly, this is exactly what
§0's backup and §7 (rollback) exist for.

## 9. Rollback if anything above fails

Same procedure validated twice on disposable EC2 this session:

```bash
cd /opt/anyaicam
sudo docker compose down
sudo cp <backed-up-vms.env> /etc/anyaicam/vms.env
sudo cp <backed-up-docker-compose.yml> /opt/anyaicam/docker-compose.yml
sudo docker compose up -d
sleep 10
sudo docker inspect anyaicam-vms --format 'Status={{.State.Status}} Health={{.State.Health.Status}}'
curl -fsS http://127.0.0.1:8000/health
```

If the *install itself* failed (not just a config regression) and the
box needs to go back to exactly its pre-attempt software state: restore
the recordings/database backup from §0, and reinstall the previously-
running commit from a freshly-built package of that same commit (there
is no separate "previous image" retag path validated for this scenario
-- rebuilding from the known-good commit is the confirmed-working
option). Do not attempt a database rollback unless §0's backup shows
the database was actually changed by the failed attempt -- this
installer's own design does not touch the database on a normal install/
repair.

## 10. What NOT to do in this phase

- Do not run camera discovery without separate, explicit approval.
- Do not change Samsung's audio, recording, or `talk_isapi_diagnostic`
  settings at any point -- confirm `talk_isapi_diagnostic` is still
  enabled at the end, matching §0's recorded baseline.
- Do not register a live Stripe webhook, touch pricing, or contact the
  shared `anyaicam-staging` cloud host as part of this runbook.
- Do not proceed past §2a into a real install if backups (§0) are not
  confirmed complete first.
