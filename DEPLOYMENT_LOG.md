# AnyAiCam Deployment Log

Persistent record of production changes, kept in sync with the reconciliation
workflow established 2026-08-20/21: every completed phase gets an entry here
before being considered done. Format: newest entry first.

---

## 2026-08-21 — R1: Recording-upload credential issuance (foundation only)

**Phase:** R1 of the recording-pipeline roadmap (distinct from the earlier
Live Phase 1–8 numbering). Scope: IAM design + credential-issuance
endpoint only, per the approved architecture. R2 was explicitly not
started.

**Production backup:** `app/appliance_cloud.py.pre-r1-recording-credentials-20260820-204658`
(the one existing file this phase modified; all other changes were new files).

**Commit:** `569ea0ff9e99edfbb080ee2dad9a98f5b83b843a` on branch
`production-reconcile-20260820`, pushed to GitHub, synced to the Ryzen 5
worktree (`/home/alejandro/anyaicam-production-reconcile`) — all three
confirmed at this same commit SHA with matching file hashes below.

**Files changed:**
- `app/recording_credentials.py` (new) — tenant-scoped prefix/policy/session-name helpers, mirroring `appliance_protocol.py`'s live-relay equivalents.
- `app/appliance_cloud.py` (modified) — new `POST /api/appliance/recordings/{camera_id}/credentials` route, gated behind `ANYAICAM_RECORDING_UPLOAD_ENABLED` (default off, not set in production) plus unset role ARN/bucket — fails closed (404 disabled / 503 unconfigured).
- `app/tests/test_recording_credentials.py` (new) — 9 tests, all passing, proving per-camera and per-customer prefix/policy isolation.
- `docs/r1-recording-iam.md` (new) — IAM role design + illustrative (not executed) AWS CLI commands; real role creation requires IAM-admin AWS credentials this application does not have.

**Verification performed:**
- `ast.parse` clean on both edited/new Python files, locally and on production.
- `pytest app/tests/test_recording_credentials.py` — 9/9 passed (run in the local `anyaicam` venv; the production container has no pytest installed).
- Inside the running container: `import appliance_cloud` succeeds; `recording_s3_prefix()`/`recording_session_policy()` produce correct, isolated output.
- `docker compose restart vms` → `healthy`; no new errors/tracebacks in logs.
- New route confirmed registered and reachable: unauthenticated `POST /api/appliance/recordings/{camera_id}/credentials` returns `401`, identical to the existing live-relay route's own unauthenticated response — proving it's wired through the same `authenticate_appliance()` gate without weakening it.
- Live-relay route (`/api/appliance/live/{camera_id}/session`) confirmed unaffected — same `401` as before this change.
- No customer-facing route, page, or behavior touched — this phase is 100% backend/appliance-facing and entirely inert (feature flag off) even once deployed.

**File hash verification (EC2 == GitHub == Ryzen 5):**

| File | SHA-256 |
|---|---|
| `app/recording_credentials.py` | `946ae120...66f8bffa` |
| `app/appliance_cloud.py` | `c1fe7127...0ded95e` |
| `app/tests/test_recording_credentials.py` | `b5bb06a5...7dfdf591` |
| `docs/r1-recording-iam.md` | `831e1fee...2909ad48` |

**Rollback:** `cp app/appliance_cloud.py.pre-r1-recording-credentials-20260820-204658 app/appliance_cloud.py`, delete `app/recording_credentials.py`, restart `vms` — reverts production to its R0 state. On Git: `git revert 569ea0f` (or reset the branch to `5f42bd4`) followed by a push.

**Not done in R1 (explicitly deferred):** no `recordings` catalog table or migration, no notification endpoint, no appliance-side uploader, no customer-facing retrieval/Playback wiring, no retention/lifecycle logic, no analytics association, no per-appliance pilot gate (mirrors how live relay's own per-appliance gate arrived much later, as its own phase) — all of that is R2 onward.

---

## 2026-08-21 — R0: Source-of-truth reconciliation

**Phase:** R0 (recording-pipeline roadmap; distinct from the earlier Live
Phase 1–8 numbering).

**Goal:** make production EC2's actual running state the safely captured
source of truth, without overwriting any existing GitHub branch.

**Trigger:** production EC2's working tree had diverged from its own
checked-out branch by 55 files, +456,933/−348,905 uncommitted lines,
accumulated through weeks of direct hand-edits with no corresponding
commits — discovered during an audit against `docs/AI_HANDOFF.md`.

### Actions taken

1. **Production backup/snapshot** — complete tar snapshot of the EC2 working
   tree taken before any Git operation:
   - Location: `/home/ubuntu/anyaicam-snapshots/production-reconcile-20260820-202801.tar.gz`
   - SHA-256: `d1f9c1f73e063eb129e78e6a29671eb8f8c226ed53b4e789b4c2a511d0843012`
   - 399 entries, 23 MB. Excludes only `.git/` and 5 pre-existing legacy
     directory-level backups (see below) — includes `recordings/` and every
     source/config file.

2. **New branch created from current production state:**
   - Old EC2 branch/commit: `feature/phase6-logout-csrf-hardening` @ `393ffc7a80a43b135ce9b8f5a3a422bf81b1c1b9`
   - New branch: `production-reconcile-20260820`
   - New commit: `4371cd9c4a8a1a230282cb0670591e1f1cafa30c`
   - 104 files changed, +3,101,318 / −348,905 (the working-tree drift, now committed)
   - `main`, `build/v1.2-modular-foundation`, and `feature/phase6-logout-csrf-hardening`
     were **not** modified, rebased, or force-pushed — verified unchanged on
     GitHub after the push (`main`@`d08282f`, `build/v1.2-modular-foundation`@`f0ee662`,
     `feature/phase6-logout-csrf-hardening`@`393ffc7`, all identical to before).

3. **Excluded from the commit** (left on EC2 disk, deliberately not
   version-controlled — captured in the tar snapshot above, but not in Git):
   - `.env.pre-global-relay-enable-20260818-024745`, `.env.pre-live-relay-disabled-20260818-021303`,
     `.env.pre-runtime-cloud-20260818-023138` — dated `.env` backups containing
     real secrets (`ANYAICAM_ADMIN_PASSWORD`, `ANYAICAM_APP_SECRETS`, etc.);
     `.gitignore` only covers the literal `.env`, not these variants.
   - `app-backup-before-45cabb4/` (3.0 GB), `app-backup-before-dd3f9b6/` (11 MB),
     `app-live-before-dd3f9b6/` (11 MB), `live-relay-staging-20260818-014324/` (1.4 MB),
     `pre-live-relay-deploy-20260818-014324/` (1.3 MB) — pre-existing full
     directory-level backups from earlier sessions, not source code.

4. **GitHub push:** `production-reconcile-20260820` pushed to
   `github.com:alexmata25/AnyAICam.git`, confirmed present via `git ls-remote`.

5. **Ryzen 5 sync:** the existing OneDrive checkout
   (`.../Desktop/AnyAiCam-VMS`, branch `build/v1.2-modular-foundation`) was
   **not** touched — its own branch and 19 CRLF-only diffs (already confirmed
   content-identical to GitHub via `git diff --ignore-all-space`) were
   preserved exactly as found. A new, separate `git worktree` was created
   instead at `/home/alejandro/anyaicam-production-reconcile`, checked out to
   `production-reconcile-20260820` @ `4371cd9`, 0 uncommitted files, 0 CRLF
   lines (clean native-Linux checkout — the CRLF issue is specific to the
   OneDrive-backed path and doesn't recur here).

### Verification — file hash cross-check

| File | EC2 disk | GitHub branch (`git show`) | Ryzen 5 worktree | Match |
|---|---|---|---|---|
| `app/main.py` | `0b269ad3...a6140cf` | `0b269ad3...a6140cf` | `0b269ad3...a6140cf` | ✅ |
| `app/live_view_page.py` | `b851d97f...2e82678` | `b851d97f...2e82678` | `b851d97f...2e82678` | ✅ |
| `app/live_view_sessions.py` | `0d532739...214332e` | `0d532739...214332e` | `0d532739...214332e` | ✅ |
| `app/customer_platform.py` | `1e198157...aeb3e3` | `1e198157...aeb3e3` | `1e198157...aeb3e3` | ✅ |
| `app/partner_workspace.py` | `0a047bcf...277874` | `0a047bcf...277874` | `0a047bcf...277874` | ✅ |
| `app/partner_portal.py` | `ab0aaf75...857c54b2` | `ab0aaf75...857c54b2` | `ab0aaf75...857c54b2` | ✅ |

**EC2 production application code == `production-reconcile-20260820` @ `4371cd9` on GitHub == Ryzen 5 working baseline.**

### Rollback / recovery

- **Undo the branch entirely:** delete `production-reconcile-20260820` locally
  and on GitHub (`git push origin --delete production-reconcile-20260820`) —
  no other branch was touched, so this is fully reversible with zero impact
  on `main`/`build/v1.2-modular-foundation`/`feature/phase6-logout-csrf-hardening`.
- **Restore any individual file:** `git show 393ffc7:app/<file>` (pre-R0) or
  extract from the tar snapshot above (full pre-commit disk state).
- **No service restart occurred** — this was a pure Git operation; deployed
  application files were not modified.

### Safety confirmations

- Production `vms` service was **not** restarted (no application files were
  changed by R0 — only Git metadata).
- No application behavior was modified.
- R1 (recording pipeline implementation) was **not** started.
