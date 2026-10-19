# Codex independent pricing-hotfix review

Date: 2026-10-18

## Source and commits

- Reviewed Red's source candidate `10d9ec6ba7e2089b88b4c9db7a6dd7cbaa9a9615`, based on live baseline `9bf3c205bedb48bbc0a3891497b44986762aead7`.
- Isolated branch: `codex/pricing-hotfix-hardening`.
- `60f1e92b`: conflicting duplicate historical Stripe Price IDs are discarded rather than allowing the last environment value to win.
- `81156047`: unknown or ambiguous stored historical Price IDs are no longer represented to customers at the current catalog amount. Legacy rows with no stored Price ID retain their existing fallback. The subscription page displays that the exact amount is unavailable instead of formatting a false amount.
- No push, merge, AWS request, production command, Stripe action, or website publication was made.

## Billing behavior reviewed

The pricing candidate changes four application files and contains no schema migration. Current checkouts resolve only current configured Price IDs. Previous Price IDs are only used for existing subscription recognition and carry their own historic cent amount. VMS-license previous IDs map to the same capacity, while new purchases use the current ID. The focused tests below verify those mappings and valid existing Local/Hybrid entitlements.

Additional defensive correction: repeated identical historical mappings are benign; conflicting mappings are removed and treated as unknown. This avoids implying an actual charge amount where configuration is ambiguous.

## Test evidence

Red's saved full comparison at `C:\Users\Alejandro Mata\anyaicam-red\pricing-hotfix-review-20261018\tests\billing-suite-9bf3c205-vs-10d9ec6b.txt` reports:

- Baseline: 1,060 passed, 0 failed, 12 skipped, plus one collection error because `anyaicam_agent` is absent from the container path.
- Red candidate: 1,095 passed, 0 failed, 12 skipped, with the same collection error.
- The excluded edge-agent test did not run on either commit; the collection error is not a pricing test failure.

Codex ran focused tests on the corrected isolated candidate:

- Repricing and capacity mapping: 37 passed.
- `test_customer_entitlements.py`: 28 passed.
- `test_local_hybrid_pricing_tiers.py`: 4 passed.
- `test_per_camera_billing_v2.py`: 33 passed.
- Total: 102 passed across these runs. Existing Pydantic v2 deprecation warnings remain.

Raw focused logs are in this directory's `review-evidence/` folder. The baseline full-suite comparison is Red's run; Codex did not rerun the heavy Docker comparison.

## AWS identity and permission finding

Read-only AWS evidence obtained 2026-10-18:

- `sts:GetCallerIdentity` succeeded for profile `anyaicam-deploy`, account `880690594006`, assumed role `AWSReservedSSO_AnyAiCamCloudDeploy`.
- `ssm:DescribeInstanceInformation` succeeded for the single approved target `i-0a082abd812929bb4`: Linux, SSM `Online`, agent `3.3.4793.0`.
- An IAM simulation attempt was denied because this deployment role lacks `iam:SimulatePrincipalPolicy`. That is a review/admin diagnostic permission, not a permission the deployment wrapper needs; do not add it to the deploy role. Use an AWS administrator's IAM simulator or permission-set review instead.
- No `SendCommand`, customer-data read, IAM policy change, credential-file read, production shell command, or deployment was performed. There is still no evidence that AWS IAM denied `SendCommand` itself. Red's runbook describes the prior refusal as Claude Code's local auto-mode approval, which is separate from IAM. Do not grant deployment permissions based solely on that refusal.

The approved wrapper calls `sts:GetCallerIdentity`, `ssm:SendCommand`, and `ssm:GetCommandInvocation`. Its caller identity is now verified, but its effective permission for the latter two actions remains unverified.

Minimum AWS permission delta to investigate, not apply:

1. `sts:GetCallerIdentity` does not require an identity-based allow; an SSO login/profile/role mismatch is a local authentication/identity issue, not a missing action grant.
2. If a sanitized CloudTrail or IAM simulation result confirms denial of the wrapper's send, add only `ssm:SendCommand` against the exact target instance ARN and the permitted SSM document ARN in `us-east-1`. The target reported by the reviewed procedure is `i-0a082abd812929bb4` in account `880690594006`; verify the account and instance again through the owner-approved inventory before any run.
3. `ssm:GetCommandInvocation` has no resource type in AWS's Service Authorization Reference, so it requires a `Resource: "*"` statement if the wrapper is to poll. Keep this statement limited to that action. Do not add `ssm:StartSession`, `ssm:SendAutomationSignal`, broad SSM write actions, or `AmazonSSMFullAccess`.
4. Separately, SSM Agent on the target needs its existing managed-node role. The backup upload running inside the VMS container needs narrowly scoped `s3:PutObject` for `anyaicam-db-backups-880690594006/cloud-db/predeploy/*` and `kms:GenerateDataKey` on the exact backup key. The independent readback step needs narrowly scoped object read and `kms:Decrypt` rights. Do not attach those data permissions to the human SSO role unless its verified workflow requires them; do not broaden the EC2 role without confirming the current role and bucket/key policies.

AWS docs: [SSM service authorization](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ssm.html), [S3 SSE-KMS permissions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html), and [S3 API permission mapping](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-with-s3-policy-actions.html).

Security caveat: the wrapper's script hash check is client-side. IAM permission to send `AWS-RunShellScript` does not enforce those approved hashes; a caller could bypass the wrapper and send arbitrary shell commands to the allowed host. Keep per-step owner approval. For a genuinely procedure-constrained role, use an administrator-maintained custom SSM document with fixed reviewed command content/version and grant `SendCommand` only for that document and the one target. Do not grant broad `AWS-RunShellScript` access as though it enforced the hash.

Deployment-kit runtime gate: `02-db-backup.sh` runs the boto3 uploader inside the live application container. The script does not explicitly pass AWS credentials, so boto3 will need a credential provider available inside that container. If this comes from EC2 IMDS, it may either fail (for example, with an IMDSv2 hop limit that blocks containers) or expose the instance role to the application container. Neither host setting is evidenced here. Before this step is eligible, Red must demonstrate the current metadata/credential path without displaying credentials and confirm the role cannot be reached by unrelated application requests; otherwise change the reviewed procedure to perform the upload from a protected host-side process with the narrow backup permissions. Do not weaken IMDS hop limits just to make the upload work.

## Before requesting IAM changes

Ask an AWS administrator (using an administrator identity, not by expanding the deploy role) to inspect the Identity Center permission set and attached managed/inline policies, permissions boundary, SCP, target instance profile, and KMS/bucket policies. Use IAM simulation against the exact `AWSReservedSSO_AnyAiCamCloudDeploy` role in Identity Center home region `us-east-2`, and separately check `ssm:SendCommand` against the exact instance/document pair and `ssm:GetCommandInvocation` against `*`. Obtain only a sanitized result with action, resource, decision, and policy-evaluation reason (no credential material). Then prepare a policy diff for owner approval. Keep the Claude Code rule separate: a narrowly approved local command rule is not an AWS IAM permission and must not allow arbitrary commands or bypass per-step owner approval.

Deployment remains unapproved and unperformed. Red must review both commits and the deployment-security note before any later integration or deployment decision.
