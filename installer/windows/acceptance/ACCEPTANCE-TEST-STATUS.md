# Windows 1.3.4.1 acceptance tooling status

## Blue handoff report

Blue's commit `028846fcf0c0ec9a4b7871ace796a10401c8a2e7` reports 350 installer-suite tests passed, 10 skipped, and one pre-existing failure: `test_windows_local_activation_wording_132`. Blue reports that the same failure occurs on base `e3d7eda`. This is a reported inherited failure, not a fully passing installer suite; the full installer suite was not independently rerun during this integration.

Blue also reports its new acceptance-tool suite passed 32 tests. The reconciled Dell run, including three launcher-cleanup regression tests added during integration, ran 35 tests: 34 passed and one SAPI test was skipped because this Dell environment has no SAPI voice. This result does not establish LocalSystem or R430 service acceptance.

## Reconciled gates

- Installer size: `build.ps1` calls `installer-size-policy.ps1` immediately after Inno Setup emits the setup and before signing. The policy is 750,000,000 bytes.
- Runtime catalog: signing verifies every staged native catalog member with SignTool. The A5 acceptance tool also checks installed native files against catalog membership or a valid embedded signature.
- Generated Python launchers: pip's top-level `runtime\python\Scripts\*.exe` wrappers are removed after dependency installation; Python modules and non-EXE files remain. The source search and package call sites found no product dependency on these wrappers. The isolated cleanup tests cover only top-level EXEs, absent Scripts folders, and installer integration order.
- Job Object: the service launcher deliberately fails open to keep recording available, but emits an explicit `resource limits NOT applied` line through WinSW stdout logging. The Simulate acceptance tests exercise the real helper and catch a no-job result. A4 Service requires the logged applied CPU setting and Job Object membership, so it cannot report PASS when limits were not applied. R430/Smart App Control service behavior remains unverified.

## Scope limitation

This is isolated acceptance-tool integration only. It does not establish LocalSystem, WinSW service, Smart App Control, signed 1.3.4 upgrade, or lifecycle acceptance on the R430. No installer was built, signed, installed, published, or deployed by this work.
