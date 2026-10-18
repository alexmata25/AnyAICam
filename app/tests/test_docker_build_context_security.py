"""Guard Docker build-context exclusions for local and production builds.

The Docker client sends the whole context to its builder, not only files
referenced by COPY. Keep common environment files, credential stores, Git
metadata, and private-key material excluded while retaining VMS runtime code.
"""

from fnmatch import fnmatchcase
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _rules():
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _rule_matches_path(path: str, rule: str) -> bool:
    """Match the simple basename/recursive forms used by this policy.

    The policy deliberately uses only literal path segments and `**/` or
    basename globs, avoiding Docker ignore negation and complex glob rules.
    A matching directory component excludes its descendants as Docker does.
    """
    parts = path.strip("/").split("/")
    pattern = rule.strip("/")

    if pattern.startswith("**/"):
        pattern = pattern[3:]
    if "/" not in pattern:
        return any(fnmatchcase(part, pattern) for part in parts)

    for start in range(len(parts)):
        candidate = "/".join(parts[start:])
        if fnmatchcase(candidate, pattern):
            return True
    return False


def _is_excluded(path: str, rules: list[str]) -> bool:
    # Negations can re-include a secret after an earlier exclusion. This
    # policy intentionally has no negation rules.
    return any(not rule.startswith("!") and _rule_matches_path(path, rule) for rule in rules)


def test_sensitive_paths_are_excluded_from_docker_context():
    rules = _rules()
    assert not any(rule.startswith("!") for rule in rules), "do not re-include excluded secrets"

    sensitive_paths = (
        "aws.env",
        "deploy/aws.env",
        ".env",
        ".env.production",
        "deploy/.env.staging",
        "app/config.env",
        "app/.git/objects/pack/index",
        ".git/config",
        ".aws/credentials",
        "home/runner/.aws/credentials",
        ".ssh/id_ed25519",
        "config/credentials.json",
        "config/credential.json",
        ".netrc",
        "deploy/.git-credentials",
        ".npmrc",
        ".pypirc",
        "secrets/stripe_api_key",
        "config/token.json",
        "config/client_secret_google.json",
        "tls/private.pem",
        "certs/server.key",
        "certs/client.p12",
        "certs/client.pfx",
        "cache/session.token",
        "config/service.secret",
        "home/id_rsa",
        "home/id_ed25519_work",
    )

    for path in sensitive_paths:
        assert _is_excluded(path, rules), f"sensitive build-context path is not excluded: {path}"


def test_docker_build_inputs_and_runtime_credentials_code_remain_in_context():
    rules = _rules()
    required_paths = (
        "Dockerfile",
        "requirements.txt",
        "requirements-cpu.txt",
        "requirements-push.txt",
        "app/main.py",
        "app/aws_cost_dashboard.py",
        "app/recording_credentials.py",
    )

    for path in required_paths:
        assert not _is_excluded(path, rules), f"required build input was excluded: {path}"


def test_aws_environment_file_is_untracked_ignored_and_excluded_without_reading_contents():
    # aws.env stopped being tracked on 2026-10-17 (no runtime reads it; production uses
    # /etc/anyaicam-staging/vms-staging.env, appliances their installer-written vms.env). A local copy may still
    # exist in a checkout: assert only pathnames -- never read, print or hash its contents.
    import subprocess
    gitignore = [line.strip() for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert "aws.env" in gitignore, "aws.env must be listed in .gitignore"
    assert _is_excluded("aws.env", _rules())
    if not (REPO_ROOT / ".git").exists():
        # A plain source copy (the regression container gets `git ls-files` output, no repository): only the
        # .gitignore/.dockerignore rules above can be checked here; tracking is checked wherever .git exists.
        return
    tracked = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch", "aws.env"],
                             capture_output=True, text=True)
    assert tracked.returncode != 0, "aws.env must not be tracked"
    ignored = subprocess.run(["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "aws.env"])
    assert ignored.returncode == 0, "aws.env must be git-ignored"
