# Windows 1.3.2: 168 h offline premium analytics: parity checklist, shared-contract proposal, test matrix

Status: **prepared, not implemented. The shared contract is Red's design (see §1); Blue implements no licensing rule.** Owner design 2026-10-19:
- premium analytics get a separate 168 h offline authorization;
- Talk Down and Voice Call keep their 24 h snapshot expiry;
- the global entitlement TTL is unchanged.

The signed contract is **shared** and authored by Red. Blue adds no Windows-only licensing rule. Windows tip: `claude/blue-windows-132` (see the BLUE.md log for the exact hash).

## 1. SUPERSEDED: Blue's earlier contract proposal (kept for the record only)

**Red decided the shared contract on 2026-10-19 23:05** (`AnyAiCam-1.2.7-HANDOFF\RED-ANALYTICS-OFFLINE-168H-DESIGN-20261019.md`, branch `claude/red-127-analytics-offline-168h`). That design is the single source of truth, and Blue follows it:

- a **separate** signed document, `anyaicam.analytics_entitlements` v1, with its own `expires_at` (≤ issued_at + 168 h, capped at the earliest recorded authorization end);
- `camera_analytics` leaves the 24 h feature snapshot, which returns to its RC7 shape;
- **no document on a successful sync means DENY** (not the "missing → 24 h" fallback proposed below);
- an older document from sync clears the grant;
- a MAC'd state file (key derived from `ANYAICAM_APP_SECRETS`) with a high-water clock guard.

The table below is Blue's superseded proposal and is NOT to be implemented.

| Point | Proposal | Why |
|---|---|---|
| Analytics window | A new signed field in the same `anyaicam.feature_entitlements` snapshot: `camera_analytics_expires_at`, set by the cloud to `issued_at + min(168 h, ANALYTICS_TTL)`. `expires_at` (paid features, capacity bookkeeping) is unchanged at `snapshot_ttl()` (24 h default). | One signature, one keyset, one identity binding. Talk Down / Voice Call keep `expires_at`, so nothing else changes. |
| Edge check | `camera_analytic_grants` verifies signature + identity exactly as today, but with `check_expiry=False`, then applies its own `camera_analytics_expires_at` bound (and the issued-in-future skew). It must not reuse `expires_at`. A snapshot without the new field means 24 h, as today. | An older cloud keeps today's behaviour; denial is the safe default. |
| Replay | Persist a high-water mark (the newest accepted `issued_at`) next to the cached snapshot. `store_snapshot` refuses an older one, and `camera_analytic_grants` ignores a cached snapshot older than the mark. | Today, putting back an older, still-unexpired snapshot after a revocation restores grants. With 168 h that window is 7 days. Test `test_an_older_snapshot_put_back_after_a_revocation_is_refused` (strict xfail). |
| Clock rollback | Persist `last_seen_utc` (the maximum of the wall clock observed by the grant check, at most once per minute). If `now < last_seen_utc − 5 min`, premium analytics are denied until the cloud sends a fresh snapshot. | Without it, setting the PC clock back revives an expired grant indefinitely. A false positive only pauses premium analytics, never recording. |
| Renewal | No change: a fresh snapshot replaces the grant; operational ON/OFF rows are never written by a sync. | Already true (Red's design; `test_renewal_keeps_a_customer_off_choice_off`). |

The high-water mark and `last_seen_utc` are **unsigned local state**. Their integrity relies on file protection:
- Linux: root-owned or the VMS user;
- Windows: SYSTEM/Administrators only, the existing `%ProgramData%\AnyAiCam` ACL.

A local administrator can reset them. That is the same trust boundary as Red's D2 note: local administrators are inside it.

## 2. Windows specifics (what Blue owns once the contract lands)

- **Storage:**
  - The snapshot, plus the proposed mark and `last_seen_utc`, live in `%ProgramData%\AnyAiCam\config\feature_entitlements.json` (Blue `e1e79e5b`).
  - The installer gives the data root an owner of Administrators, `SY`/`BA` FullControl only, with no inheritance from ProgramData and links refused (`install-runtime.ps1`, `data-root-guard.ps1`).
  - Before 1.3.2 this file was `C:\opt\...`, which any user could delete or roll back.
- **Restart and reboot:** the grant is re-verified from the persisted file on every pipeline read (Red's cache: 10 s, plus file mtime). A service restart or reboot changes nothing. `test_a_verified_grant_survives_a_service_restart` passes; a real reboot needs the acceptance VM.
- **Service account changes:** both services run as LocalSystem (WinSW XML). If an administrator changes the account, the new account cannot read the SY/BA-only folder. The VMS then fails closed for everything, not only premium analytics, and the setup's repair restores LocalSystem. This is documented, not supported.
- **Copying to another PC:** the identity binding is `config\appliance_identity.json` (a random UUID) plus the cloud-assigned `cloud_id`/`customer_id`. An administrator who copies the whole `%ProgramData%\AnyAiCam` to a second PC can run both until the snapshot expires (≤ 168 h for analytics). Mitigations:
  1. the cloud sees two heartbeats for one identity (detectable; not implemented);
  2. optional hardware binding (MachineGuid or TPM-backed key, `local_license.py` fingerprint idea).

  Both are owner decisions. **Software-only binding cannot stop an administrator from cloning.**
- **Clock:** Windows Time can be changed by administrators only, but the proposed rollback check covers it anyway.

## 3. Windows / Linux parity checklist

| # | Requirement | Linux (Red) | Windows (Blue) | Evidence |
|---|---|---|---|---|
| 1 | Basic pixel motion ON by default, no licensing | `MOTION_DETECTION_ENABLED` default true; never consults licensing | Same code; the installer never writes `MOTION_DETECTION_ENABLED` | `test_windows_premium_defaults_132` (static), `test_corrupt_or_missing_state_denies_premium_but_never_the_camera` |
| 2 | Premium OFF by default; entitlement AND explicit ON | 96d57666 | Merged unchanged (8e7d8d0e) | Red's 26 tests pass on the Windows runtime (focused 534/0) |
| 3 | Assignment alone never activates | 96d57666 | Same | `test_purchase_and_assignment_unlock_but_do_not_turn_on` on Windows |
| 4 | Backend + pipeline enforcement | grant gate in `recording_uploader._camera_identity`, 409s | Same, plus Windows availability gate (LPR/PPE without an engine) | `test_windows_analytics_paths_132` |
| 5 | Per-camera licensing | `camera_analytics` per camera | Same | `test_a_grant_for_one_camera_does_not_license_another` on Windows |
| 6 | Upgrade keeps ON only where still entitled | `seed_explicit_analytics_operations` | Same code. **Finding F1:** edge DBs have no `camera_analytics_entitlements` rows (`edge_camera_sync` never writes them), so a Local-mode edge seeds nothing and its premium analytics come out of the upgrade OFF. Windows 1.3.0 kept its snapshot at `C:\opt\...`, so it is not visible at the new path either. | Reported to Red/Codex; not changed by Blue |
| 7 | Installer preserves authorized settings | add-if-missing | add-if-missing, explicit false kept | `test_an_explicit_false_survives_an_upgrade` (real PowerShell) |
| 8 | Signed entitlement file protected | root-owned dirs | `%ProgramData%\AnyAiCam\config`, SY/BA only | `test_windows_feature_entitlement_snapshot_lives_in_the_protected_config_folder` |
| 9 | Offline analytics 168 h, Talk Down/Voice Call 24 h | **pending Red's separate `anyaicam.analytics_entitlements` document** | **pending**: tests prepared; Windows preconditions (`ANYAICAM_APP_SECRETS` generated, kept, passed to the VMS) **PASS** (`test_windows_app_secret_132`) | 3 strict xfail + 4 passing boundary/TTL tests |
| 10 | Replay / clock rollback rejected | **pending** (Red's design: older document clears the grant; MAC'd high-water mark) | **pending** | 1 strict xfail (replay); clock rollback test to be written with the contract |
| 11 | Tamper / corrupt / forged / other-appliance | signature + identity | Same | Red's `test_an_unverifiable_or_older_snapshot...`; Blue's corrupt/truncated/deleted/unsigned on Windows |
| 12 | Revocation / cancellation / downgrade | next sync | Same | Red's revocation test on Windows (cancelled, suspended) |
| 13 | Hybrid on Windows reads the agent's files | n/a (container mounts) | `ANYAICAM_STATE_DIR` fix | `test_windows_hybrid_file_contract_132`, launcher tests |

## 4. Test matrix for owner items 5 and 8

| Scenario | How | Status |
|---|---|---|
| Service restart | grant re-read from file | **PASS** (unit) |
| Machine reboot | acceptance VM | needs VM (deferred) |
| Internet loss / reconnection | offline = no sync; reconnection = `_sync` with a fresh snapshot | unit: offline expiry (Red), renewal (Blue) **PASS**; real network loss needs VM |
| Service-account change | ACL makes it fail closed | documented; VM check |
| Plan changes / cancellation / downgrade / revocation | Red's revocation tests (cancelled, suspended) | **PASS** on Windows |
| Renewal keeps OFF | Blue | **PASS** |
| Invalid / expired signature, other appliance | Red + Blue corrupt-state tests | **PASS** |
| 24 h, 167 h 59 m | strict xfail | pending contract |
| 168 h, 168 h 1 m, 30 days denied | Blue | **PASS** (holds both before and after the contract) |
| Talk Down / Voice Call at 23 h 59 m (valid) and 24 h 1 m (expired) | Blue | **PASS** |
| Replay of an older snapshot | strict xfail | pending contract |
| Clock rollback | to be written against the contract's rollback rule | pending |

## 5. Cloud compatibility and rollout order (coordination; no deployment by Blue)

- Red's 96d57666: an appliance of this release denies premium analytics if the cloud does not send `camera_analytics`. **The cloud must deploy first.**
- With the 168 h field: the cloud must send `camera_analytics_expires_at` before appliances rely on it. An appliance that gets no field falls back to `expires_at` (24 h), so the order is safe either way.
- Windows-specific: the Windows agent's `platform` heartbeat field (Blue 931b164c) is ignored by the live cloud. Deploying it is part of the same cloud release; the owner and Codex decide that, Green/Red deploy.
