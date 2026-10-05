#!/usr/bin/env python3
"""
Run one stage of run-upgrade.sh and show it as a single updating line:

    [5/9] Stage image to flash ........... 63% (316/502 MB)  04:12

The stage's full output (print_result dumps, switch output, tracebacks) goes
to the log file, never the screen, unless the stage fails: then all of it is
printed under the FAILED line so nothing is hidden.

    taskui.py --label "[2/9] Backup running-config" --kind changed --log LOG -- cmd args...
"""
import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time

GREEN, YELLOW, RED, BOLD, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"
LABEL_WIDTH = 38
# nornir_utils turns on colorama's autoreset, which wraps every print in
# ESC[0m, so marker lines arrive as "\x1b[0m@@PROGRESS ...\x1b[0m".
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def fmt_elapsed(sec: float, clock: bool = False) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if clock:
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s" if m else f"{s}s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--kind", choices=["ok", "changed"], default="ok")
    ap.add_argument("--log", required=True)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd

    # Ctrl+C reaches the child too (same process group). Let the child deal
    # with it and print its own message; we just report how it ended.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    tty = sys.stdout.isatty()
    head = f"{args.label} ".ljust(LABEL_WIDTH, ".") + " "
    log = open(args.log, "a", buffering=1, errors="replace")
    log.write(f"\n===== {args.label} =====\n$ {' '.join(cmd)}\n")

    state = {"status": "", "note": "", "output": []}
    lock = threading.Lock()

    env = dict(os.environ, UPGRADE_PROGRESS="1", PYTHONUNBUFFERED="1")
    child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                             preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))

    def reader():
        for raw in child.stdout:
            line = raw.decode(errors="replace").rstrip("\r\n")
            plain = ANSI_RE.sub("", line)
            with lock:
                if plain.startswith("@@PROGRESS "):
                    state["status"] = plain[len("@@PROGRESS "):]
                    log.write(f"[progress] {state['status']}\n")
                elif plain.startswith("@@NOTE "):
                    state["note"] = plain[len("@@NOTE "):]
                    log.write(f"[note] {state['note']}\n")
                else:
                    state["output"].append(line)
                    log.write(line + "\n")

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    start = time.monotonic()
    while t.is_alive():
        if tty:
            with lock:
                status = state["status"]
            elapsed = fmt_elapsed(time.monotonic() - start, clock=True)
            line = f"{head}{status}  {elapsed}" if status else f"{head}{elapsed}"
            sys.stdout.write(f"\r{line}\033[K")
            sys.stdout.flush()
        t.join(timeout=1)
    rc = child.wait()
    took = fmt_elapsed(time.monotonic() - start)

    note = f"  {state['note']}" if state["note"] else ""
    if rc == 0:
        colour = YELLOW if args.kind == "changed" else GREEN
        final = f"{head}{colour}{args.kind}{RESET} ({took}){note}"
    else:
        why = "interrupted" if rc in (130, -signal.SIGINT) else f"exit {rc}"
        final = f"{head}{RED}{BOLD}FAILED{RESET}{RED} ({why}, {took}){RESET}{note}"
    sys.stdout.write(("\r" if tty else "") + final + ("\033[K" if tty else "") + "\n")
    log.write(final + "\n")

    if rc != 0:
        print(f"{RED}----- full output of this stage -----{RESET}")
        print("\n".join(state["output"]))
        print(f"{RED}----- end of output -----{RESET}")
    sys.stdout.flush()
    log.close()
    sys.exit(rc if rc >= 0 else 128 - rc)


if __name__ == "__main__":
    main()
