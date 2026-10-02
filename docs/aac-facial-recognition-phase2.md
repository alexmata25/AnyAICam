# AAC Facial Recognition -- Phase 2

Continues from the reviewed Phase 1 commit (`5be90475cd34622e2a8291b48bcdc2084257f4b2`)
on `claude/aac-facial-recognition-phase1`. Nothing merged, nothing deployed,
no real relay hardware enabled.

## 1. PostgreSQL -- attempted, blocked by a sandbox network restriction

A real, official PostgreSQL 16.4 was obtained (EnterpriseDB's portable
Windows binaries, ~339MB, extracted with no system install) and `initdb`
completed successfully. Starting the server failed: **this sandbox blocks
binding any TCP listening socket, even on `127.0.0.1`, on any port** --
confirmed directly with a bare Python `socket.bind()`/`listen()` call
outside of Postgres entirely, which failed identically. This is a hard
environmental boundary, not a missing-tool problem (last checkpoint's
finding was "no binary/Docker available"; this one is more precise: the
binaries work, the sandbox's own network policy is what blocks it). Docker
Desktop is present on this machine but not at its standard path and its
daemon isn't running, which would hit the identical restriction anyway.

Compensating evidence (unchanged from the Phase 1 review, still valid):
the gated, real `PostgreSQLFacialMigrationTests` class exists and is ready
to run the moment a reachable disposable database is available; the
always-on static rewrite check and the manual statement-by-statement
compatibility trace both still pass.

**PostgreSQL remains "reviewed, not executed" against a live server.**

## 2. Production-capable face engine: `OnnxFaceEngine`

Added `facial_engine_onnx.py`, keeping the `FaceEngine` interface from
Phase 1 unchanged. `HaarEmbeddingFaceEngine` remains fully available as the
default/fallback (see engine selection below).

- **Detection + landmarks:** OpenCV's own YuNet (`cv2.FaceDetectorYN`) --
  reuses `cv2`, already a hard dependency; no new package needed for this
  step.
- **Alignment:** OpenCV's own `cv2.FaceRecognizerSF.alignCrop()`, a real
  landmark-based affine warp to a canonical 112x112 pose -- not a plain
  resize. This directly targets the pose/rotation sensitivity the Phase 1
  review measured and flagged as unresolved by normalization alone.
- **Embedding:** the SFace ONNX model, run directly through `onnxruntime`
  (an *optional* dependency, imported lazily -- core VMS startup never
  requires it). Preprocessing (RGB channel order, raw 0-255 float32, NCHW)
  was empirically verified against `cv2.FaceRecognizerSF.feature()`'s own
  reference output: **cosine similarity 0.999999999997** on a controlled
  test input -- not a guess at undocumented preprocessing, a bit-for-bit
  match with OpenCV's own trusted implementation, and now a permanent
  regression test (`test_embedding_preprocessing_matches_opencvs_own_reference_implementation`).
- **Model provenance:** both models come from the OpenCV Zoo
  (Apache-2.0), downloaded lazily on first real use (never bundled in this
  git repo, matching `ppe.py`'s own `YOLO(PPE_MODEL_NAME)` precedent).
  Every download's SHA-256 is checked against a hash pinned in
  `facial_engine_onnx.py` (computed from the exact files fetched during
  this session) before it is ever loaded -- a supply-chain integrity
  check: a corrupted **or tampered/substituted** file is rejected and
  deleted, never used. Verified with real tests: a hash mismatch is
  rejected, a corrupted cached file is detected and automatically
  re-downloaded/repaired.
- **CPU-only, no mandatory CUDA:** the default `onnxruntime` PyPI wheel
  and this engine's default provider list (`["CPUExecutionProvider"]`) are
  CPU-only. `ANYAICAM_ONNX_PROVIDERS` swaps in `onnxruntime-gpu`'s
  `CUDAExecutionProvider` (or DirectML/ROCm) with no code change --
  `capability()`'s `gpu` field reports this honestly from the session's
  actual active providers.
- **Engine/version compatibility, enforced, not just assumed:** building
  this engine surfaced a **real latent bug** in Phase 1's own matching
  code -- `match_face()`/`EnrolledEmbedding` compared embeddings by engine
  *name* only, never *version*. `embed_face_crop()`'s own mean-centering
  fix (below) would have silently been compared against old, incompatible
  embeddings under the old code. Fixed: both engine and engine_version are
  now required to match before any two embeddings are ever compared, at
  both the pure-matching layer and the DB query layer
  (`enrolled_embeddings_for_matching()`); `facial_events` gained an
  `engine_version` column so historical matches record which exact formula
  produced them. Verified with a dedicated test
  (`test_onnx_and_haar_embeddings_are_never_silently_compared`).

**Selection:** `ANYAICAM_FACE_ENGINE` = `haar` (default) | `onnx` | `auto`.
Default stays `haar` deliberately -- so this project's test suite and any
existing deployment stay hermetic/network-free unless an operator
explicitly opts in; `onnx`/`auto` builds `OnnxFaceEngine` and **falls back
to Haar with a logged warning** if it's unavailable (onnxruntime missing,
model download/verification failed), matching this codebase's "never
crash the detection pipeline" convention.

### Bonus fix found via empirical testing: mean-centering the Haar embedding

While building accuracy tests for the new engine, the SAME synthetic-image
methodology found a real accuracy bug in the EXISTING `HaarEmbeddingFaceEngine`
(carried over unnoticed from the Phase 1 review, which used a milder version
of this same test): two clearly different synthetic images scored high
similarity because cosine similarity on a raw, un-centered pixel vector is
dominated by overall brightness, not shape. `embed_face_crop()` now
mean-centers before L2-normalizing (bumped to `version="2"`, caught by the
same engine/version enforcement above).

## 3. Accuracy testing (synthetic only -- see each test's own caveat)

62 tests across `test_facial_recognition_engine_limits.py` (Haar, extended
this session) and `test_facial_engine_onnx.py` (ONNX) cover, with real
measured numbers, not assumptions:

| Requirement | Status |
|---|---|
| Same person under lighting changes | Covered (Haar, quantified: moderate change tolerated >0.85, severe underexposure measurably worse) |
| Moderate pose changes | Covered (Haar: 5° tolerated >0.7; 45° drops <0.6 -- the exact gap ONNX's real alignment targets) |
| Scale changes | Covered (Haar: mild downscale >0.8, severe downscale measurably worse) |
| Different people | Covered (structurally different patterns score <0.8 after the mean-centering fix; same-layout patterns correctly score high) |
| Threshold boundaries | Covered (`test_facial_events.py`: exact-at-threshold vs. just-below, both engines share this logic) |
| False-positive protection | Covered -- and is what caught the mean-centering bug above |
| Multiple reference images | Covered (`test_facial_people.py`: best-reference-wins, not averaged) |
| Glasses/partial occlusion | **Partially covered.** A synthetic occlusion proxy (a solid block over part of the crop) measurably degrades the match, proving this engine has no learned "this is the same face, just occluded" understanding -- but a flat block is not a real mask or real sunglasses, and this **cannot** be honestly extended to a real accuracy claim without a real photo. |
| Embedding-version mismatch | Covered (new: engine+version scoping, described above) |

**No production accuracy number is claimed from any of this.** These are
classical-CV/model-preprocessing correctness and sensitivity measurements
on synthetic images, exactly as scoped.

## 4. Edge/cloud sync -- all three gaps addressed with real, tested code

Reuses the existing `authenticate_appliance()`/appliance-credential system
throughout -- no new identity or auth mechanism.

1. **Enrolled embedding distribution (the largest gap): resolved.**
   New `GET /api/appliance/facial-directory` (cloud, `appliance_cloud.py`)
   returns the requesting appliance's own customer's full
   people/embeddings/watchlists/watchlist_members snapshot, tenant-scoped
   exactly like every other appliance route. New `facial_embedding_sync.py`
   (edge) polls it and does a **full-replace** of the local mirror inside
   one transaction -- deliberately simple, and this is what correctly
   handles a person deleted on the cloud (already hard-deleted there per
   Phase 1) no longer lingering as a stale, still-matchable embedding on
   an edge appliance: the next sync just doesn't re-insert them. A failed
   fetch never deletes the existing local copy (fail-safe). The original
   enrollment image never syncs -- only the embedding vector does.
2. **`facial_events` detail-row synchronization: resolved.**
   `analytics_sync.py` gained a `facial_recognition` special case in
   `_build_payload()` (mirrors the existing `ppe` one exactly).
   `appliance_cloud.py`'s existing `analytics_event_available()` route now
   also creates the matching cloud-side `facial_events` row when
   `event_type='facial_recognition'`, idempotently (never duplicated on a
   replayed POST). The edge's own `save_yolo_events()` AAC hook now
   **also** appends to the local `ANALYTICS_EVENTS_FILE`
   (`append_analytics_event()`, the exact call `ppe.py` already makes) *in
   addition to* its Phase 1 direct-database write -- the direct write is
   what makes local matching/debounce/history work with no cloud at all;
   the JSON copy is what gives `analytics_sync.py` something to forward
   in a split deployment.
3. **Face-crop thumbnail cloud sync: cloud side resolved, edge side not yet wired.**
   New `POST /api/appliance/facial-events/{detection_event_id}/thumbnail`
   reuses the existing `object_storage.py` abstraction (the same
   `'thumbnails'` category/backend other event media already uses --
   S3 or local, per `settings.storage_backend`), tenant-checked (the
   `facial_events` row must belong to a camera assigned to the
   authenticated appliance). **The edge side automatically reading its
   local face-crop file and POSTing it here is the one piece of these
   three gaps not implemented this session** -- the tested, working cloud
   half is ready for it.

All of the above is disabled by default
(`ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED=false`), independently toggleable
from `ANALYTICS_SYNC_ENABLED`, and covered by 20 new tests
(`test_facial_recognition_cloud_sync.py`) exercising tenant isolation,
idempotency, deletion propagation, and fail-safe behavior on an
unreachable cloud.

## 5. UI / navigation

**Nav wired**, permission-aware: `NAV_ITEMS` gained `("aac", "/aac/people",
"◎", "Facial Recognition")`. Visibility runs through TWO gates -- the
existing coarse role-allowlist (`navigation_keys_for_role()`, extended for
`CUSTOMER_PORTAL_ROLES`) AND a new fine-grained check,
`_facial_view_permitted()`, which calls the exact same
`partner_db.allowed(identity, 'facial.view')` every AAC route already
enforces -- so the nav link and the pages it points to can never disagree
about who's allowed to see them.

**Known, pre-existing gap surfaced, not introduced by this change:**
`partner_owner`/`salesperson`/`technician` (the `partner_db.py`-style role
names) aren't wired into *any* branch of `navigation_keys_for_role()` at
all -- that function's explicit role-allowlists use a **different, older
naming scheme** (`partner_admin`, `partner_sales`, `installer`) left over
from this codebase's two parallel auth/nav systems, and its final legacy
fallback branch uses yet another, `main.py`-local `ROLE_PERMISSIONS` dict
that also doesn't recognize those names. Concretely: `technician`
authenticated via `partner_identity()` currently sees almost no nav items
at all (not just AAC's) via this path -- a real, pre-existing limitation
of this legacy nav system, unrelated to and not worsened by AAC, but worth
a dedicated follow-up given `technician` does hold `facial.manage`.
`administrator`/`customer_owner`/`customer_viewer` (both naming schemes
agree on these) work correctly and are tested
(`test_facial_recognition_navigation.py`, 10 tests).

**Inert settings field: fixed.** `facial_settings.engine` was accepted and
stored by `update_settings()` but never actually read by `facial_events.py`
-- a misleading no-op setting (there is exactly one engine running per
deployment, shared by every customer, so "per customer" was never a
coherent choice for this field). `get_settings()` now always reports the
REAL, live active engine (`facial_recognition.get_engine()`'s name +
version); `update_settings()` silently ignores any `engine` override
attempt. The AAC Settings page now shows "Active face engine: ... (vN)"
as read-only, informational text.

## 6. Relay -- confirmed still fully dormant

No new file in this session references `relay_control`, `RelayProvider`,
or `relay_provider` at all (grepped directly). The existing structural
guard (`relay_control.py` contains exactly `RelayProvider` +
`MockRelayProvider`, nothing else) and the static guard proving
`main.py`'s live hook never passes `relay_provider=` both still pass, 31/31.

## 7. Performance (synthetic, single dev machine -- not the target appliance)

New `OnnxFaceEngine` component costs, measured the same way as Phase 1's
Haar numbers (warmed-up `time.perf_counter()`, random-noise images):

| Stage | Input | Measured cost |
|---|---|---|
| Detection (YuNet) | 150x150 | ~4.0 ms |
| Detection (YuNet) | 300x300 | ~6.8 ms |
| Detection (YuNet) | 500x500 | ~12.9 ms |
| Alignment (`alignCrop`, landmark-based affine warp) | any crop | ~0.1 ms -- effectively free |
| Embedding forward pass (SFace via onnxruntime) | 112x112 aligned | ~16.1 ms |
| Full pipeline, no face found (the common case for a random crop) | 300x300 | ~11.3 ms |
| Matching (`match_face`, pure Python) | 200 enrolled people | ~2.1 ms |

**Composed estimate for one detected-and-matched face** (detection on a
~300px frame + `embed()`'s own inner crop-detection at ~150px + alignment
+ embedding forward): **roughly 25-30 ms/face**, vs. Haar's ~13 ms/face at
the same scale in the Phase 1 review. The ONNX engine is meaningfully
heavier per face -- expected, given it does real alignment and a real
(if still small) neural embedding rather than a histogram operation -- but
still comfortably sub-frame-rate for the same "per person detection, not
per frame" cost profile PPE/LPR already have in this codebase.

**Not a camera-count capacity claim.** Same caveats as the Phase 1 report:
single dev workstation, synthetic images, no concurrent YOLO load, no real
multi-camera contention. A real number needs the actual target hardware
under real load.

## AAC FACIAL RECOGNITION PHASE 2 RESULT

- **starting commit:** `5be90475cd34622e2a8291b48bcdc2084257f4b2`
- **final commit:** see this session's commit hash, recorded alongside this report
- **PostgreSQL live tests:** NOT RUN -- blocked by a sandbox-level restriction on binding any listening socket (verified directly, not a missing-tool issue); static + manual compatibility review still passes
- **face engine:** `OnnxFaceEngine` (YuNet detection + landmarks, real alignment, SFace embedding via onnxruntime) added; `HaarEmbeddingFaceEngine` remains the default/fallback
- **alignment:** Real, landmark-based (`cv2.FaceRecognizerSF.alignCrop()`), not a plain resize -- ~0.1 ms, effectively free
- **embedding model:** SFace (OpenCV Zoo, Apache-2.0), run via onnxruntime; preprocessing verified bit-for-bit against OpenCV's own reference (cosine similarity 0.999999999997)
- **CPU-only:** YES -- default provider is `CPUExecutionProvider`; onnxruntime itself is an optional, lazily-imported dependency
- **NVIDIA required:** NO
- **accuracy improvements:** Mean-centered the Haar embedding (fixed a real false-positive-risk bug found via testing); engine+version compatibility is now enforced everywhere embeddings are compared, closing a latent bug the new engine's own arrival would otherwise have triggered
- **false-positive testing:** Extended and passing; the mean-centering fix is itself a direct result of this testing
- **engine/version compatibility:** Enforced at both the pure-matching layer and the DB query layer; dedicated cross-engine test passing
- **edge embedding sync:** Implemented and tested (cloud route + edge full-replace worker, tenant-isolated, fail-safe, deletion-propagating)
- **facial event sync:** Implemented and tested (analytics_sync.py forwarding + cloud-side facial_events row creation, idempotent)
- **thumbnail cloud sync:** Cloud route implemented and tested; edge-side automatic upload not yet wired
- **navigation:** Wired, permission-aware (`facial.view`-gated) for administrator/customer_owner/customer_viewer; a pre-existing, unrelated legacy-nav gap for partner_owner/salesperson/technician documented, not fixed
- **permissions:** Unchanged from Phase 1 (`facial.view`/`facial.manage`), now also gating nav visibility consistently
- **relay remains dormant:** YES -- verified structurally and via a static guard test, 31/31 passing, zero relay references in any Phase 2 file
- **tests run:** full suite (1582 collected, including 258 AAC-specific tests, up from 204)
- **passed:** see the exact count logged for this run
- **failed:** the same pre-existing, base-commit-inherited set from the Phase 1 report (verified by diffing failure lists)
- **regressions introduced:** NONE
- **CPU benchmark:** ~25-30 ms/face composed estimate for the new ONNX engine (detection+alignment+embedding), vs. ~13 ms/face for Haar -- see Section 7's own table and caveats; not a camera-count claim
- **safe to merge AAC framework:** NO -- PostgreSQL still not executed live, ONNX engine has not been validated against a real face photo (synthetic-only per this project's testing rules), edge automatic thumbnail upload not wired, and the pre-existing partner-role nav gap remains
- **safe to deploy AAC:** NO -- same reasons, plus real relay hardware still untested (by design) and no real-world camera validation has occurred yet
- **remaining blockers:** (1) no reachable disposable PostgreSQL in this sandbox; (2) no real face photo has validated the ONNX engine's actual detection/alignment/match accuracy; (3) edge-side automatic thumbnail upload not wired; (4) partner-role nav visibility gap (pre-existing, documented)
- **recommended Phase 3:** (a) run the gated PostgreSQL tests from an environment that can bind a listening socket; (b) real-camera validation of the ONNX engine (blocked this session -- see the separate real-camera-test status below); (c) wire the edge's automatic face-crop thumbnail upload using the now-ready cloud route; (d) reconcile the two parallel nav/role-permission systems for partner-side roles; (e) consider benchmarking on actual target appliance hardware (Ryzen/Samsung) rather than a dev workstation
- **next exact action:** User review of this report, the ONNX model provenance/hashes, and the four commits on this branch; explicit authorization before any merge, deploy, or Phase 3 work

## Real bedroom-camera validation: not started, blocked by a permission boundary

A follow-up request in this same session asked to validate the Phase 2
engine against a real, user-owned bedroom camera (via the Ryzen appliance,
reachable on the local network with a pre-configured SSH key). The first
step -- a read-only SSH connection to confirm reachability -- was
**blocked by this harness's own permission classifier**, independent of
this session's own judgment. Per that tool's explicit guidance, this was
not worked around through an alternate channel (e.g., hitting the
appliance's HTTP API instead): the block's evident intent is to require a
human's own explicit, real-time authorization before an agent reaches into
a production home-security appliance, which is exactly the category of
action requested. No camera was contacted, no biometric data was
captured, and no credentials were touched. This needs the user's own
action (granting the specific permission, or performing the connection
step themselves) before it can proceed -- it is not a technical blocker
this session could resolve on its own.

STOP after reporting. Nothing was merged or deployed.
