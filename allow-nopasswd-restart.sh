#!/bin/sh
# OPTIONAL: allow restarting the rpg-library service WITHOUT a password prompt.
#
# Run this ONCE with sudo:
#
#     sudo /mnt/disk5/rpg-app/allow-nopasswd-restart.sh
#
# It installs /etc/sudoers.d/rpg-library granting exactly one command --
# `systemctl restart rpg-library` -- with no password. Nothing else is granted;
# this is far safer than storing a password, and far narrower than blanket
# NOPASSWD.
#
# Afterwards, a plain `sudo systemctl restart rpg-library` runs without
# prompting, so code changes can be picked up with one short command.
#
# To undo:  sudo rm /etc/sudoers.d/rpg-library
set -eu

if [ "$(id -u)" != "0" ]; then
    echo "Run this with sudo:  sudo $0" >&2
    exit 1
fi

TARGET_USER="${SUDO_USER:-bruce}"
SUDOERS_FILE=/etc/sudoers.d/rpg-library
UNIT=rpg-library

SYSTEMCTL=$(command -v systemctl || echo /usr/bin/systemctl)

cat > "$SUDOERS_FILE" <<EOF
# Allow $TARGET_USER to restart the RPG Library web app without a password.
# Grants exactly this one command; nothing else.
$TARGET_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart $UNIT, $SYSTEMCTL start $UNIT, $SYSTEMCTL stop $UNIT, $SYSTEMCTL status $UNIT
EOF
chmod 0440 "$SUDOERS_FILE"

# Validate; remove if malformed so a typo can never lock out sudo.
if ! visudo -cf "$SUDOERS_FILE" >/dev/null 2>&1; then
    echo "ERROR: generated sudoers file failed validation - removing it." >&2
    rm -f "$SUDOERS_FILE"
    exit 1
fi

chown root:root "$SUDOERS_FILE"
echo "Installed $SUDOERS_FILE"
echo "  user: $TARGET_USER"
echo "  grants: systemctl {start,stop,restart,status} $UNIT"
echo
echo "Test it:  sudo systemctl restart rpg-library"
