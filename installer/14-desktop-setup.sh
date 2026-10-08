#!/usr/bin/env bash
# Zero-terminal onboarding (2026-10-08): the "AnyAiCam Setup" desktop entry.
#
# A customer finishes setup in a browser, never a terminal. The appliance
# agent serves its own setup page on 127.0.0.1:8790 only
# (appliance-agent/anyaicam_agent/link_server.py): "Link this appliance"
# sends the browser to the AnyAiCam cloud, where the customer signs in and
# confirms -- no Cloud ID, activation token or claim code to see or type.
#
#   /usr/local/bin/anyaicam-setup-page        opens that page in the desktop
#                                             user's browser (--autostart: only
#                                             while the appliance is not linked)
#   /etc/xdg/autostart/anyaicam-setup.desktop runs it at every desktop sign-in
#   /usr/share/applications/anyaicam-setup.desktop  "AnyAiCam Setup" in the
#                                             applications menu
# Root writes only these three fixed system paths (never into a user's home
# or an agent-owned folder) and refuses to write through a symlink.

DESKTOP_SETUP_LAUNCHER="${DESKTOP_SETUP_LAUNCHER:-/usr/local/bin/anyaicam-setup-page}"
DESKTOP_SETUP_AUTOSTART="${DESKTOP_SETUP_AUTOSTART:-/etc/xdg/autostart/anyaicam-setup.desktop}"
DESKTOP_SETUP_MENU="${DESKTOP_SETUP_MENU:-/usr/share/applications/anyaicam-setup.desktop}"
DESKTOP_SETUP_URL="${DESKTOP_SETUP_URL:-http://127.0.0.1:8790/}"

desktop_setup_write() {  # path mode < content
    local path="$1" mode="$2" tmp
    if [[ -L "$path" ]]; then
        echo "[ERROR] $path is a symbolic link; refusing to write through it." >&2
        return 1
    fi
    mkdir -p "$(dirname "$path")"
    tmp="$(mktemp "$(dirname "$path")/.anyaicam-setup.XXXXXX")"
    cat > "$tmp"
    chmod "$mode" "$tmp"
    [[ $EUID -eq 0 ]] && chown root:root "$tmp"
    mv -f "$tmp" "$path"
}

desktop_setup_provision() {
    desktop_setup_write "$DESKTOP_SETUP_LAUNCHER" 0755 <<EOF || return 1
#!/bin/sh
# Opens this appliance's AnyAiCam Setup page (served by the AnyAiCam agent on
# 127.0.0.1 only). --autostart: at desktop sign-in, only while the appliance is
# not yet linked to an AnyAiCam account. Installed by the AnyAiCam installer.
URL='$DESKTOP_SETUP_URL'
if [ "\${1:-}" = "--autostart" ]; then
    # The agent may still be starting after a reboot: ask it for up to ~2 minutes.
    i=0
    while [ \$i -lt 24 ]; do
        state=\$(python3 -c 'import sys,urllib.request;print(urllib.request.urlopen(sys.argv[1]+"status",timeout=3).read().decode())' "\$URL" 2>/dev/null) && break
        i=\$((i+1)); sleep 5
    done
    case "\$state" in
        *'"linked": true'*) exit 0 ;;
        *'"linked": false'*) ;;
        *) exit 0 ;;
    esac
fi
exec xdg-open "\$URL"
EOF
    desktop_setup_write "$DESKTOP_SETUP_AUTOSTART" 0644 <<EOF || return 1
[Desktop Entry]
Type=Application
Name=AnyAiCam Setup
Comment=Link this appliance to your AnyAiCam account
Exec=$DESKTOP_SETUP_LAUNCHER --autostart
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOF
    desktop_setup_write "$DESKTOP_SETUP_MENU" 0644 <<EOF || return 1
[Desktop Entry]
Type=Application
Name=AnyAiCam Setup
Comment=Link this appliance to your AnyAiCam account
Exec=$DESKTOP_SETUP_LAUNCHER
Icon=preferences-system
Terminal=false
Categories=Settings;
EOF
    log "AnyAiCam Setup desktop entry installed (opens $DESKTOP_SETUP_URL at sign-in until the appliance is linked)."
}

desktop_setup_remove() {
    local path
    for path in "$DESKTOP_SETUP_LAUNCHER" "$DESKTOP_SETUP_AUTOSTART" "$DESKTOP_SETUP_MENU"; do
        [[ -f "$path" && ! -L "$path" ]] && rm -f -- "$path"
    done
    return 0
}
