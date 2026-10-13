#!/usr/bin/env bash
# Deploy exactly the VMS payload embedded in the built installer artifact.
# No application files are read from the surrounding Git checkout.

migrate_legacy_persistent_data() {
    local old="$1" new="$2" label="$3"
    [[ -d "$old" ]] || return 0
    log "Found legacy $label under $old -- migrating to $new ..."
    mkdir -p "$new"
    chmod 0750 "$new"
    chown anyaicam:anyaicam "$new" 2>/dev/null || true
    rsync -a --ignore-existing "$old/" "$new/"
    local unresolved=0 rel
    while IFS= read -r -d '' f; do
        rel="${f#"$old"/}"
        if [[ -f "$new/$rel" ]] && cmp -s "$f" "$new/$rel"; then
            rm -f "$f"
        else
            log "WARNING: could not verify migration of $label file '$rel' -- leaving it at $old for manual review."
            unresolved=1
        fi
    done < <(find "$old" -type f -print0)
    find "$old" -type d -empty -delete 2>/dev/null || true
    if [[ "$unresolved" -eq 0 ]]; then
        rmdir "$old" 2>/dev/null || true
    fi
}

migrate_legacy_persistent_file() {
    local old="$1" new="$2" label="$3"
    [[ -f "$old" ]] || return 0
    if [[ ! -f "$new" ]]; then
        log "Found legacy $label at $old -- migrating to $new ..."
        mkdir -p "$(dirname "$new")"
        if ! printf 'op copy\nsource %s\npath %s\nmode 0640\nowner anyaicam\n' "$old" "$new" | agent_file; then
            log "WARNING: could not safely copy legacy $label from $old -- preserving the original."
            return 1
        fi
        if ! cmp -s "$old" "$new"; then
            log "WARNING: could not verify migrated $label at $new -- preserving the original at $old."
            return 1
        fi
        rm -f "$old"
    elif cmp -s "$old" "$new"; then
        rm -f "$old"
    else
        log "WARNING: legacy $label differs from existing $new -- preserving both."
    fi
}

# Root edits files inside folders the anyaicam user owns (/etc/anyaicam).
# Shell redirection, cp and chown there would follow a symlink that user
# planted, so every such write goes through the Software Update applier's
# symlink-safe routines (appliance-agent/system/apply_release.py:
# agent_file_main): never through a symlink, owner and mode set on the new
# file itself, atomic rename. Requests travel on stdin, so values -- secrets
# included -- never appear on a command line.
_agent_file_helper_dir() {
    local here candidate
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    for candidate in "${PAYLOAD_DIR:-}/agent/system" "$here/payload/agent/system" "$here/../appliance-agent/system"; do
        if [[ -f "$candidate/apply_release.py" ]]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    echo "[ERROR] The installer's file helper (agent/system/apply_release.py) is missing." >&2
    return 1
}

agent_file() {
    local helper
    helper="$(_agent_file_helper_dir)" || return 1
    python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import apply_release; sys.exit(apply_release.agent_file_main())' "$helper"
}

upsert_env_key() {
    local file="$1" key="$2" value="$3"
    printf 'op update_env\npath %s\nmode 0640\nowner anyaicam\noverride %s=%s\n' "$file" "$key" "$value" | agent_file
}

ensure_vms_env() {
    mkdir -p "$CONFIG_DIR"
    chmod 0750 "$CONFIG_DIR"
    local template=""
    [[ -f "$PAYLOAD_DIR/config/vms.env.template" ]] && template="$PAYLOAD_DIR/config/vms.env.template"
    if [[ ! -e "$VMS_ENV_FILE" && ! -L "$VMS_ENV_FILE" ]]; then
        if [[ -n "$template" ]]; then
            log "Creating VMS environment config from the release template (existing configs are never overwritten)."
        else
            log "Creating minimal VMS environment config (existing configs are never overwritten)."
        fi
    fi
    # Every default below is added only when its key is absent; existing
    # values are never changed. One safe edit (see agent_file above).
    local -a env_request=("op update_env" "path $VMS_ENV_FILE" "mode 0640" "owner anyaicam")
    [[ -n "$template" ]] && env_request+=("template $template")

    env_request+=("default ANYAICAM_RUNTIME_ROLE=edge")
    # Canonical name is ANYAICAM_ENV -- the only variable app/main.py and
    # app/cloud_config.py actually read (DEPLOYMENT_ENV = os.environ.get
    # ("ANYAICAM_ENV", "local")). This installer previously wrote
    # ANYAICAM_ENVIRONMENT here, a different name the app has never read
    # -- every appliance installed that way silently stayed on the
    # "local" default forever, regardless of this line ever running.
    env_request+=("default ANYAICAM_ENV=production")

    # Live View staging transport (2026-09-13): the existing S3/CloudFront
    # live-relay worker (app/live_relay_uploader.py) already self-gates on
    # this flag and defaults OFF in the app if the key is absent entirely
    # -- this line only makes that default explicit and present in every
    # installed appliance's own env file, the same way ANYAICAM_RUNTIME_
    # ROLE/ANYAICAM_ENV above do, so a future repair-install never has to
    # guess whether an existing appliance already has an opinion here.
    # Never overwrites an existing value -- flipping this to true for a
    # specific pilot appliance (alongside setting AWS_REGION and the
    # cloud-side live_relay_pilot DB flag) is a separate, explicit,
    # per-appliance decision, not something this installer makes for
    # every appliance by default.
    env_request+=("default ANYAICAM_LIVE_RELAY_ENABLED=false")

    # On-device analytics workers whose code default is off (2026-09-25).
    # Advanced Analytics grants People Counting and LPR per camera from the
    # cloud (cameras.<analytic>_enabled), but these appliance-wide switches
    # defaulted to false and nothing set them, so a fresh appliance never
    # ran either analytic even for a paying customer. Each worker still
    # does nothing for a camera without that camera's entitlement (and,
    # for People Counting / zones / alert lines, a customer-drawn line or
    # zone). Add-if-missing, like every default here: an explicit value,
    # true or false, is never changed. Facial recognition stays an
    # explicit opt-in (biometric processing), not an installer default.
    local analytics_flag
    for analytics_flag in ANYAICAM_LPR_ENABLED PEOPLE_COUNTING_ENABLED CUSTOMER_ANALYTICS_RULES_ENABLED; do
        env_request+=("default ${analytics_flag}=true")
    done

    # Generated once, per appliance, the first time this file has no
    # value yet -- and, like ANYAICAM_ENV/ANYAICAM_RUNTIME_ROLE above
    # (never like the always-refreshed build-identity keys below), NEVER
    # regenerated once present: rotating it silently on every
    # reinstall/repair would instantly invalidate every existing signed
    # session/cookie. 32 raw bytes (256 bits) of /dev/urandom entropy,
    # hex-encoded with only coreutils (od/tr) -- no new dependency, and
    # deliberately never derived from the appliance ID, hostname, MAC
    # address, or anything else an attacker could predict or observe by
    # other means. Never printed or logged anywhere: the generated value
    # exists only in this command substitution and the file it's
    # redirected into.
    env_request+=("default ANYAICAM_APP_SECRETS=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')")

    # Camera credential encryption key -- an appliance/installer
    # requirement, not a Samsung-only fix: confirmed live that a fresh
    # edge appliance never provisioned this at all, so ANY attempt to
    # add a discovered camera WITH ONVIF/RTSP credentials failed closed
    # (app/appliance_protocol.py's encrypt_camera_credentials() returns
    # nothing without a key, and app/partner_workspace.py's
    # request_camera_provisioning() 503s rather than ever storing
    # credentials it can't encrypt). Generated once, exactly like
    # ANYAICAM_APP_SECRETS above, and for the same reason NEVER
    # regenerated once present: this key is what every already-stored
    # camera credential (camera_credentials.encrypted_blob) is encrypted
    # with -- silently rotating it on a reinstall/repair would make every
    # existing credential permanently undecryptable, breaking every
    # already-working camera stream. A valid Fernet key (the format
    # app/appliance_protocol.py requires) is 32 random bytes, URL-safe
    # base64-encoded -- reproduced here with only coreutils (head/base64/
    # tr), the same no-new-dependency approach as ANYAICAM_APP_SECRETS,
    # since this must generate correctly before the VMS image (which
    # bundles the `cryptography` package) has even been built yet. Never
    # printed or logged anywhere: the generated value exists only in this
    # command substitution and the file it's redirected into.
    env_request+=("default ANYAICAM_CAMERA_CREDENTIAL_KEY=$(head -c 32 /dev/urandom | base64 | tr -d '\n' | tr '+/' '-_')")
    printf '%s\n' "${env_request[@]}" | agent_file || return $?

    persist_product_mode

    # These two keys are installer-owned build identity. They are updated on
    # every reinstall/repair while all other customer configuration survives.
    upsert_env_key "$VMS_ENV_FILE" "ANYAICAM_VMS_COMMIT" "$VMS_RELEASE_COMMIT"
    upsert_env_key "$VMS_ENV_FILE" "ANYAICAM_BUILD_ID" "$VMS_RELEASE_COMMIT"
    # Software Update (2026-10-03): the product release version /version
    # reports next to build_id (older installers carry none).
    if [[ -n "${RELEASE_VERSION:-}" ]]; then
        upsert_env_key "$VMS_ENV_FILE" "ANYAICAM_VERSION" "$RELEASE_VERSION"
    fi
    # Owner (anyaicam) and mode (0640) were set by each safe write above.
}

# The installed VMS software tree must be root-owned: the container
# bind-mounts $VMS_INSTALL_ROOT/app over /app, so whoever can write it
# controls the code the VMS runs. The paths the mirror excludes (legacy
# persistent state, and the separately-installed MediaMTX binary) are
# left exactly as they are.
normalize_vms_install_ownership() {
    local owner="${VMS_INSTALL_OWNER:-root:root}"
    find "$VMS_INSTALL_ROOT" \( -path "$VMS_INSTALL_ROOT/recordings" -o -path "$VMS_INSTALL_ROOT/data/config" \
        -o -path "$VMS_INSTALL_ROOT/.env" -o -path "$VMS_INSTALL_ROOT/mediamtx" \) -prune \
        -o -exec chown -h "$owner" {} +
}

# ---------------------------------------------------------------- stale backup directories
# (2026-09-30) Hand-made backups left inside the install root (e.g. the
# Ryzen's app-pre-event-mode-pilot-20260919T223956Z) made every upgrade print
# "cannot delete non-empty directory": the mirror below protects any folder
# named recordings/ (legacy customer state), so a backup holding one can never
# be deleted -- and it should not be silently deleted anyway. Top-level
# folders named like a backup (*-pre-*) that are not part of the release are
# MOVED, contents intact, to $ROLLBACK_DIR/stale-install-dirs/ before the
# mirror runs. Everything else is handled exactly as before.
relocate_stale_install_dirs() {
    local entry name dest_root dest stamp
    [[ -d "$VMS_INSTALL_ROOT" ]] || return 0
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    dest_root="${ROLLBACK_DIR:-/var/lib/anyaicam/rollback}/stale-install-dirs"
    for entry in "$VMS_INSTALL_ROOT"/*-pre-*; do
        [[ -d "$entry" && ! -L "$entry" ]] || continue
        name="$(basename "$entry")"
        [[ -e "$VMS_PAYLOAD_DIR/$name" ]] && continue
        mkdir -p "$dest_root" && chmod 0750 "$dest_root" || return 1
        dest="$dest_root/$name-moved-$stamp"
        mv "$entry" "$dest" || return 1
        log "Moved stale backup directory $name out of $VMS_INSTALL_ROOT to $dest (contents kept)."
    done
}

# ---------------------------------------------------------------- rollback point
# (2026-09-25) A repair/upgrade rebuilds anyaicam-vms:latest in place and
# rsync --delete replaces the code, so before this the previous release
# survived only if someone tagged/archived it by hand first. Every
# non-clean install now leaves, BEFORE touching anything:
#   - the previous image tagged anyaicam-vms:rollback-<previous commit>
#   - the previous code tree (no recordings/config/.env/secrets) archived
#   - an online, integrity-checked copy of the VMS database
#   - a manifest (ROLLBACK_DIR/rollback-*.env, latest.env) that rollback.sh
#     restores from.
# Nothing here deletes or rewrites existing data; old rollback points are
# kept (pruning them is left to the operator).
# Root's own state directory (2026-10-04), not /var/lib/anyaicam/rollback:
# that older location sits inside a directory the unprivileged agent user
# owns, which could swap in a forged manifest for an operator to run as root.
# Software Update writes its rollback points here too.
ROLLBACK_DIR="${ANYAICAM_ROLLBACK_DIR:-${UPDATE_STATE_DIR:-/var/lib/anyaicam-update}/rollback}"
ROLLBACK_TOOL="${ANYAICAM_ROLLBACK_TOOL:-/usr/local/sbin/anyaicam-rollback}"
VMS_IMAGE="${ANYAICAM_VMS_IMAGE:-anyaicam-vms}"
VMS_CONTAINER="${ANYAICAM_VMS_CONTAINER:-anyaicam-vms}"
VMS_DATABASE_NAME="partner_portal.db"
PREPARED_VMS_IMAGE_TAG=""

# ---------------------------------------------------------------- preloaded VMS image
# (2026-10-11) A release built with build_release_installer.py
# --preload-vms-image carries the VMS image already built from exactly this
# payload (images/anyaicam-vms-<commit12>.tar.zst|.tar.gz) and records it in
# release.env: archive SHA-256 and size, image ID and config digest, payload
# digest, platform. release.env and the archive are both listed in
# artifact-files.json, which verify_installer_payload checked before anything
# changed (and, for Software Update, the signed manifest covers the whole
# package). The appliance then only runs `docker load` -- no CPU-heavy build,
# no package or model downloads -- but only after every check below passes:
#   - the archive is a regular file inside this installer folder;
#   - the staged build context has the payload digest the image was built from;
#   - enough free space for a root-only copy of the archive and the load;
#   - the root-only copy has the recorded SHA-256 (checked BEFORE loading, so
#     an archive swapped after the start-up check is never loaded);
#   - the loaded image has the recorded ID and the release's labels.
# Anything else is logged and the existing `docker build` path runs instead:
# an unverified image is never tagged for use. ANYAICAM_VMS_PRELOAD=0 skips
# the preloaded image. Everything here still runs before downtime.
VMS_PRELOAD_LABEL_COMMIT="com.anyaicam.vms.release-commit"
VMS_PRELOAD_LABEL_PAYLOAD="com.anyaicam.vms.payload-sha256"
VMS_PRELOAD_MARGIN_BYTES=$(( 1024 * 1024 * 1024 ))

# Prints the payload digest of a staged VMS build context. Byte-for-byte the
# same definition as build_release_installer.py vms_payload_digest().
vms_payload_digest() {
    python3 - "$1" <<'PYEOF'
import hashlib, os, sys
root = sys.argv[1]
rows = []
for directory, dirs, files in os.walk(root):
    for name in dirs + files:
        path = os.path.join(directory, name)
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if os.path.islink(path):
            sys.exit(f"symlink in the staged VMS payload: {rel}")
        if name in files and os.path.isfile(path):
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            rows.append((rel.encode("utf-8"), digest.hexdigest()))
rows.sort()
total = hashlib.sha256()
for rel, digest in rows:
    total.update(digest.encode("ascii") + b"  " + rel + b"\n")
print(total.hexdigest())
PYEOF
}

_vms_free_bytes() {
    df --output=avail -B1 "$1" 2>/dev/null | tail -n 1 | tr -d ' '
}

_vms_preload_reject() {
    log "Preloaded VMS image not used: $* -- building the image on this machine instead."
    return 1
}

# Prints nothing; returns 0 when every filesystem involved in loading the
# preloaded image has room (see load_preloaded_vms_image).
_vms_preload_space_ok() {
    local staging_root="$1" docker_root containerd_root dir dev need free i j found
    local need_image=$(( VMS_PRELOAD_IMAGE_BYTES + VMS_PRELOAD_IMAGE_UNPACKED_BYTES + VMS_PRELOAD_MARGIN_BYTES ))
    local -a dirs=() kinds=() devs=() dev_need=() dev_dir=() dev_has_image=()
    docker_root="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null)"
    [[ -n "$docker_root" && -d "$docker_root" ]] || docker_root=/var/lib/docker
    [[ -d "$docker_root" ]] || docker_root=/var/lib
    containerd_root="${ANYAICAM_CONTAINERD_ROOT:-/var/lib/containerd}"
    dirs+=("$staging_root"); kinds+=(archive)
    dirs+=("$docker_root"); kinds+=(image)
    if [[ -d "$containerd_root" ]]; then
        dirs+=("$containerd_root"); kinds+=(image)
    fi
    for i in "${!dirs[@]}"; do
        dev="$(stat -c %d -- "${dirs[$i]}" 2>/dev/null)" || { _vms_preload_reject "cannot inspect ${dirs[$i]}"; return 1; }
        found=-1
        for j in "${!devs[@]}"; do [[ "${devs[$j]}" == "$dev" ]] && found=$j; done
        if (( found < 0 )); then
            found=${#devs[@]}
            devs+=("$dev"); dev_need+=(0); dev_dir+=("${dirs[$i]}"); dev_has_image+=(0)
        fi
        if [[ "${kinds[$i]}" == archive ]]; then
            dev_need[found]=$(( dev_need[found] + VMS_PRELOAD_IMAGE_BYTES + VMS_PRELOAD_MARGIN_BYTES ))
        elif [[ "${dev_has_image[$found]}" == 0 ]]; then
            # The image lands in Docker's or containerd's root, never both on one disk.
            dev_need[found]=$(( dev_need[found] + need_image )); dev_has_image[found]=1
        fi
    done
    for i in "${!devs[@]}"; do
        dir="${dev_dir[$i]}"; need="${dev_need[$i]}"
        free="$(_vms_free_bytes "$dir")"
        [[ "$free" =~ ^[0-9]+$ ]] || { _vms_preload_reject "free disk space at $dir could not be determined"; return 1; }
        if (( free < need )); then
            _vms_preload_reject "not enough free disk space to load it (need $(( need / 1048576 )) MiB on the filesystem of $dir, $(( free / 1048576 )) MiB free)"
            return 1
        fi
    done
    return 0
}

# Removes the preload name this run created. A name that existed before the
# run ($1 = its image ID then) is pointed back at that image: docker load
# moves the name to the loaded image.
_vms_preload_untag() {
    if [[ -n "$1" ]]; then
        docker tag "$1" "$VMS_PRELOAD_IMAGE_REF" >/dev/null 2>&1 \
            || log "WARNING: could not restore $VMS_PRELOAD_IMAGE_REF to its previous image $1."
        return 0
    fi
    docker image rm "$VMS_PRELOAD_IMAGE_REF" >/dev/null 2>&1 || true
}

# $1 = staged build context (root-only), $2 = staging root, $3 = release tag.
# Returns 0 only when $3 now names the verified preloaded image.
load_preloaded_vms_image() {
    local staged="$1" staging_root="$2" image_tag="$3"
    local short="${VMS_RELEASE_COMMIT:0:12}" tar_rel tar_path bundle_root real_tar
    local load_timeout copy_dir copy sha actual_payload loaded_id label_commit label_payload
    local arch expected_platform preload_ref_previous=""

    if [[ "${ANYAICAM_VMS_PRELOAD:-1}" == "0" ]]; then
        log "ANYAICAM_VMS_PRELOAD=0: not using a preloaded VMS image."
        return 1
    fi
    tar_rel="${VMS_PRELOAD_IMAGE_FILE:-}"
    if [[ -z "$tar_rel" && -z "${ANYAICAM_VMS_IMAGE_TAR:-}" ]]; then
        return 1   # this release ships no preloaded image: build as before
    fi
    log "Checking the preloaded VMS image shipped with this release ..."
    # The recorded metadata (release.env, verified at start-up) must be complete.
    [[ "$tar_rel" =~ ^images/anyaicam-vms-[0-9a-f]{12}\.tar\.(zst|gz)$ && "$tar_rel" == "images/anyaicam-vms-$short."* ]] \
        || { _vms_preload_reject "the release does not record a valid preloaded image for $short"; return 1; }
    [[ "${VMS_PRELOAD_IMAGE_SHA256:-}" =~ ^[0-9a-f]{64}$ ]] || { _vms_preload_reject "no valid recorded archive SHA-256"; return 1; }
    [[ "${VMS_PRELOAD_IMAGE_PAYLOAD_SHA256:-}" =~ ^[0-9a-f]{64}$ ]] || { _vms_preload_reject "no valid recorded payload digest"; return 1; }
    [[ "${VMS_PRELOAD_IMAGE_ID:-}" =~ ^sha256:[0-9a-f]{64}$ && "${VMS_PRELOAD_IMAGE_CONFIG_DIGEST:-}" =~ ^sha256:[0-9a-f]{64}$ ]] \
        || { _vms_preload_reject "no valid recorded image ID"; return 1; }
    [[ "${VMS_PRELOAD_IMAGE_BYTES:-}" =~ ^[1-9][0-9]{0,14}$ && "${VMS_PRELOAD_IMAGE_UNPACKED_BYTES:-}" =~ ^[1-9][0-9]{0,14}$ ]] \
        || { _vms_preload_reject "no valid recorded archive size"; return 1; }
    [[ "${VMS_PRELOAD_IMAGE_REF:-}" == "anyaicam-vms-preload:$short" ]] || { _vms_preload_reject "unexpected recorded image name"; return 1; }
    arch="$(uname -m)"
    case "$arch" in
        x86_64) expected_platform="linux/amd64" ;;
        aarch64) expected_platform="linux/arm64" ;;
        *) expected_platform="unknown" ;;
    esac
    [[ "${VMS_PRELOAD_IMAGE_PLATFORM:-}" == "$expected_platform" ]] \
        || { _vms_preload_reject "it was built for ${VMS_PRELOAD_IMAGE_PLATFORM:-an unknown platform}, this machine is $arch"; return 1; }

    # Only a file inside this verified installer folder is ever considered.
    [[ -n "${INSTALLER_DIR:-}" ]] || { _vms_preload_reject "the installer folder is unknown"; return 1; }
    bundle_root="$(realpath -e -- "$INSTALLER_DIR" 2>/dev/null)" || { _vms_preload_reject "the installer folder cannot be resolved"; return 1; }
    tar_path="${ANYAICAM_VMS_IMAGE_TAR:-$INSTALLER_DIR/$tar_rel}"
    if [[ ! -e "$tar_path" ]]; then
        _vms_preload_reject "the image archive $tar_path is missing"; return 1
    fi
    if [[ -L "$tar_path" || ! -f "$tar_path" ]]; then
        _vms_preload_reject "$tar_path is not a regular file"; return 1
    fi
    real_tar="$(realpath -e -- "$tar_path" 2>/dev/null)" || { _vms_preload_reject "$tar_path cannot be resolved"; return 1; }
    [[ "$real_tar" == "$bundle_root/"* ]] || { _vms_preload_reject "$tar_path is outside the verified installer folder $bundle_root"; return 1; }
    [[ "$(stat -c %s -- "$real_tar")" == "$VMS_PRELOAD_IMAGE_BYTES" ]] || { _vms_preload_reject "the image archive has the wrong size"; return 1; }

    # The image must have been built from exactly the context staged here.
    actual_payload="$(vms_payload_digest "$staged")" || { _vms_preload_reject "the staged payload digest could not be computed"; return 1; }
    [[ "$actual_payload" == "$VMS_PRELOAD_IMAGE_PAYLOAD_SHA256" ]] \
        || { _vms_preload_reject "the staged VMS payload ($actual_payload) is not the one the image was built from"; return 1; }

    # Space: a root-only copy of the archive at the staging root; in Docker's
    # storage the stored layer blobs (at most the archive's size) plus the
    # extracted layers (VMS_PRELOAD_IMAGE_UNPACKED_BYTES, measured by the
    # builder); a margin. With the containerd image store (Docker 29 on the
    # Ryzen: overlayfs/io.containerd.snapshotter.v1) image content lives under
    # containerd's root, not DockerRootDir, so BOTH are checked. Needs are
    # summed per filesystem, so folders on the same disk count once each.
    _vms_preload_space_ok "$staging_root" || return 1

    # Verify a root-only copy, then load exactly that copy.
    copy_dir="$(mktemp -d "$staging_root/image.XXXXXXXX")" || { _vms_preload_reject "no staging space for the archive"; return 1; }
    chmod 0700 "$copy_dir" || { rm -rf -- "$copy_dir"; _vms_preload_reject "could not secure the archive staging folder"; return 1; }
    copy="$copy_dir/${tar_rel##*/}"
    if ! cp -- "$real_tar" "$copy"; then
        rm -rf -- "$copy_dir"; _vms_preload_reject "could not copy the image archive"; return 1
    fi
    sha="$(sha256sum -- "$copy" | awk '{print $1}')"
    if [[ "$sha" != "$VMS_PRELOAD_IMAGE_SHA256" ]]; then
        rm -rf -- "$copy_dir"
        _vms_preload_reject "the image archive SHA-256 ($sha) does not match the release ($VMS_PRELOAD_IMAGE_SHA256); it was NOT loaded"
        return 1
    fi
    # A tag of that name that already existed before this run (e.g. from an
    # operator's own load) is never removed: its image ID is recorded and the
    # name is restored to it afterwards, whatever happens.
    # Fail closed (Codex review of 8e1164d): only "the listing worked and the
    # name is absent" counts as no prior tag; a failed listing, or a tag
    # whose ID cannot be read, skips the preload and builds instead.
    local preload_ref_listing
    if ! preload_ref_listing="$(docker image ls -q --no-trunc "$VMS_PRELOAD_IMAGE_REF" 2>/dev/null)"; then
        rm -rf -- "$copy_dir"
        _vms_preload_reject "could not check for an existing $VMS_PRELOAD_IMAGE_REF"; return 1
    fi
    preload_ref_previous=""
    if [[ -n "$preload_ref_listing" ]]; then
        preload_ref_previous="$(docker image inspect -f '{{.Id}}' "$VMS_PRELOAD_IMAGE_REF" 2>/dev/null)" || preload_ref_previous=""
        if [[ ! "$preload_ref_previous" =~ ^sha256:[0-9a-f]{64}$ ]]; then
            rm -rf -- "$copy_dir"
            _vms_preload_reject "the existing $VMS_PRELOAD_IMAGE_REF has an unreadable image ID"; return 1
        fi
    fi
    load_timeout="${ANYAICAM_VMS_LOAD_TIMEOUT:-1800}"
    [[ "$load_timeout" =~ ^[1-9][0-9]*$ ]] || load_timeout=1800
    log "Loading the verified preloaded VMS image ($(( VMS_PRELOAD_IMAGE_BYTES / 1048576 )) MiB, timeout ${load_timeout}s) ..."
    if ! timeout --kill-after=30s "${load_timeout}s" docker load -q -i "$copy"; then
        rm -rf -- "$copy_dir"
        _vms_preload_untag "$preload_ref_previous"
        _vms_preload_reject "docker load failed"; return 1
    fi
    rm -rf -- "$copy_dir" || log "WARNING: could not remove the temporary image archive copy in $copy_dir."

    loaded_id="$(docker image inspect -f '{{.Id}}' "$VMS_PRELOAD_IMAGE_REF" 2>/dev/null)"
    label_commit="$(docker image inspect -f "{{index .Config.Labels \"$VMS_PRELOAD_LABEL_COMMIT\"}}" "$VMS_PRELOAD_IMAGE_REF" 2>/dev/null)"
    label_payload="$(docker image inspect -f "{{index .Config.Labels \"$VMS_PRELOAD_LABEL_PAYLOAD\"}}" "$VMS_PRELOAD_IMAGE_REF" 2>/dev/null)"
    if [[ -z "$loaded_id" || ( "$loaded_id" != "$VMS_PRELOAD_IMAGE_ID" && "$loaded_id" != "$VMS_PRELOAD_IMAGE_CONFIG_DIGEST" ) ]]; then
        _vms_preload_untag "$preload_ref_previous"
        _vms_preload_reject "the loaded image ID (${loaded_id:-none}) is not the release's ($VMS_PRELOAD_IMAGE_ID)"; return 1
    fi
    if [[ "$label_commit" != "$VMS_RELEASE_COMMIT" || "$label_payload" != "$VMS_PRELOAD_IMAGE_PAYLOAD_SHA256" ]]; then
        _vms_preload_untag "$preload_ref_previous"
        _vms_preload_reject "the loaded image's labels (commit ${label_commit:-none}, payload ${label_payload:-none}) do not match this release"; return 1
    fi
    # Tag by the verified ID, never by a name something else could move.
    if ! docker tag "$loaded_id" "$image_tag" || [[ "$(docker image inspect -f '{{.Id}}' "$image_tag" 2>/dev/null)" != "$loaded_id" ]]; then
        _vms_preload_untag "$preload_ref_previous"
        _vms_preload_reject "could not tag the verified image as $image_tag"; return 1
    fi
    _vms_preload_untag "$preload_ref_previous"   # untag only; $image_tag keeps it
    log "Verified preloaded VMS image $loaded_id loaded as $image_tag (no image build needed)."
    return 0
}

# Build from a root-only copy of the verified release payload before changing
# any live VMS files or rollback metadata. The image build downloads OS/Python
# packages and model assets, so it must complete while the current VMS is up.
# When the release ships a verified preloaded image (load_preloaded_vms_image
# above), that image is loaded instead and no build runs.
prepare_vms_image() {
    local build_timeout staging_root staging_dir image_tag build_status
    build_timeout="${ANYAICAM_VMS_BUILD_TIMEOUT:-5400}"
    staging_root="${ANYAICAM_INSTALLER_STAGING_ROOT:-${UPDATE_STATE_DIR:-/var/lib/anyaicam-update}/installer-staging}"
    image_tag="$VMS_IMAGE:release-${VMS_RELEASE_COMMIT:0:12}"
    PREPARED_VMS_IMAGE_TAG=""

    if [[ ! "$build_timeout" =~ ^[1-9][0-9]*$ ]]; then
        echo "[ERROR] ANYAICAM_VMS_BUILD_TIMEOUT must be a positive number of seconds." >&2
        return 1
    fi
    if [[ -L "$staging_root" ]]; then
        echo "[ERROR] Installer staging directory must not be a symlink: $staging_root" >&2
        return 1
    fi
    if [[ "$(id -u)" == "0" ]]; then
        install -d -m 0700 -o root -g root "$staging_root"
    else
        install -d -m 0700 "$staging_root"
    fi || {
        echo "[ERROR] Could not create the secure installer staging directory." >&2
        return 1
    }
    if [[ ! -d "$staging_root" || -L "$staging_root" ]]; then
        echo "[ERROR] Installer staging directory is not a real directory: $staging_root" >&2
        return 1
    fi

    staging_dir="$(mktemp -d "$staging_root/vms.XXXXXXXX")" || {
        echo "[ERROR] Could not create a fresh VMS build staging directory." >&2
        return 1
    }
    chmod 0700 "$staging_dir" || { rm -rf -- "$staging_dir"; return 1; }
    if ! rsync -a --no-owner --no-group --delete \
        --exclude 'recordings/' --exclude 'data/config/' --exclude '.env' --exclude 'mediamtx/' \
        "$VMS_PAYLOAD_DIR/" "$staging_dir/"; then
        rm -rf -- "$staging_dir"
        echo "[ERROR] Could not stage the verified VMS release; the running VMS was not changed." >&2
        return 1
    fi

    if load_preloaded_vms_image "$staging_dir" "$staging_root" "$image_tag"; then
        rm -rf -- "$staging_dir" || {
            echo "[ERROR] Could not remove the temporary VMS build context: $staging_dir" >&2
            return 1
        }
        PREPARED_VMS_IMAGE_TAG="$image_tag"
        return 0
    fi

    log "Preparing the VMS image before downtime (timeout ${build_timeout}s, tag $image_tag) ..."
    if timeout --kill-after=30s "${build_timeout}s" docker build -t "$image_tag" "$staging_dir"; then
        build_status=0
    else
        build_status=$?
    fi
    rm -rf -- "$staging_dir" || {
        echo "[ERROR] Could not remove the temporary VMS build context: $staging_dir" >&2
        return 1
    }
    if [[ "$build_status" -ne 0 ]]; then
        if [[ "$build_status" -eq 124 || "$build_status" -eq 137 ]]; then
            echo "[ERROR] VMS image preparation timed out; the running VMS, installed files, and rollback point were not changed." >&2
        else
            echo "[ERROR] Building the VMS image failed (network/model download may be unavailable); the running VMS, installed files, and rollback point were not changed. Retry when network access is available." >&2
        fi
        return 1
    fi
    PREPARED_VMS_IMAGE_TAG="$image_tag"
}

previous_vms_commit() {
    sed -n 's/^ANYAICAM_VMS_COMMIT=//p' "$VMS_ENV_FILE" 2>/dev/null | tail -n 1
}

# The running release's product version ("" for releases from before
# product versions), so a rollback restores the version as well as the build.
previous_vms_version() {
    local version
    version="$(sed -n 's/^ANYAICAM_VERSION=//p' "$VMS_ENV_FILE" 2>/dev/null | tail -n 1)"
    [[ "$version" =~ ^[0-9]{1,4}(\.[0-9]{1,4}){1,3}$ ]] && printf '%s' "$version"
    return 0
}

# Installs rollback.sh as a fixed command (2026-10-04). An in-app update
# replaces only /opt/anyaicam, so the installer folder an operator still has
# may be older than the release running; this copy is always the one from
# the last installer run.
install_rollback_tool() {
    install -d -m 0755 "$(dirname "$ROLLBACK_TOOL")"
    install -m 0755 "$INSTALLER_DIR/rollback.sh" "$ROLLBACK_TOOL"
    if [[ "$(id -u)" == "0" ]]; then chown root:root "$ROLLBACK_TOOL"; fi
    log "Installed the rollback command at $ROLLBACK_TOOL."
}

# Prints the backup's path ("none" when there is no database yet). Online
# and consistent via sqlite3's backup API inside the running container
# (the same method used for every manual Ryzen backup), integrity-checked;
# a plain copy (with its WAL) only when the VMS is not running.
backup_vms_database() {
    local dest_name="$1" db="$VMS_RECORDINGS_DIR/$VMS_DATABASE_NAME"
    if [[ ! -f "$db" ]]; then
        echo "none"
        return 0
    fi
    if [[ "$(docker inspect -f '{{.State.Running}}' "$VMS_CONTAINER" 2>/dev/null)" == "true" ]]; then
        docker exec "$VMS_CONTAINER" python3 -c '
import sqlite3, sys
name = sys.argv[1]
src = sqlite3.connect("file:/app/recordings/'"$VMS_DATABASE_NAME"'?mode=ro", uri=True)
out = sqlite3.connect("/app/recordings/" + name)
src.backup(out); out.close(); src.close()
check = sqlite3.connect("file:/app/recordings/" + name + "?mode=ro", uri=True)
sys.exit(0 if check.execute("PRAGMA integrity_check").fetchone()[0] == "ok" else 3)
' "$dest_name" >&2 || return 1
    else
        cp -p "$db" "$VMS_RECORDINGS_DIR/$dest_name" || return 1
        if [[ -f "$db-wal" ]]; then
            cp -p "$db-wal" "$VMS_RECORDINGS_DIR/$dest_name-wal" || return 1
        fi
    fi
    [[ -s "$VMS_RECORDINGS_DIR/$dest_name" ]] || return 1
    echo "$VMS_RECORDINGS_DIR/$dest_name"
}

create_rollback_point() {
    local previous previous_version short stamp image_tag="" archive db_backup manifest root_parent root_name marker_copy="none"
    previous="$(previous_vms_commit)"
    previous_version="$(previous_vms_version)"
    [[ -n "$previous" ]] || previous="unknown"
    short="${previous:0:12}"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)" || return 1
    mkdir -p "$ROLLBACK_DIR" && chmod 0750 "$ROLLBACK_DIR" || return 1
    if [[ "$(id -u)" == "0" ]]; then chown root:root "$(dirname "$ROLLBACK_DIR")" "$ROLLBACK_DIR" || return 1; fi

    if docker image inspect "$VMS_IMAGE:latest" >/dev/null 2>&1; then
        image_tag="$VMS_IMAGE:rollback-$short"
        docker tag "$VMS_IMAGE:latest" "$image_tag" || return 1
    fi

    root_parent="$(dirname "$VMS_INSTALL_ROOT")"
    root_name="$(basename "$VMS_INSTALL_ROOT")"
    archive="$ROLLBACK_DIR/vms-code-$short-$stamp.tar.gz"
    if [[ -d "$VMS_INSTALL_ROOT" ]]; then
        tar -czf "$archive" -C "$root_parent" \
            --exclude="$root_name/recordings" --exclude="$root_name/data/config" --exclude="$root_name/.env" \
            --exclude="$root_name/mediamtx" --exclude="$root_name/app/static/hls" --exclude="$root_name/app/recordings" \
            --exclude="$root_name/app/auto.key" --exclude="$root_name/app/auto.crt" --exclude='__pycache__' \
            "$root_name" || return 1
        gzip -t "$archive" || return 1
        chmod 0640 "$archive" || return 1
    else
        archive="none"
    fi

    db_backup="$(backup_vms_database "partner_portal-pre-${VMS_RELEASE_COMMIT:0:12}-$stamp.db")" || return 1

    # The installed-release record as it is now, so a rollback restores it.
    if [[ -f "${VMS_RELEASE_MARKER:-}" && ! -L "${VMS_RELEASE_MARKER:-}" ]]; then
        marker_copy="$ROLLBACK_DIR/release-marker-$short-$stamp.json"
        cp "$VMS_RELEASE_MARKER" "$marker_copy" && chmod 0640 "$marker_copy" || return 1
    fi

    manifest="$ROLLBACK_DIR/rollback-$short-$stamp.env"
    {
        printf 'ROLLBACK_COMMIT=%s\n' "$previous"
        printf 'ROLLBACK_VERSION=%s\n' "$previous_version"
        printf 'ROLLBACK_MARKER=%s\n' "$marker_copy"
        printf 'ROLLBACK_IMAGE=%s\n' "${image_tag:-none}"
        printf 'ROLLBACK_CODE_ARCHIVE=%s\n' "$archive"
        printf 'ROLLBACK_DATABASE_BACKUP=%s\n' "$db_backup"
        printf 'ROLLBACK_CREATED_AT=%s\n' "$stamp"
        printf 'UPGRADE_TO_COMMIT=%s\n' "$VMS_RELEASE_COMMIT"
        printf 'CREATED_BY=installer\n'
    } > "$manifest" || return 1
    chmod 0640 "$manifest" || return 1
    cp -f "$manifest" "$ROLLBACK_DIR/latest.env" || return 1
    log "Rollback point for $previous: image ${image_tag:-none}, code $archive, database $db_backup (manifest $manifest)."
}

deploy_vms() {
    local state="$1" expected_image_tag

    [[ -d "$VMS_PAYLOAD_DIR/app" ]] || {
        echo "[ERROR] Missing built VMS payload: $VMS_PAYLOAD_DIR/app" >&2
        return 1
    }

    expected_image_tag="$VMS_IMAGE:release-${VMS_RELEASE_COMMIT:0:12}"
    if [[ "$PREPARED_VMS_IMAGE_TAG" != "$expected_image_tag" ]]; then
        prepare_vms_image || return $?
    fi

    migrate_legacy_persistent_data "$VMS_INSTALL_ROOT/recordings" "$VMS_RECORDINGS_DIR" "VMS recordings/application state"
    migrate_legacy_persistent_data "$VMS_INSTALL_ROOT/data/config" "$VMS_DATA_CONFIG_DIR" "VMS data/config"
    migrate_legacy_persistent_file "$VMS_INSTALL_ROOT/.env" "$VMS_ENV_FILE" "VMS environment config"

    if [[ "$state" != "clean" ]]; then
        if [[ "${ANYAICAM_SKIP_ROLLBACK_POINT:-}" == "1" ]]; then
            log "WARNING: ANYAICAM_SKIP_ROLLBACK_POINT=1 -- replacing the installed VMS WITHOUT a rollback point."
        elif ! create_rollback_point; then
            echo "[ERROR] Could not create a rollback point for the installed VMS; nothing was changed. Fix the error above, or set ANYAICAM_SKIP_ROLLBACK_POINT=1 to replace it without one." >&2
            return 1
        fi
    fi

    install -d -m 0755 -o root -g root "$VMS_INSTALL_ROOT" || return 1

    log "Installing exact VMS release $VMS_RELEASE_COMMIT into $VMS_INSTALL_ROOT ..."
    # --delete makes /opt/anyaicam an exact software mirror of the release.
    # Legacy customer state locations are excluded defensively; current state
    # lives under /var/lib or /etc and is never part of this mirror.
    #
    # 'mediamtx/' is excluded for the same reason (2026-09-17, real bug
    # confirmed live on Ryzen): 10-install-mediamtx.sh places the real
    # MediaMTX binary at $VMS_INSTALL_ROOT/mediamtx, and its own
    # docstring promises an ordinary VMS-only release rebuild (one built
    # without --mediamtx-binary, which never embeds a payload/mediamtx/
    # directory at all) is a complete no-op for that binary -- "changes
    # nothing about live camera behavior". That promise was false: this
    # rsync runs BEFORE install_mediamtx() in install.sh's own pipeline
    # and, with --delete and no exclusion for it, silently deleted the
    # previously-installed, already-validated MediaMTX binary out from
    # under a P2P-enabled appliance the moment any unrelated VMS-only
    # repair (e.g. this session's own LPR/PPE fix) ran, breaking P2P
    # live view with no error anywhere in the install output.
    #
    # --no-owner/--no-group (2026-09-24, real defect confirmed live on
    # Ryzen): plain -a copied the PAYLOAD's own owner/group, and the
    # payload is unpacked by whichever login user extracted the release
    # tarball (uid 1000) -- so a root-run repair left /opt/anyaicam and
    # every VMS source file owned by that user, writable without sudo.
    # normalize_vms_install_ownership() also repairs files an earlier
    # install already left with the wrong owner (rsync skips unchanged
    # files, so --no-owner alone would never fix those).
    relocate_stale_install_dirs || return 1
    # The VMS container bind-mounts $VMS_INSTALL_ROOT/app as its /app, so it
    # runs whatever is in that tree. A repair must not rewrite the tree
    # under a running VMS (it could load half-old, half-new modules): stop
    # it first; systemd_setup restarts it once the new release is in place.
    # (Software Update does not use this path: it swaps whole directories.)
    if [[ "$state" != "clean" ]] && systemctl is-active --quiet anyaicam-vms.service 2>/dev/null; then
        log "Stopping the VMS before replacing its bind-mounted application tree ..."
        systemctl stop anyaicam-vms.service || return 1
    fi
    rsync -a --no-owner --no-group --delete \
        --exclude 'recordings/' --exclude 'data/config/' --exclude '.env' --exclude 'mediamtx/' \
        "$VMS_PAYLOAD_DIR/" "$VMS_INSTALL_ROOT/" || {
            echo "[ERROR] Could not replace the installed VMS files with the verified release payload." >&2
            return 1
        }
    normalize_vms_install_ownership || return 1

    ensure_vms_env

    log "Activating the prepared VMS image $PREPARED_VMS_IMAGE_TAG ..."
    docker tag "$PREPARED_VMS_IMAGE_TAG" "$VMS_IMAGE:latest" || {
        echo "[ERROR] Could not activate the prepared VMS image." >&2
        return 1
    }
    PREPARED_VMS_IMAGE_TAG=""

    if [[ "$state" == "clean" ]]; then
        log "VMS deployed from exact release payload (clean install)."
    else
        log "VMS replaced with exact release payload (existing install); persistent state untouched."
    fi
}
