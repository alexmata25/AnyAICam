# Privileged watcher — real end-to-end validation report

Follow-up to `docs/phase1-privileged-watcher-fix-report.md`, which
explicitly left live re-verification as not done. This pass did it, on
a second fresh disposable EC2 instance, and found (and fixed) one more
real defect in the process.

Instance: `i-07094d39b376bfb00`, `t3.xlarge`, Ubuntu 24.04, `us-east-1`.
**Terminated at the end of this validation** (see §Teardown). Final
code validated: commit `f1257ac0b5ec95c6e1a5ce5698d6af03e09924ea`,
branch `staging/cloud-integration-repair`, pushed to origin. No
staging, production, or Samsung touched. Synthetic data only
(`AIC-SYNTH0002`, `synthetic-appliance-0002`, etc., created directly via
the real installed code, never through activation/discovery).

## Clean install of the final build

`sudo ./install.sh` on a genuinely fresh instance: `detected state=clean`,
succeeded end to end, and for the first time actually printed
`Created symlink .../anyaicam-privileged-watcher.path → ...` -- direct
confirmation the packaging + install fix (previous commits) works on a
real target, not just in the bash fixture harness. `validate.sh` and
container health matched the previous validation pass (same one
pre-existing, unrelated `GET /ready` AWS-config-readiness gap already
documented; not re-litigated here).

Real, on-disk POSIX permissions (unlike the prior pass, which could only
run on Windows for this specific check):

```
700 root:root /opt/anyaicam-agent/privileged/watcher.py
700 root:root /opt/anyaicam-agent/privileged
644 root:root /etc/systemd/system/anyaicam-privileged-watcher.path
644 root:root /etc/systemd/system/anyaicam-privileged-watcher.service
```

`systemctl is-enabled`: `.path` = `enabled`; `.service` = `static`
(never independently enabled, exactly as designed).

## restart_vms end to end, for real

1. Exercised the real `first_enroll()` with synthetic activation data,
   then the real `_upsert_vms_env_key()` to write
   `ANYAICAM_CLOUD_URL=https://mock-portal.internal.test`.
2. Confirmed the running container did **not** yet have it
   (`docker exec anyaicam-vms printenv ANYAICAM_CLOUD_URL` → empty).
3. Queued the action through the actual agent code path,
   `anyaicam_agent.commands.execute('restart_vms', {'confirmed': True}, config)`
   -- not a hand-crafted marker file.
4. The real systemd `.path` unit reacted in **~49ms**
   (`Active: activating (start) ... 49ms ago`, `TriggeredBy: anyaicam-privileged-watcher.path`).
5. After the built-in 10s cancellation grace period, the journal shows:
   `Executing ['docker', 'compose', '--project-directory', '/opt/anyaicam', 'up', '-d'] for marker type=restart_vms command_id=...`
   followed by `Container anyaicam-vms Recreate/Recreated/Starting/Started`.
6. The marker was consumed (removed) and the service exited
   `status=0/SUCCESS`.
7. **`docker exec anyaicam-vms printenv ANYAICAM_CLOUD_URL` now returns
   `https://mock-portal.internal.test`** -- the exact value written in
   step 1, confirmed reloaded through the real trigger chain, not
   inferred.
8. Container stayed `healthy` throughout, `RestartCount=0`.

This is the live confirmation the bash fixture harness explicitly could
not provide on its own (no real systemd there): the `.path` unit really
does fire the `.service`, which really does run the fixed dispatch
command, which really does make `vms.env` changes take effect.

## Malformed actions fail safely -- and a real defect this exposed

Five malformed/unsupported markers were placed directly in
`/var/lib/anyaicam/pending_actions` (a JSON array instead of an object,
a non-string `type`, invalid JSON, a missing `command_id`, and an
unknown `type` string): every one produced a clean, single-line
`WARNING` in the journal and **zero tracebacks**, confirming
`privileged_watcher.py`'s own crash fix (previous commit, for the
non-string-`type` case specifically) holds on a real host.

**However**, because these marker types are deliberately never deleted
(so an operator can inspect what was rejected), five closely-spaced
triggers were enough to hit **systemd's own default start-rate-limit**
on both `anyaicam-privileged-watcher.path` and its `.service`, putting
each into `failed (Result: start-limit-hit)`. A failed unit is never
retriggered by its `.path` unit again until an operator runs
`systemctl reset-failed` -- meaning a real `restart_vms`/
`reboot_appliance` request queued after that point would have been
silently ignored, with nothing in the individual command's own exit
code or logs pointing at why. **Fixed live, then verified live:**
`StartLimitIntervalSec=0` added to both units (commit `f1257ac`);
re-ran the identical five-marker sequence afterward and confirmed
neither unit ever reached `failed`, and a genuine `restart_vms` request
issued immediately after still completed successfully.

One related, secondary artifact of my own test process is worth being
precise about, since it looked like a bug before it was diagnosed:
after removing the five test markers with a plain (non-root) shell's
`sudo rm -f .../*.json`, the files appeared to "come back." The actual
cause was mundane and specific to the test session, not the product:
`/var/lib/anyaicam` is `0750 anyaicam:anyaicam`, so the unprivileged
`ubuntu` shell could not even list that directory to expand the `*.json`
wildcard *before* `sudo` ran -- the glob silently failed to expand, and
`rm -f` was handed the literal string `*.json`, which doesn't exist, so
it removed nothing while reporting no error. Using
`sudo find /var/lib/anyaicam/pending_actions -name '*.json' -delete`
(root does its own traversal) cleared the directory for real on the
first try, and both units settled to a quiet idle state immediately.
Documenting this because it consumed real investigation time before
being understood, not because it reflects anything wrong with the
shipped code.

## Teardown

Same procedure and rigor as the previous round:

| Resource | ID | Action |
|---|---|---|
| EC2 instance | `i-07094d39b376bfb00` | Terminated |
| EBS volume | (attached, `DeleteOnTermination=true`) | Deleted with the instance |
| Security group | `sg-07fe4292dc1c6579d` (`anyaicam-edge-validation-temp2-sg`) | Deleted |
| EC2 keypair | `anyaicam-edge-validation-temp2` | Deleted |
| Local private key | `~/.ssh/anyaicam_edge_validation_temp2_ed25519(.pub)` | Shredded |

Exact IDs and confirmation of each deletion (and a final sweep for any
other billable resource) are in the session's chat reply for this turn.

## Transparency

No secret VALUES were printed this round (learned from the prior
pass's slip) -- `ANYAICAM_CLOUD_URL` is not a secret and was printed
deliberately as the actual thing under test; every credential/key check
used name-only or pattern-only inspection.
