# Claude Code — First Prompt for AnyAiCam

Use this after cloning/opening the repository and starting Claude Code from the repository root.

```text
Read CLAUDE.md and docs/CLAUDE_HANDOFF.md completely before making changes.

This is an existing AnyAiCam VMS/security platform. Do not rebuild it from scratch and do not assume historical backup/override/phase files are active runtime code.

First perform a repository assessment only:
1. Show the current branch, latest commit, and git status.
2. Identify the active application and appliance-agent entry points.
3. Inspect the existing tests and dependency files.
4. Run the documented baseline tests/compile checks that are safe in this environment.
5. Separate pre-existing failures from environment/setup problems.
6. Summarize the current architecture, working areas, known risks, and the next 3 bounded development priorities.

Do not scan networks, contact real cameras/NVRs, authenticate to devices, enable cloud providers, provision AWS resources, deploy to production, or persist credentials during this assessment.

Do not edit code until you have presented the assessment and a proposed first task for approval.
```

After the assessment, give Claude one bounded feature/fix at a time. Require it to work on a feature branch, add/update tests, report exact test results, and prepare a pull-request summary.
