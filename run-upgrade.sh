#!/bin/bash
# One-shot upgrade run for a single switch, with Ansible-style TASK/PLAY
# RECAP output. Decrypts credentials once, host-bound via systemd-creds
# (same pattern as run-backup.sh) so nothing plaintext ever touches disk,
# then chains:
#   decrypt -> backup -> pre-snapshot -> check -> stage -> upgrade -> verify
#   -> post-snapshot -> diff
# Each task must succeed before the next runs (upgrade.py/backup_config.py
# exit non-zero on any failed host, including a failed enable mode), so a
# bad backup/check/stage/credential-decrypt stops the run before it ever
# reaches the reload step.
set -uo pipefail
cd "$(dirname "$0")"

HOST="${1:?usage: run-upgrade.sh <inventory-host-name>}"

LOG_DIR=logs
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/upgrade-${HOST}-$(date +%Y%m%d-%H%M%S).log"

CRED_DIR=creds
dec() { sudo systemd-creds decrypt --name="$1" "$CRED_DIR/$1.cred" -; }

(
    set -uo pipefail
    BOLD='\033[1m'; CYAN='\033[36m'; GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'; RESET='\033[0m'
    OK_COUNT=0; CHANGED_COUNT=0; FAILED_COUNT=0

    stars() { printf '%*s' "$1" '' | tr ' ' '*'; }

    banner() {
        local prefix="TASK [$1] "
        local width=80
        local n=$(( width - ${#prefix} ))
        (( n < 3 )) && n=3
        printf "\n${CYAN}${BOLD}%s%s${RESET}\n" "$prefix" "$(stars "$n")"
    }

    run_task() {
        local title="$1" kind="$2"; shift 2
        banner "$title"
        if "$@"; then
            if [ "$kind" = changed ]; then
                CHANGED_COUNT=$((CHANGED_COUNT + 1))
                printf "${YELLOW}changed: [%s]${RESET}\n" "$HOST"
            else
                OK_COUNT=$((OK_COUNT + 1))
                printf "${GREEN}ok: [%s]${RESET}\n" "$HOST"
            fi
            return 0
        else
            local rc=$?
            FAILED_COUNT=$((FAILED_COUNT + 1))
            printf "${RED}fatal: [%s]: FAILED! => task '%s' exited %s${RESET}\n" "$HOST" "$title" "$rc"
            return "$rc"
        fi
    }

    decrypt_creds() {
        if NET_USER=$(dec net_user) && NET_PASS=$(dec net_pass) && NET_ENABLE=$(dec net_enable); then
            export NET_USER NET_PASS NET_ENABLE
            return 0
        fi
        return 1
    }

    maybe_check() {
        if [ -n "${SKIP_CHECK:-}" ]; then
            echo "SKIP_CHECK set — skipping the free-space/boot-mode gate."
            echo "(upgrade_install_mode doesn't call check_free_space itself — only stage/upgrade run now, and Cisco's own 'install add' does its own space validation.)"
            return 0
        fi
        "$PY" upgrade.py check --host "$HOST"
    }

    PY=./venv/bin/python3

    printf "${BOLD}PLAY [%s upgrade — %s]${RESET} %s\n" "$HOST" "$(date -Is)" "$(stars 30)"
    if [ -n "${SKIP_CHECK:-}" ]; then
        printf "${YELLOW}NOTE: SKIP_CHECK is set — the free-space/boot-mode check will be bypassed this run.${RESET}\n"
    fi

    run_task "Decrypt credentials"           ok      decrypt_creds &&
    run_task "Backup running-config"         changed "$PY" backup_config.py    --host "$HOST" &&
    run_task "Capture pre-upgrade snapshot"  ok      "$PY" snapshot.py capture --host "$HOST" --label pre &&
    run_task "Check boot mode / free space"  ok      maybe_check &&
    run_task "Stage image to flash"          changed "$PY" upgrade.py stage    --host "$HOST" &&
    run_task "Upgrade and reload"            changed "$PY" upgrade.py upgrade  --host "$HOST" &&
    run_task "Verify post-upgrade version"   ok      "$PY" upgrade.py verify   --host "$HOST" --wait 300 &&
    run_task "Capture post-upgrade snapshot" ok      "$PY" snapshot.py capture --host "$HOST" --label post &&
    run_task "Diff pre/post state"           ok      "$PY" snapshot.py diff    --host "$HOST"
    PHASE_STATUS=$?

    printf "\n${BOLD}PLAY RECAP${RESET} %s\n" "$(stars 62)"
    if [ "$PHASE_STATUS" -eq 0 ]; then
        printf "%-20s : ${GREEN}ok=%-3s${RESET} ${YELLOW}changed=%-3s${RESET} failed=%-3s\n" \
            "$HOST" "$OK_COUNT" "$CHANGED_COUNT" "$FAILED_COUNT"
    else
        printf "%-20s : ok=%-3s changed=%-3s ${RED}failed=%-3s${RESET}\n" \
            "$HOST" "$OK_COUNT" "$CHANGED_COUNT" "$FAILED_COUNT"
    fi
    exit "$PHASE_STATUS"
) 2>&1 | tee -a "$LOG_FILE"
STATUS=${PIPESTATUS[0]}

echo "Upgrade run for $HOST finished (exit $STATUS) — see $LOG_FILE"
exit "$STATUS"
