# Camera runtime correction: Linux launch software gate

Base evidence: d44781d31a1f1e1131d0c3027f8df82054e54915.
Branch: codex/linux-launch-camera-acceptance.
Samsung was not touched during this software regression phase. The user confirms its previously accepted installer 1.2.3 has PHYSICAL ACCEPTANCE PASSED; this software-only run does not reopen or repeat that gate.
Owner account, location, completed claim, appliance identity and installer are preserved.

## Proven root causes and scope

1. VMS: app/main.py::_provisioned_camera_stream rejected every configured camera lacking a camera_credentials row. Anonymous commissioned cameras now return their existing RTSP source with empty credentials. Unreadable encrypted credentials still fail closed. Licensing checks in camera_url are unchanged; the RTSP server continues to enforce authentication.
2. Agent: discovery.scan reduced RTSP availability to a Boolean and provisioning.verify_device always verified port 554 and a legacy stream path. Discovery now records the open RTSP port; verification uses a locally discovered URI or real ONVIF GetStreamUri endpoint, including its port/path/query. A different host, embedded credentials, invalid port or fragment is refused. Anonymous commissioning now requires actual RTSP DESCRIBE acceptance, so protected cameras without credentials are rejected. Diagnostic query tokens are withheld.
3. Agent: CameraBindingStore accepted only ARP MACs. An L3 tunnel cannot supply a camera MAC. A non-nil canonical ONVIF UUID can now bind only when this appliance's local discovery matches its current authorized cloud camera configuration. IP/name/IP-hash identities cannot replace a MAC. Active UUID/MAC/slot collisions, ambiguous duplicate discovery and established real MAC checks remain enforced. Legacy MAC bindings remain supported. No fabricated MAC and no direct database binding are used.

These are VMS/agent defects, not installer-script defects. Claim, owner authorization, possession, CSRF/origin, nonce/expiration and enrollment encryption paths are unchanged.

## Regression inventory

Exact suite paths and pinned test runner dependencies are recorded in runtime_regression_suites.json. Tests execute in /home/ubuntu/anyaicam-lab/camera-runtime-tests on the dedicated EC2 helper with fresh fixture databases and isolated test dependencies, not its running /app portal or lab database.

- Focused camera regression: 120 passed.
- Windows VMS targeted regression: 113 passed.
- Initial broad Windows exploration: 648 passed / 9 failed. One new test incorrectly compared a text string against encrypted bytes and was corrected. Three Linux-specific packaging/filesystem assumptions and five obsolete source-text assertions are outside the camera runtime changes. Targeted behavioral replacements and the actual Linux runs determine this gate.
- Initial Ubuntu agent test payload omitted two unchanged shell fixtures: 542 passed / 5 failed fixture tests; complete fixtures supplied, 547 passed. Complete subsequent agent run: 622 passed / 0 failed / 0 skipped / 0 errors before adding the four real media E2E cases.
- Initial Linux VMS collection: missing httpx test dependency, 15 collection errors; pinned httpx 0.28.1 installed into the test-only directory.
- Real RTSP media E2E: isolated loopback MediaMTX v1.21.0 and FFmpeg. Fixture disabled an unused MoQ listener to avoid the existing simulator's port; one-second keyframe interval avoids waiting 25 seconds for the default keyframe. Final four tests passed: anonymous DESCRIBE/video, authenticated DESCRIBE/video, absent/wrong credential rejection and changing decoded frames. Test process cleanup runs on every outcome; no existing simulator configuration is changed.
- Chromium transport fixture originally omitted RTCTrackEvent.track, whereas the actual player adds event.track to its audio/video stream. Test fixture corrected; production player unchanged. Linux Chromium run excludes Microsoft Edge/WebKit cases explicitly rather than counting them as passes.

SOFTWARE REGRESSION GATE: PASSED

Final Linux runs: agent 626 passed / 0 failed / 0 skipped / 0 errors (includes all four real RTSP/media E2E cases); 46 VMS regression files, 699 passed / 0 failed / 0 skipped / 0 errors; Chromium 18 passed / 0 failed / 0 skipped / 0 errors, 27 non-Chromium cases deselected. Total 1,343 unique tests passed across these runs. The 120 focused and 113 Windows VMS results are subsets, not extra tests.

VMS run: 614.04 seconds. Complete agent run with real media enabled: 86.87 seconds. Chromium run after fixture correction: 112.80 seconds. Remaining warnings are pre-existing invalid Python string escapes, dependency deprecations and TestClient cookie warnings; no test failures remain. Module-level counts and JUnit evidence digests are in CAMERA_RUNTIME_REGRESSION_RESULTS.json. Full original JUnit evidence is retained under dist/installer-track/runtime-regression-evidence (unpublished).

Applicable Linux E2E includes real anonymous/authenticated RTSP plus changing decoded video; real FFmpeg remux/H.264/HEVC recording integration; throwaway-DB HTTP camera, live, recording, playback and analytics/event endpoints; API activation/identity/revocation E2E; Chromium live transport/fallback and real browser-generated inline video playback. These software tests are separate from the previously completed Samsung physical gate, which the user confirms PASSED. Shared-host e2e/conftest.py was not run because it explicitly targets excluded staging/production hosts.

Samsung's physical acceptance remains PASSED. Local mode is expected to use supported local owner viewing; inability to use a cloud-remote URL does not invalidate that gate. No additional physical acceptance run is authorized or started here.

## Completed physical gate and artifact preservation

Authoritative user status: PHYSICAL ACCEPTANCE PASSED for the previously physically accepted 1.2.3 artifact. No onboarding, account, claim, installation, physical test or Samsung runtime modification was performed during this software regression. No new claim code or customer account was created.

This commit changes four production runtime source files: app/main.py and appliance-agent/anyaicam_agent/{camera_binding,discovery,provisioning}. Tests/fixtures and evidence are separate. No installer scripts, packaging, artifact, build/version identity, licensing, Billing, Stripe, Household, storefront or website changes are included.

The previously accepted 1.2.3 artifact is not rebuilt, replaced or modified. Distributing these newly committed runtime changes requires a separately versioned installer candidate; do not silently patch or relabel the accepted artifact. No such candidate was built, signed, installed, published or deployed in this turn. The accepted physical gate remains PASSED.

For the earlier 1.1.1 input archive retained in this worktree, its unchanged SHA-256 was independently verified during this software turn: 9dd0fe3fef6b46752a74ea49b4426cf9030313b926b1795a2e50c68bc48a53ac. This is historical input evidence, not a checksum claim for the separate accepted 1.2.3 artifact.

LINUX SOFTWARE GATE: PASSED
SAMSUNG PHYSICAL GATE: PASSED
DEPLOYMENT PERFORMED: NO
