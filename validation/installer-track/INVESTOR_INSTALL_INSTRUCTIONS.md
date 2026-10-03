# Ubuntu investor candidate — unpublished, preliminary validation only

Installer 1.1.1, VMS and installer source commit:
a1c81e0f492633a80198503e721fc6c277e6a8b3.
Derived from immutable base d5af9c53cbef1f2c400f56e148c5cb080770dc07.
Exact proposed download artifact (not uploaded anywhere):
anyaicam-appliance-installer-1.1.1-vms-a1c81e0f4926.tar.gz
SHA-256: 9dd0fe3fef6b46752a74ea49b4426cf9030313b926b1795a2e50c68bc48a53ac
Size: 36,467,580 bytes. Includes verified MediaMTX v1.21.0.

This is an Ubuntu archive, not a Windows executable. Use only a disposable,
clean Ubuntu 24.04 x86_64 desktop for the next acceptance test. Allow at least
4 CPUs, 8 GB usable RAM and 100 GiB actual free root disk space. Internet or a
validated dependency cache is still required: this is not an offline installer.
The minimum recommended VM disk is 180 GB to leave room after OS installation.

## Customer steps for the future isolated test

1. Download AnyAiCam from the approved isolated portal owner account.
2. Extract the archive into a new folder. Start setup.sh as your normal desktop
   user; follow the installer prompts and approve the Ubuntu administrator prompt.
3. Connect your system: open the browser connection page, sign in, choose your
   location, review the device, and confirm the connection.

Account linking requires explicit authenticated owner confirmation. It does not
silently link merely because the customer downloaded the archive. If the browser
handoff fails, keep the launcher open, enter its displayed setup code on the
signed-in portal's Connect system page, choose the location and confirm.
Never send setup codes or device possession credentials to another person.

For the lab technician, from the extracted directory (future VM only):

    sha256sum ../anyaicam-appliance-installer-1.1.1-vms-a1c81e0f4926.tar.gz
    ./setup.sh --portal-url https://installer-test.test --product-mode local

Compare the checksum exactly. The launcher verifies the embedded package before
requesting privileges. Do not run setup.sh as root; that prevents the desktop
browser from using the customer's session. The guest must trust the isolated
portal certificate and resolve installer-test.test; do not disable TLS checks.

The Ubuntu file-manager execution experience and complete installation remain
unproven on a clean machine. Do not distribute this as a validated investor build.
No Windows EXE, native Windows runtime, signing or clean-machine proof is claimed.
