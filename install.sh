#!/bin/sh
# Install or update lg-airco-logger on a Raspberry Pi (or any systemd Linux).
# Run from a checkout: sudo ./install.sh
#
# It creates the user lg-airco, /opt/lg-airco-logger, /var/lib/lg-airco-logger
# and /etc/lg-airco-logger, and installs the systemd units. It never creates
# or changes the secrets: put those in place yourself (see README.md):
#   /etc/lg-airco-logger/token        the ThinQ Connect personal access token
#   /etc/lg-airco-logger/rclone.conf  the rclone remote for the NAS
#   /etc/lg-airco-logger/sync.env     REMOTE=<remote>:<path>
set -eu
cd "$(dirname "$0")"

id lg-airco >/dev/null 2>&1 || useradd --system --home-dir /var/lib/lg-airco-logger --shell /usr/sbin/nologin lg-airco
install -d -m 755 /opt/lg-airco-logger
install -m 644 lg-airco-logger.py /opt/lg-airco-logger/
install -d -m 750 -o lg-airco -g lg-airco /var/lib/lg-airco-logger
install -d -m 750 -o root -g lg-airco /etc/lg-airco-logger
for f in systemd/*.service systemd/*.timer; do
    install -m 644 "$f" /etc/systemd/system/
done
systemctl daemon-reload

missing=""
for f in token rclone.conf sync.env; do
    [ -f "/etc/lg-airco-logger/$f" ] || missing="$missing $f"
done
if [ -n "$missing" ]; then
    echo "Installed. Still missing in /etc/lg-airco-logger:$missing (see README.md); then:"
else
    echo "Installed. Start (or restart after an update) with:"
fi
echo "  sudo systemctl enable --now lg-airco-logger.service lg-airco-sync.timer"
echo "  sudo systemctl restart lg-airco-logger.service    # after an update"
