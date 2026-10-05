#!/bin/bash
# One-shot upgrade run for a single switch, condensed display:
#
#   [2/9] Backup running-config .......... changed (14s)  812 lines saved
#   [5/9] Stage image to flash ........... 63% (316/502 MB)  04:12
#
# Stages:
#   decrypt -> backup -> pre-snapshot -> check -> stage -> confirm -> upgrade
#   -> verify -> post-snapshot -> diff
# The confirm stage is an interactive gate right before the reload: type the
# hostname back to proceed. It reads from the controlling terminal, not stdin,
# so piped input can't satisfy it. AUTO_APPROVE_RELOAD=1 skips it for runs a
# human already approved out of band.
# Each stage's full output goes to the log file only; if a stage fails, its
# full output is printed on screen under the FAILED line. VERBOSE=1 shows
# everything live instead.
set -uo pipefail
cd "$(dirname "$0")"

HOST="${1:?usage: run-upgrade.sh <inventory-host-name>}"

LOG_DIR=logs
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/upgrade-${HOST}-$(date +%Y%m%d-%H%M%S).log"
REPORT_DIR=reports
REPORT="$REPORT_DIR/${HOST}-diff-$(date +%Y%m%d_%H%M%S).html"
mkdir -p "$REPORT_DIR"

CRED_DIR=creds
dec() { sudo systemd-creds decrypt --name="$1" "$CRED_DIR/$1.cred" -; }

PY=./venv/bin/python3
BOLD='\033[1m'; CYAN='\033[36m'; GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'; RESET='\033[0m'
OK_COUNT=0; CHANGED_COUNT=0; FAILED_COUNT=0
STEP=0; TOTAL=10

stars() { printf '%*s' "$1" '' | tr ' ' '*'; }
# Print to the screen and append to the log.
say() { printf "$@" | tee -a "$LOG_FILE"; }

label() { printf '[%d/%d] %s' "$STEP" "$TOTAL" "$1"; }
dots()  { local l; l="$(label "$1") "; printf '%s' "$l"; printf '%*s' $(( 38 - ${#l} )) '' | tr ' ' '.'; printf ' '; }

count() {
    case "$1" in
        0) [ "$2" = changed ] && CHANGED_COUNT=$((CHANGED_COUNT + 1)) || OK_COUNT=$((OK_COUNT + 1)) ;;
        *) FAILED_COUNT=$((FAILED_COUNT + 1)) ;;
    esac
}

run_task() {
    local title="$1" kind="$2"; shift 2
    STEP=$((STEP + 1))
    local rc
    if [ -n "${VERBOSE:-}" ]; then
        say "\n${CYAN}${BOLD}TASK [%s]${RESET}\n" "$(label "$title")"
        "$@" 2>&1 | tee -i -a "$LOG_FILE"
        rc=${PIPESTATUS[0]}
        if [ "$rc" -eq 0 ]; then say "%s\n" "$kind: [$HOST]"; else say "${RED}fatal: [%s]: task '%s' exited %s${RESET}\n" "$HOST" "$title" "$rc"; fi
    else
        "$PY" taskui.py --label "$(label "$title")" --kind "$kind" --log "$LOG_FILE" -- "$@"
        rc=$?
    fi
    count "$rc" "$kind"
    return "$rc"
}

decrypt_creds() {
    STEP=$((STEP + 1))
    local t0=$SECONDS
    # sudo may prompt for a password here, so the stage line prints after.
    if NET_USER=$(dec net_user) && NET_PASS=$(dec net_pass) && NET_ENABLE=$(dec net_enable); then
        export NET_USER NET_PASS NET_ENABLE
        say "%s${GREEN}ok${RESET} (%ss)\n" "$(dots "Decrypt credentials")" $((SECONDS - t0))
        count 0 ok
        return 0
    fi
    say "%s${RED}${BOLD}FAILED${RESET}\n" "$(dots "Decrypt credentials")"
    count 1 ok
    return 1
}

maybe_check() {
    if [ -n "${SKIP_CHECK:-}" ]; then
        STEP=$((STEP + 1))
        say "%s${YELLOW}skipped${RESET} (SKIP_CHECK set; install add does its own space check)\n" "$(dots "Check boot mode / free space")"
        count 0 ok
        return 0
    fi
    run_task "Check boot mode / free space" ok "$PY" upgrade.py check --host "$HOST"
}

# Interactive human-approval gate, right before the irreversible step.
# Reads from /dev/tty explicitly (not stdin) and fails closed if no terminal
# is attached. read's own exit status is the only reliable "is there a
# terminal" signal: `-r /dev/tty` stays true under setsid/cron.
confirm_reload() {
    STEP=$((STEP + 1))
    if [ -n "${AUTO_APPROVE_RELOAD:-}" ]; then
        say "%s${YELLOW}skipped${RESET} (AUTO_APPROVE_RELOAD set)\n" "$(dots "Confirm reload")"
        count 0 ok
        return 0
    fi
    say "%s\n" "$(dots "Confirm reload")"
    say "${BOLD}${YELLOW}About to run install add/activate/commit and reload %s now. This is the irreversible step.${RESET}\n" "$HOST"
    local reply=""
    # Prompt printed separately: read -p writes it to stderr, which the
    # 2>/dev/null below (there to hide a failed /dev/tty open) would swallow.
    printf "Type the hostname exactly (%s) to proceed, anything else aborts: " "$HOST"
    if ! read -r reply < /dev/tty 2>/dev/null; then
        echo
        say "${RED}Couldn't read a confirmation from a terminal, and AUTO_APPROVE_RELOAD is not set. Aborting.${RESET}\n"
        count 1 ok
        return 1
    fi
    if [ "$reply" != "$HOST" ]; then
        say "${RED}Confirmation did not match, aborting before reload.${RESET}\n"
        count 1 ok
        return 1
    fi
    say "%s${GREEN}ok${RESET} (approved)\n" "$(dots "Confirm reload")"
    count 0 ok
    return 0
}

say "${BOLD}PLAY [%s upgrade - %s]${RESET} %s\n" "$HOST" "$(date '+%Y-%m-%d %H:%M')" "$(stars 30)"
say "log: %s\n" "$LOG_FILE"

decrypt_creds &&
run_task "Backup running-config"         changed "$PY" backup_config.py    --host "$HOST" &&
run_task "Pre-upgrade snapshot"          ok      "$PY" snapshot.py capture --host "$HOST" --label pre &&
maybe_check &&
run_task "Stage image to flash"          changed "$PY" upgrade.py stage    --host "$HOST" &&
confirm_reload &&
run_task "Upgrade and reload"            changed "$PY" upgrade.py upgrade  --host "$HOST" &&
run_task "Verify version"                ok      "$PY" upgrade.py verify   --host "$HOST" &&
run_task "Post-upgrade snapshot"         ok      "$PY" snapshot.py capture --host "$HOST" --label post &&
run_task "Diff pre/post state"           ok      "$PY" snapshot.py diff    --host "$HOST" --html "$REPORT"
STATUS=$?

say "\n${BOLD}PLAY RECAP${RESET} %s\n" "$(stars 40)"
if [ "$STATUS" -eq 0 ]; then
    say "%-14s : ${GREEN}ok=%-3s${RESET} ${YELLOW}changed=%-3s${RESET} failed=%-3s\n" "$HOST" "$OK_COUNT" "$CHANGED_COUNT" "$FAILED_COUNT"
else
    say "%-14s : ok=%-3s changed=%-3s ${RED}failed=%-3s${RESET}\n" "$HOST" "$OK_COUNT" "$CHANGED_COUNT" "$FAILED_COUNT"
fi
say "full log: %s\n" "$LOG_FILE"

# View differences by browsing to http://<server IP>:8000 (the hostname only
# resolves on this box itself, via /etc/hosts)
# Serves only the reports/ directory until Ctrl+C. Set NO_SERVE=1 to skip.
if [ "$STATUS" -eq 0 ] && [ -z "${NO_SERVE:-}" ] && [ -f "$REPORT" ]; then
    echo "Browse to http://$(hostname -I | awk '{print $1}'):8000/$(basename "$REPORT")  (Ctrl+C to stop)"
    cd "$REPORT_DIR" && exec "$OLDPWD/venv/bin/python3" -m http.server 8000
fi
exit "$STATUS"
