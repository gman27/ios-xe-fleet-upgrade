#!/bin/bash
# Post-upgrade verification: confirm target version, capture a post-upgrade
# snapshot, and diff it against the most recent pre-upgrade one. Entirely
# read-only against the switch - no config changes, no reload. Safe to
# (re)run any time after an upgrade.
set -uo pipefail
cd "$(dirname "$0")"

HOST="${1:?usage: post-verify.sh <inventory-host-name>}"

CRED_DIR=creds
dec() { sudo systemd-creds decrypt --name="$1" "$CRED_DIR/$1.cred" -; }

if ! NET_USER=$(dec net_user) || ! NET_PASS=$(dec net_pass) || ! NET_ENABLE=$(dec net_enable); then
    echo "credential decrypt failed" >&2
    exit 1
fi
export NET_USER NET_PASS NET_ENABLE

PY=./venv/bin/python3

echo "--- verify version ---"
"$PY" upgrade.py verify --host "$HOST" --wait 0

echo "--- post-upgrade snapshot ---"
"$PY" snapshot.py capture --host "$HOST" --label post

echo "--- diff pre vs post ---"
mkdir -p reports
REPORT="reports/${HOST}-diff-$(date +%Y%m%d_%H%M%S).html"
"$PY" snapshot.py diff --host "$HOST" --html "$REPORT"
echo "HTML report: $(pwd)/$REPORT"

# View differences by browsing to http://<hostname>:8000
# Serves only the reports/ directory. Press Ctrl+C to stop.
echo "--- serving reports ---"
echo "Browse to http://$(hostname):8000/$(basename "$REPORT")  (Ctrl+C to stop)"
cd reports && exec "$OLDPWD/$PY" -m http.server 8000
