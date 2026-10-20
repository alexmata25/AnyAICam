# Windows 1.3.2 EXE build checklist (PREPARED; nothing built)

Owner rule (2026-10-19 / 10-20):
- no final release installer, signing, publishing or deployment without separate authorization;
- a **test** candidate build also needs the owner's go-ahead, an adequate C: and the shared Dell lock;
- the acceptance VM is deferred.

## Gate 0: the source is complete (all must be true before any build)

| # | Gate | Status (2026-10-20 16:00) |
|---|---|---|
| 0.1 | Red's shared 168 h analytics commit merged into `claude/blue-windows-132` | **DONE**: ae23d024 arrives through RC8 be366f8b, merge `179e6ae9` |
| 0.2 | Red's F1 edge-seed fix + shard-1 fixture fix merged | **DONE** (in ae23d024) |
| 0.3 | Blue's 4 strict-xfails adapted to the separate `anyaicam.analytics_entitlements` document and passing | **DONE** `c5a0d98f` |
| 0.4 | `analytics_entitlements.json` resolves under `%ProgramData%\AnyAiCam\config` on Windows; the AST `native_path` guard passes | **DONE** `c5a0d98f` (test) |
| 0.5 | Focused Windows suites green on the merge tip (section 3) | **DONE (focused)**: 103/1/0, 99/1/0, 36/0 on the Windows host. Full section 3 (agent suite, installer suite with Inno compile) not rerun |
| 0.6 | Red's full Linux regression of the shared commits complete with only known environment failures | **DONE for be366f8b** (8163/14/352, all 14 closed by Codex). The Windows tree's own Linux shards are not run |
| 0.6b | Red's rc8-dev test/LF commits (`ee055c05`) | **not merged** (awaiting Red's final). Trial merge clean; adds no CR failure on the Windows tree |
| 0.6c | Open product item **W-1**: Voice Call speech-to-text has no Vosk model on Windows | **owner/Codex decision**: bundle the pinned model, or report it unavailable |
| 0.7 | Codex independent review of the exact Windows commit: PASS | requested (`CODEX-REVIEW-WINDOWS-1.3.2.md`) |
| 0.8 | Owner go-ahead for an **unsigned test-candidate** build | owner |

## Gate 1: resources and lock (checked right before starting)

- **C: free ≥ 12 GB.** The setup is about 0.58 GB and `output\` (runtime catalog staging, keyset, setup) about 1.5 GB. Inno's LZMA2 temp is about 2 GB. The shared floor is 8 GB, plus margin.
- **RAM free ≥ 6.2 GB.** Same start gate as the RC8 regressions (raised from 4.3 GB on 2026-10-20).
- **Prerequisites probed read-only 2026-10-20:** all present for an unsigned build:
  - ISCC 7, git, `py -3.11` (keyset prep dry-run OK);
  - 6 pinned vendor files (hashes match);
  - VC++ with a valid Microsoft signature;
  - 108 locked wheels (0 missing, 0 unpinned);
  - makecat and signtool (SDK 10.0.26100);
  - production keyset `2389a655…`.
- For `-AzureSign` the dlib and metadata are present, but signing needs an owner-authorized Azure session. For `-Sign`, no code-signing certificate is installed.
- **The shared lock** `anyaicam-red\.docker-test.lock` must be free. Take it with an owner line `blue windows 1.3.2 unsigned build <commit> <time>` and release only Blue's own line.
- **No Red, Green or Codex heavy job running.** One heavy job at a time.

## Gate 2: inputs (all are verified by `build.ps1` itself; the build refuses on any mismatch)

| Input | Pin |
|---|---|
| Clean worktree at the exact reviewed commit (`git status` empty; `source-integrity.ps1` checks every packaged folder) | commit from 0.7 |
| `installer\windows\vendor\` (copied from `wt-127-win-build`, hash-pinned in `build.ps1` and `wheels.lock.sha256`) | python 3.12.10 embed `4acbed6d…`, get-pip `fb24e693…`, WinSW `05b82d46…`, FFmpeg 8.1.2 `db580001…`, MediaMTX 1.21.0 `8a58a9b8…`, VC++ 14.44.35211 `cc0ff0eb…` (+ Microsoft Authenticode), wheels lock `c30ecd32…` |
| Production entitlement **public** keyset | `anyaicam-red\windows-1.2.7-build\inputs\entitlement-signing-public-keys.json`, SHA-256 **`2389a655fb3e5ed91e8a4a9216f9c45b97feb3847571b0536384b8305979af7c`** |
| Synthetic **test-only** keyset (second build, DO-NOT-SHIP) | `…\synthetic-test-keyset\public-keyset.json`, SHA-256 `629ac8e4545a9c324a21d6b0f08098ebc1c814bcc29e9a6f46fff992357c8b49` |
| Tools | `%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe`, Windows Kits 10.0.26100 `makecat.exe`, `py -3.11` (`prepare-keyset.py`); all present on the Dell |

## Gate 3: build (unsigned; never `-Sign` / `-AzureSign`)

```
powershell -NoProfile -ExecutionPolicy Bypass -File <wt>\installer\windows\build.ps1 -Version 1.3.2 `
  -EntitlementKeyset "C:\Users\Alejandro Mata\anyaicam-red\windows-1.2.7-build\inputs\entitlement-signing-public-keys.json" `
  -EntitlementKeysetSha256 2389a655fb3e5ed91e8a4a9216f9c45b97feb3847571b0536384b8305979af7c
```

The build must log:
- every vendor pin verified;
- the VC++ Microsoft signature;
- "Runtime catalog: N native files", where N must match the previous builds' **314** unless the wheels changed;
- the keyset accepted.

Then the synthetic-keyset build from the same tree goes into a folder named `…-SYNTHETIC-KEYSET-DO-NOT-SHIP`.

## Gate 4: record and verify the artifacts (no publishing)

- Move the setups to `anyaicam-release-windows\candidates\1.3.2-<commit7>-UNSIGNED\` and `…-SYNTHETIC-KEYSET-DO-NOT-SHIP\`. Write a `LOCAL-CANDIDATE-NOT-PUBLISHED.txt` in each.
- Record for each:
  - size;
  - SHA-256 (`.sha256` file);
  - `Get-AuthenticodeSignature` = NotSigned;
  - `ProductVersion` = 1.3.2;
  - the source commit embedded in the name and in `ANYAICAM_BUILD_ID`.
- Offline inspection without installing on the Dell:
  - `innounp` / ISCC log listing: every file in `[Files]` present;
  - no `vendor\` file outside the pins;
  - no `tests\` or `__pycache__`;
  - the packaged keyset hash = `2389a655…` (production) / `629ac8e4…` (synthetic).
- **Never install on the Dell** (it is not a clean machine, and the old 0.1.3 VMS was on port 8000). Windows Sandbox can't install unsigned setups (Smart App Control). Real installs wait for the deferred acceptance VM.

## Section 3: automated tests on the exact build commit (Windows runtime: embedded 3.12.10 + pinned wheels)

| Suite | Last result (pre-168 h tip c493d6f9) |
|---|---|
| Red's premium default-OFF focused 18 files + Blue Windows app files | 534 / 0 (3 skipped) |
| `installer/tests/test_windows_*.py` (incl. launchers, ACL/junction, downgrade guard, premium defaults, app secret) | 152 / 0 (+4 app-secret, +5 premium defaults since) |
| appliance-agent Windows tests | 50 / 0 |
| `test_windows_offline_premium_132.py` | 10 passed / 4 strict-xfail (to be 14 / 0 after 0.3) |
| Opt-in VMS startup smoke test (`ANYAICAM_WINDOWS_RUNTIME_PYTHON`) | PASS |
| Known Windows-host-only failures (unchanged vs RC7) | agent `test_blocked_when_marker_stat_fails` (POSIX), installer `test_a_symlinked_directory_is_refused` (symlink privilege) |

## Gate 5: Codex handoff for the build

Codex receives: the exact commit; both setups' sizes and SHA-256s; the build logs; the test logs above; the inspection output; and the list of what is NOT yet proven on a real machine:
- install, upgrade from published 1.3.0 `aedecb6`, repair, uninstall;
- reboot;
- network loss;
- service-account change;
- real or simulated cameras: discovery, live view, recording, playback, analytics ON/OFF;
- Local/Hybrid with the test cloud.

All of these belong to the deferred acceptance VM.

## Later (separate authorizations)

- **Signing:** `build.ps1 -AzureSign` (owner's Azure Artifact Signing). Re-validate the signed file. The publisher pin (`publisher-pin.ps1`) must match `CN=Any AI Cam, O=Any AI Cam, L=Porter, S=Texas, C=US` plus its identity EKU.
- **Publishing:** `publish_bluehost_windows.py`. It refuses unsigned or unvalidated files. That is Green's and the owner's area.
