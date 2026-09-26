#!/usr/bin/env bash
# Local, non-destructive, non-root test harness for
# install_privileged_watcher() (scripts/lib-privileged-watcher.sh).
#
# Sources the REAL production function unmodified -- no duplicated/
# rewritten logic that could drift from what actually ships. Every path
# it touches is redirected into a disposable fixture root by shadowing
# `install` and `systemctl` with shell functions of the same name
# (exactly the shadowing discipline installer/tests/run_tests.sh already
# uses for id/docker/df/apt-get) -- nothing under the real /opt, /etc,
# or systemd is read or written, and no real root privilege is required
# to run this.
#
# Usage: bash appliance-agent/tests/test_privileged_watcher_install.sh
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$TESTS_DIR/.." && pwd)"

PASS=0
FAIL=0

FIXTURE_ROOT="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_ROOT"' EXIT

reset_fixture() {
    rm -rf "$FIXTURE_ROOT"
    mkdir -p "$FIXTURE_ROOT"
    SOURCE_PAYLOAD_DIR="$FIXTURE_ROOT/payload/agent"
    OPT_ROOT="$FIXTURE_ROOT/opt/anyaicam-agent"
    SYSTEMD_DIR="$FIXTURE_ROOT/etc/systemd/system"
    mkdir -p "$SYSTEMD_DIR"
    export PRIVILEGED_WATCHER_INSTALL_DIR="$OPT_ROOT/privileged"
    export PRIVILEGED_WATCHER_SYSTEMD_DIR="$SYSTEMD_DIR"
    INSTALL_CALL_LOG="$FIXTURE_ROOT/.install-calls"
    SYSTEMCTL_CALL_LOG="$FIXTURE_ROOT/.systemctl-calls"
    rm -f "$INSTALL_CALL_LOG" "$SYSTEMCTL_CALL_LOG"
}

make_fake_watcher_payload() {
    mkdir -p "$SOURCE_PAYLOAD_DIR/system"
    cat > "$SOURCE_PAYLOAD_DIR/system/privileged_watcher.py" <<'FAKE_WATCHER'
#!/usr/bin/env python3
print("fake watcher")
FAKE_WATCHER
    cat > "$SOURCE_PAYLOAD_DIR/system/anyaicam-privileged-watcher.path" <<'FAKE_PATH_UNIT'
[Unit]
Description=fake path unit
[Path]
DirectoryNotEmpty=/var/lib/anyaicam/pending_actions
Unit=anyaicam-privileged-watcher.service
[Install]
WantedBy=multi-user.target
FAKE_PATH_UNIT
    cat > "$SOURCE_PAYLOAD_DIR/system/anyaicam-privileged-watcher.service" <<'FAKE_SERVICE_UNIT'
[Unit]
Description=fake service unit
[Service]
Type=oneshot
ExecStart=/opt/anyaicam-agent/privileged/watcher.py
FAKE_SERVICE_UNIT
}

# Shadow `install` well enough for this function's own two call shapes
# (`-d ... DIR` and `SRC DEST`) -- performs the real, unprivileged parts
# (mkdir/cp/chmod) into the fixture root so subsequent test -f/-d
# assertions work, and logs the full invocation (including any -o/-g,
# which would require real root to actually apply) so a test can assert
# on exactly what ownership/mode was requested without needing root.
install() {
    printf '%s\n' "$*" >>"$INSTALL_CALL_LOG"
    local args=("$@") is_dir=0 mode="" paths=()
    local i=0
    while [[ $i -lt ${#args[@]} ]]; do
        case "${args[$i]}" in
            -d) is_dir=1 ;;
            -m) i=$((i + 1)); mode="${args[$i]}" ;;
            -o) i=$((i + 1)) ;;
            -g) i=$((i + 1)) ;;
            *) paths+=("${args[$i]}") ;;
        esac
        i=$((i + 1))
    done
    if [[ "$is_dir" -eq 1 ]]; then
        local d
        for d in "${paths[@]}"; do
            mkdir -p "$d"
            [[ -n "$mode" ]] && chmod "$mode" "$d"
        done
    else
        local dest="${paths[$((${#paths[@]} - 1))]}"
        mkdir -p "$(dirname "$dest")"
        cp "${paths[0]}" "$dest"
        [[ -n "$mode" ]] && chmod "$mode" "$dest"
    fi
}

systemctl() {
    printf '%s\n' "$*" >>"$SYSTEMCTL_CALL_LOG"
    return 0
}

# shellcheck source=../scripts/lib-privileged-watcher.sh
source "$AGENT_DIR/scripts/lib-privileged-watcher.sh"

assert_eq() {
    local description="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        echo "PASS: $description"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $description (expected '$expected', got '$actual')"
        FAIL=$((FAIL + 1))
    fi
}

assert_exit() {
    local description="$1" expected_exit="$2"; shift 2
    local actual_exit=0
    ( "$@" ) >/dev/null 2>&1 || actual_exit=$?
    assert_eq "$description" "$expected_exit" "$actual_exit"
}

echo "== install_privileged_watcher(): required files packaged =="

# 1. Fails closed (and touches nothing) when the bundled payload is
#    missing -- never silently skip installing the one component with
#    real root privilege.
reset_fixture
assert_exit "fails when the watcher payload is entirely missing" 1 install_privileged_watcher "$SOURCE_PAYLOAD_DIR"
assert_exit "nothing is installed when the payload is missing" 1 test -f "$OPT_ROOT/privileged/watcher.py"

reset_fixture
mkdir -p "$SOURCE_PAYLOAD_DIR/system"
touch "$SOURCE_PAYLOAD_DIR/system/privileged_watcher.py"
# .path/.service units still missing
assert_exit "fails closed when only the watcher script is present (units missing)" 1 install_privileged_watcher "$SOURCE_PAYLOAD_DIR"

echo
echo "== install_privileged_watcher(): correct installation =="

# 2. A complete payload installs the script and both units.
reset_fixture
make_fake_watcher_payload
assert_exit "install_privileged_watcher succeeds with a complete payload" 0 install_privileged_watcher "$SOURCE_PAYLOAD_DIR"
assert_exit "watcher script installed" 0 test -f "$OPT_ROOT/privileged/watcher.py"
assert_exit ".path unit installed" 0 test -f "$SYSTEMD_DIR/anyaicam-privileged-watcher.path"
assert_exit ".service unit installed" 0 test -f "$SYSTEMD_DIR/anyaicam-privileged-watcher.service"

# 3. Least-privilege permissions: watcher script and its directory are
#    0700, root:root requested; unit files are the normal 0644 systemd
#    expects (readable by systemd, not writable by anyone but root).
reset_fixture
make_fake_watcher_payload
install_privileged_watcher "$SOURCE_PAYLOAD_DIR" >/dev/null 2>&1
assert_eq "watcher script requested as 0700 root:root (least privilege)" "1" "$(grep -c -- '-m 0700 -o root -g root .*privileged_watcher\.py .*watcher\.py' "$INSTALL_CALL_LOG")"
assert_eq "privileged directory requested as 0700 root:root" "1" "$(grep -c -- '-d -m 0700 -o root -g root .*privileged$' "$INSTALL_CALL_LOG")"
# Windows/NTFS cannot represent real POSIX permission bits at all --
# the same platform gap installer/tests/test_build_release_installer.py's
# DeterministicTarExecutableBitTests already documents and skips for the
# executable-bit case. The `install -m 0700 -o root -g root ...`
# assertion above already proves the correct mode/ownership was
# *requested*; this one additionally proves it actually lands on disk,
# meaningful only on a real POSIX filesystem.
case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
        echo "SKIP: watcher script is actually 0700 on disk (Windows/NTFS cannot represent real POSIX permission bits)"
        ;;
    *)
        assert_eq "watcher script is actually 0700 on disk" "700" "$(stat -c '%a' "$OPT_ROOT/privileged/watcher.py" 2>/dev/null || stat -f '%Lp' "$OPT_ROOT/privileged/watcher.py" 2>/dev/null)"
        ;;
esac
assert_eq "path unit installed at 0644 (systemd-readable, not group/other-writable)" "1" "$(grep -c -- '-m 0644 .*anyaicam-privileged-watcher\.path' "$INSTALL_CALL_LOG")"

# 4. Only the .path unit is enabled/started -- the oneshot .service must
#    never be enabled for boot directly.
reset_fixture
make_fake_watcher_payload
install_privileged_watcher "$SOURCE_PAYLOAD_DIR" >/dev/null 2>&1
assert_eq "daemon-reload was called" "1" "$(grep -c '^daemon-reload$' "$SYSTEMCTL_CALL_LOG")"
assert_eq "the .path unit is enabled --now" "1" "$(grep -c '^enable --now anyaicam-privileged-watcher.path$' "$SYSTEMCTL_CALL_LOG")"
assert_eq "the .service unit is never separately enabled or started" "0" "$(grep -cE 'anyaicam-privileged-watcher\.service' "$SYSTEMCTL_CALL_LOG")"

echo
echo "== install_privileged_watcher(): idempotent reruns preserve everything else =="

# 5. Rerunning (repair/reinstall) succeeds again, still installs
#    correctly, and -- the actual point of this scenario -- never
#    touches recordings/configuration paths it has no business touching
#    in the first place, proving this addition can't be the cause of
#    any installer-rerun data-loss regression.
reset_fixture
make_fake_watcher_payload
install_privileged_watcher "$SOURCE_PAYLOAD_DIR" >/dev/null 2>&1
RECORDINGS_DIR="$FIXTURE_ROOT/var/lib/anyaicam/vms/recordings"
mkdir -p "$RECORDINGS_DIR"
echo "real-recording-data" > "$RECORDINGS_DIR/clip1.mp4"
PENDING_DIR="$FIXTURE_ROOT/var/lib/anyaicam/pending_actions"
mkdir -p "$PENDING_DIR"
echo '{"type":"restart_vms","command_id":"already-queued"}' > "$PENDING_DIR/restart_vms.json"
assert_exit "a second (repair) run succeeds" 0 install_privileged_watcher "$SOURCE_PAYLOAD_DIR"
assert_eq "recordings are untouched by a repair run" "real-recording-data" "$(cat "$RECORDINGS_DIR/clip1.mp4" 2>/dev/null)"
assert_eq "an already-queued pending action marker is untouched by a repair run" '{"type":"restart_vms","command_id":"already-queued"}' "$(cat "$PENDING_DIR/restart_vms.json" 2>/dev/null)"
assert_exit "watcher script still present after repair" 0 test -f "$OPT_ROOT/privileged/watcher.py"

echo
echo "== uninstall_privileged_watcher(): leaves no stale unit or software behind =="

# 6. Concrete defect found while reviewing the uninstall/reinstall
#    workflow ahead of Samsung deployment: appliance-agent/scripts/
#    uninstall.sh already did `rm -rf /opt/anyaicam-agent` (removing
#    this watcher's own script) but nothing disabled or removed its two
#    systemd units -- a default uninstall left the .path unit enabled
#    and watching a directory with no watcher script left to run.
reset_fixture
make_fake_watcher_payload
install_privileged_watcher "$SOURCE_PAYLOAD_DIR" >/dev/null 2>&1
: >"$SYSTEMCTL_CALL_LOG"  # clear install-time calls so uninstall's own are isolated below
assert_exit "uninstall_privileged_watcher succeeds" 0 uninstall_privileged_watcher
assert_eq "the .path unit is disabled --now" "1" "$(grep -c '^disable --now anyaicam-privileged-watcher.path$' "$SYSTEMCTL_CALL_LOG")"
assert_exit "watcher script is removed" 1 test -f "$OPT_ROOT/privileged/watcher.py"
assert_exit "watcher directory is removed" 1 test -d "$OPT_ROOT/privileged"
assert_exit ".path unit file is removed" 1 test -f "$SYSTEMD_DIR/anyaicam-privileged-watcher.path"
assert_exit ".service unit file is removed" 1 test -f "$SYSTEMD_DIR/anyaicam-privileged-watcher.service"
assert_eq "daemon-reload was called after removal" "1" "$(grep -c '^daemon-reload$' "$SYSTEMCTL_CALL_LOG")"

# 7. Uninstall never touches recordings or a queued pending action --
#    those belong to /var/lib/anyaicam, a completely different tree
#    this function has no path variable pointing at, but the properties
#    checked in Phase 1's own default-uninstall coverage
#    (installer/tests/run_tests.sh) are worth re-confirming here since
#    this is the function that changed.
reset_fixture
make_fake_watcher_payload
install_privileged_watcher "$SOURCE_PAYLOAD_DIR" >/dev/null 2>&1
RECORDINGS_DIR="$FIXTURE_ROOT/var/lib/anyaicam/vms/recordings"
mkdir -p "$RECORDINGS_DIR"
echo "real-recording-data" > "$RECORDINGS_DIR/clip1.mp4"
uninstall_privileged_watcher >/dev/null 2>&1
assert_eq "recordings are untouched by uninstall" "real-recording-data" "$(cat "$RECORDINGS_DIR/clip1.mp4" 2>/dev/null)"

# 8. Uninstalling when nothing was ever installed is a safe no-op
#    (matches run_uninstall()'s own `|| true` / 2>/dev/null tolerance
#    for a component that may never have been present).
reset_fixture
assert_exit "uninstalling a never-installed watcher does not error" 0 uninstall_privileged_watcher

echo
echo "== summary: $PASS passed, $FAIL failed =="
[[ "$FAIL" -eq 0 ]]
