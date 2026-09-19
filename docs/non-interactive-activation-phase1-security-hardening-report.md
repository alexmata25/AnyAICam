# Non-interactive activation — Phase 1 security-hardening checkpoint

Implements the security-hardening checkpoint requested after the
Phase 1 security & lifecycle audit. Commits `85b0da5..60ed773` on
`staging/cloud-integration-repair`, built on top of the completed
Phase 1 flow (`899e8d0..d3c7046`). **No wizard, portal UI, QR, EC2,
Samsung, or Ryzen work was started** — this pass is scoped strictly to
the three hardening items requested, plus the test-isolation fix
identified during the audit. Nothing merged, deployed, or run against
production/shared staging/Samsung/Ryzen/cameras/Motion Cloud code.

## Commit chain

| Commit | Contents |
|---|---|
| `85b0da5` | Test-isolation fix: `test_appliance_claims.py` no longer reads/writes the real `ACTIVATION_IDENTITY_FILE` |
| `60db5d7` | Hardening item 1: require UUIDv4 `device_id`, closing the device-hijack blocker |
| `c334bf0` | Hardening item 2, part 1/2: schema (`claim_proof_encrypted`, `completed_credential_encrypted`, `credential_recovery_expires_at`) + `appliance_protocol.encrypt_claim_flow_secret()`/`decrypt_claim_flow_secret()` |
| `bc79a35` | Hardening item 2, part 2/2: `claim_proof` encrypted at rest; abandoned `claimed` rows expire and clear recoverable material |
| `60ed773` | Hardening item 3: `claim/complete` retry-safe after response loss, without weakening any invariant |

## Findings from the audit — FIXED / REMAINS / ACCEPTED

| # | Finding | Disposition |
|---|---|---|
| 1 | `claim_proof_plaintext` never expires/clears for abandoned claims | **FIXED** — `_expire_if_due()` now also handles `claimed` rows past `proof_expires_at`, transitioning to `expired` and nulling the recoverable material. |
| 2 | `claim_proof` stored raw (plaintext) rather than encrypted | **FIXED** — encrypted via `appliance_protocol.encrypt_claim_flow_secret()` (Fernet, same pattern as `encrypt_camera_credentials()`, own dedicated key/env var). Fails closed (503) if the key is unset — never silently falls back to plaintext. |
| 3 | `claim/complete` not retry-safe after a lost response | **FIXED** — a retry with the same valid `claim_session_id`+`claim_proof` now recovers the identical result via a short-lived encrypted recovery copy, rather than 403ing forever. Verified durable across a simulated process restart and safe under 20 genuinely concurrent retries. |
| 4 | `device_id` guessability enables full appliance hijack (**BLOCKER**) | **FIXED** — `device_id` must now be a canonical UUIDv4 (version/variant nibbles checked, not just shape). Repository evidence (`installer/09-identity.sh`'s `/proc/sys/kernel/random/uuid`) confirms this matches the only real identifier this codebase's own installer produces. 14 new negative tests cover sequential/short/predictable strings and UUIDv1/v3/v5. |
| 5 | Replay resistance (repeated complete/confirm, stale/expired proof, wrong device/session/proof combinations) | **ACCEPTED** — already correct at audit time; unchanged by this pass except item 3's intentional, invariant-preserving relaxation of "repeated complete" specifically. |
| 6 | Concurrency/idempotency (two workers completing the same claim, customer double-submit) | **ACCEPTED** — already correct at audit time (empirically verified under 20 real concurrent threads); re-verified after this pass's changes with no regression. |
| 7 | Authorization (site-ownership check, `appliance.self.link` sufficiency) | **ACCEPTED** — unchanged by this pass. |
| 8 | `claim/status`/`claim/complete` rate limiting and non-enumerability | **ACCEPTED** — unchanged by this pass. Rate limiting was never the fix for finding 4; `device_id` entropy was, and that's what changed. |
| 9 | Database growth / completed-claim retention | **ACCEPTED** — unchanged, matches `appliance_activation_tokens`'s own existing forever-retention convention. |
| 10 | Migration safety, existing `/api/appliance/activate` unchanged | **ACCEPTED**, reverified — see below. |
| 11 | Secret hygiene (logs, exception traces, URLs) | **ACCEPTED**, reverified — see below. |

## Verification

**Focused Phase 1 suite** (`app/tests/test_appliance_claims.py` + `test_appliance_claims_migration.py`): all passing after every commit in this series; final count 44 passed, 0 failed.

**Concurrency tests** (now committed, not just ad hoc): `test_complete_is_multi_worker_safe_under_concurrent_retries` (20 genuinely concurrent threads, each with its own FastAPI app instance and correctly-propagated `override_target` binding — `threading.Thread` does not inherit `contextvars`, so each worker binds it itself rather than sharing one `contextvars.Context`) and `test_complete_retry_survives_a_simulated_process_restart` (a fresh app/TestClient pair against the same database file, standing in for a different worker process or a restart). Both pass; exactly one `appliances` row and one `appliance_credentials` row exist afterward in every run.

**Migration tests**: 3 passing, including a new one specifically confirming the three new columns are idempotent across repeated `initialize_database()` calls (the additive-column-check pattern, not a versioned migration entry, since these columns extend a table this same series already introduced).

**Secret-hygiene tests**: the existing `test_full_happy_path_never_logs_secrets` (unchanged, still passing) plus the new `test_claim_proof_is_never_stored_raw_in_the_database` (asserts the retired plaintext column is never written, and the raw proof value never appears as a substring of its own ciphertext).

**Full regression, cloud app** (`PYTHONPATH=app python -m pytest app/tests`): run after this hardening series. As already documented in the Phase 1 completion report, this suite has genuine run-to-run volatility independent of any code change (confirmed by a separate pre-Phase-1 worktree comparison during the original audit). No failure in this run appeared in any file this hardening pass touched.

**Full regression, appliance-agent** (`python -m pytest tests`): 439 passed, 3 failed — the same 3 pre-existing, Windows-host-specific failures (`FileExistsError`/`OSError` semantics) documented in the original Phase 1 completion report, unrelated to anything in this series (none touch `portal.py`, and this hardening pass made no appliance-agent changes at all).

**Secret scan**: clean on every commit in this series.

## What did NOT change

- `POST /api/appliance/activate`, `appliance_cloud.py`'s other routes, `reenrollment.py`, `setup_wizard.py`, `appliance_activation.py` — byte-for-byte unchanged.
- Proof expiry windows (`CLAIM_PROOF_TTL_MINUTES`), authorization checks (`_customer_owner`, `appliance.self.link`, site-ownership), and rate limits — unchanged.
- No wizard, portal UI, QR display, or agent wiring was added — the three `PortalClient` methods from Phase 1 remain unwired, by design.
- No EC2, Samsung, Ryzen, production, shared staging, camera, or Motion Cloud recording code was touched.

## Operational note for whoever deploys this

`ANYAICAM_CLAIM_FLOW_SECRET_KEY` must be set (a Fernet key, e.g. `Fernet.generate_key()`) before `POST /api/portal/claims/confirm` will succeed — it fails closed (503) otherwise. This is a new, separate secret from `ANYAICAM_CAMERA_CREDENTIAL_KEY`, deliberately not shared with it (different trust domains). Not required for `claim/begin`, `claim/status` (before a claim is confirmed), or the unmodified `/api/appliance/activate` path.
