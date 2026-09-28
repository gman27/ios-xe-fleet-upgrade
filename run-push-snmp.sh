#!/bin/bash
# Decrypt the switch credentials (host-bound via systemd-creds) and push the
# SNMPv3 read-only config to all 13 switches. Args are passed through to
# push_snmp_config.py (e.g. --host BRANCH-WEST, --no-save).
set -euo pipefail
cd "$(dirname "$0")"

CRED_DIR=creds
dec() { sudo systemd-creds decrypt --name="$1" "$CRED_DIR/$1.cred" -; }

export NET_USER=$(dec net_user)
export NET_PASS=$(dec net_pass)
export NET_ENABLE=$(dec net_enable)

# SNMPv3 auth/priv passwords — prefer the systemd-creds vault
# (creds/snmp_auth_pass.cred, creds/snmp_priv_pass.cred); fall back to
# already-exported env vars if those files don't exist yet.
if [[ -f "$CRED_DIR/snmp_auth_pass.cred" ]]; then
    export SNMP_AUTH_PASS=$(dec snmp_auth_pass)
else
    : "${SNMP_AUTH_PASS:?Set SNMP_AUTH_PASS in your shell, or add creds/snmp_auth_pass.cred}"
fi
if [[ -f "$CRED_DIR/snmp_priv_pass.cred" ]]; then
    export SNMP_PRIV_PASS=$(dec snmp_priv_pass)
else
    : "${SNMP_PRIV_PASS:?Set SNMP_PRIV_PASS in your shell, or add creds/snmp_priv_pass.cred}"
fi

exec ./venv/bin/python push_snmp_config.py "$@"
