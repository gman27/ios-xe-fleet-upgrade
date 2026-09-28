#!/bin/bash
# Decrypt the switch credentials (host-bound via systemd-creds) and run the
# running-config backup. Args are passed through to backup_config.py.
set -euo pipefail
cd "$(dirname "$0")"

CRED_DIR=creds
dec() { sudo systemd-creds decrypt --name="$1" "$CRED_DIR/$1.cred" -; }

export NET_USER=$(dec net_user)
export NET_PASS=$(dec net_pass)
export NET_ENABLE=$(dec net_enable)

exec ./venv/bin/python backup_config.py "$@"
