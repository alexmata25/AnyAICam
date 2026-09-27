# Samsung Step 3: Phase D cloud-side enrollment path (resolved)

Addendum to `SAMSUNG_STEP3_DEPLOYMENT_HANDOFF.md`. Everything here was determined **read-only** on 2026-09-27: the staging database, the staging container environment (presence only) and the authoritative code at f5a6d87. Nothing was enrolled, written or changed on staging or Samsung.

## Decision

**Use the device-initiated claim flow** (`anyaicam-setup --claim`). It results in **exactly one** appliance record, and Samsung **keeps its current cloud ID** `66AA5864-B13E-4E8C-AE45-5C71EC3E2FCA`.

Don't use the pre-provisioned activation flow (an admin-created record with an `AIC-…` Cloud ID and token). It would give Samsung a new cloud ID and needs an admin-created record first. It's supported, but it isn't the flow built for an already-installed appliance.

**Correction to the handoff's §5.** Samsung's current identity (32-hex appliance ID `dead660d…`, with its installer UUID as cloud ID) **was issued by this same claim flow**. It was issued on the legacy cloud deployment at 32.197.193.3, whose database is gone. The handoff's claim that the ID formats come from "different code" is wrong. The conclusion stands: staging has no record of Samsung, so Samsung must be claimed again.

## Staging state (read-only, 2026-09-27)

| Appliance | Customer / owner | Site | Status | Relation to Samsung |
|---|---|---|---|---|
| `2f941627b4` (AIC-C814766E) | `d75bdbecdd4887de4d2b89a9fcea9092` "Alejandro Mata" / anyaicamtest@gmail.com | `f67fa371cd` "Primary site" | active (the current Ryzen) | none |
| `5e76625989` (AIC-C90CF0C9) | `4efaf5153f` "Alejandro Mata" / alexmata25@gmail.com | `4de6186be8` "Ryzen Home Site" | offline since 2026-09-11 (the Ryzen's earlier enrollment) | none |
| `2d41ced390` (AIC-C075CDD3) | `6d8e437804` Sandbox Test Customer | `fac3ef3c64` | activated 2026-09-09, before Samsung was installed | none |
| `e2ewgtestappliance` | E2E test fixture | — | — | none |

- **No appliance** has cloud ID `66AA5864-…`.
- **No claim row** has device `66aa5864-…`. The claim rows belong to devices `637ad320-…` (the Ryzen's claim attempts) and `0da1c986-…` (one abandoned pending claim).
- **Leave all existing records alone.** They don't affect Samsung's claim.

## Why this gives exactly one record (verified in code and schema)

1. `appliances.cloud_id` is `UNIQUE NOT NULL`.
2. `POST /api/appliance/claim/begin` refuses a device that already has a record: 409 "This device is already provisioned".
3. A device's pending claim is **resumed**, never duplicated.
4. `POST /api/appliance/claim/complete` inserts the record once, via an atomic `status='claimed'→'completed'` update. A retry of the same completed session **recovers the same credential** instead of inserting again. The recovery key `ANYAICAM_CLAIM_FLOW_SECRET_KEY` is set on staging (44 characters), and a live encryption probe succeeded.
5. Samsung's device ID comes from `/etc/anyaicam/appliance_identity.json` (the installer UUID `66aa5864-b13e-4e8c-ae45-5c71ec3e2fca`). It passes the server's UUIDv4 validation and becomes cloud ID `66AA5864-B13E-4E8C-AE45-5C71EC3E2FCA`.

## The one decision you need to make: customer and site

A customer **owner** must approve the claim code in the portal, choosing one of **their own** sites.

- **Recommended:** customer `d75bdbec…` (owner **anyaicamtest@gmail.com**), site **"Primary site"** (`f67fa371cd`). This is the active account that already owns the Ryzen and the tested cameras. Samsung has no cameras, so nothing conflicts.
- **Alternative:** customer `4efaf5153f` (owner alexmata25@gmail.com), site "Ryzen Home Site". That account only holds the stale old-Ryzen record, so this isn't recommended.
- **Not suitable:** Sandbox, RDM test harness, E2E. They're synthetic.

## Execution (after Phase C; Samsung Claude runs the device side, you approve in the portal)

1. **Pre-flight (read-only, on the Dell):** confirm there's still no staging appliance with cloud ID `66AA5864-B13E-4E8C-AE45-5C71EC3E2FCA`.
2. **On Samsung:**
   ```
   sudo test -f /var/lib/anyaicam/offline_queue.db && sudo mv /var/lib/anyaicam/offline_queue.db "$B/offline_queue.db.pre-claim" || true
   sudo -u anyaicam /opt/anyaicam-agent/venv/bin/anyaicam-setup --claim
   ```
   At the prompts, enter `https://portal-staging.anyaicam.com` and `production`. It then shows a claim code.
3. **You, within 15 minutes:** sign in to `https://portal-staging.anyaicam.com/customer/claim-appliance` as the chosen owner, enter the code, pick the site and confirm.
4. **The agent then:**
   - completes the claim;
   - writes the new identity (`agent.json`, `credential.json` and the VMS `appliance_identity.json`) through `coordinated_reenroll()`, which rolls back on failure;
   - resets stale camera bindings;
   - restarts the services.

   **Don't interrupt it.** If it's interrupted, re-run the same command **within 15 minutes**. The agent keeps its claim state, and the server returns the same credential. After 15 minutes the credential can't be recovered, and the record would need an admin cleanup before claiming again.
5. **Verification (read-only, on the Dell):**
   - **exactly one** `appliances` row with `cloud_id='66AA5864-B13E-4E8C-AE45-5C71EC3E2FCA'`, under the chosen customer and site;
   - the claim row `completed`, pointing at that appliance ID;
   - one `appliance_credentials` row with `created_by='claim'`;
   - `last_check_in` updating and `software_version` reported;
   - on Samsung, the agent journal is free of `timed out` errors, and `step3-smoke.sh` still reports 12/12.
