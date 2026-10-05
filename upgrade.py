#!/usr/bin/env python3
"""
Cisco IOS-XE switch upgrade via Nornir.

Run in phases (safer than one big auto-pilot run against 10 production switches):

    python3 upgrade.py check                 # detect install vs bundle mode, free space
    python3 upgrade.py stage                 # copy image to flash, verify MD5
    python3 upgrade.py upgrade --host sw01    # upgrade one switch, then check it before continuing
    python3 upgrade.py verify                 # confirm post-upgrade version

Credentials come from env vars, never from inventory files:
    export NET_USER=admin
    export NET_PASS='...'
    export NET_ENABLE='...'   # enable/privileged-mode secret, separate from the login password
"""
import argparse
import os
import logging
import re
import socket
import subprocess
import sys
import time

import warnings

# nornir's __init__ imports pkg_resources, which newer setuptools flags as
# deprecated. Harmless noise; filter only that exact message.
warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API")

# paramiko has no handler of its own, so Python dumps its connection
# tracebacks straight to the terminal. Send them to nornir.log instead;
# real failures still show in full via print_result().
_paramiko_log = logging.getLogger("paramiko")
_paramiko_log.addHandler(logging.FileHandler(os.path.join(os.path.dirname(os.path.abspath(__file__)), "nornir.log")))
_paramiko_log.propagate = False

from nornir import InitNornir
from nornir.core.filter import F
from nornir.core.task import Result, Task
from nornir_netmiko.tasks import netmiko_send_command, netmiko_send_config, netmiko_file_transfer
from nornir_utils.plugins.functions import print_result

import creds
from progress import note, progress
import progress as _progress

BOOT_MODE_RE = re.compile(r'System image file is "flash:(?P<file>\S+)"')


def load_inventory(num_workers=1):
    nr = InitNornir(config_file="config.yaml")
    nr.config.runner.options["num_workers"] = num_workers

    # env vars win if set (handy for CI / non-interactive runs); otherwise fall
    # back to the encrypted vault, prompting once for its passphrase.
    user = os.environ.get("NET_USER")
    pwd = os.environ.get("NET_PASS")
    enable_secret = os.environ.get("NET_ENABLE")
    if not (user and pwd and enable_secret):
        if os.path.exists(creds.CREDS_FILE):
            vault = creds.load_credentials()
            user, pwd, enable_secret = vault["NET_USER"], vault["NET_PASS"], vault["NET_ENABLE"]
        else:
            sys.exit(
                "No credentials available. Either export NET_USER/NET_PASS/NET_ENABLE, "
                "or run ./venv/bin/python3 encrypt_creds.py once to store them encrypted."
            )
    nr.inventory.defaults.username = user
    nr.inventory.defaults.password = pwd
    # groups.yaml already builds a netmiko ConnectionOptions per group; mutate its
    # extras in place rather than replacing it, or the conn_timeout
    # settings defined there would be lost.
    for group in nr.inventory.groups.values():
        conn_opts = group.connection_options.get("netmiko")
        if conn_opts is not None:
            conn_opts.extras["secret"] = enable_secret
    return nr


def enable_mode(task: Task) -> Result:
    conn = task.host.get_connection("netmiko", task.nornir.config)
    if not conn.check_enable_mode():
        conn.enable()
    return Result(host=task.host, result="enable mode confirmed")


def check_boot_mode(task: Task) -> Result:
    out = task.run(task=netmiko_send_command, command_string="show version | include image", read_timeout=30).result
    m = BOOT_MODE_RE.search(out)
    if not m:
        return Result(host=task.host, failed=True, result="could not parse 'show version' output")
    image_file = m.group("file")
    mode = "install" if image_file == "packages.conf" else "bundle"
    task.host["boot_mode"] = mode
    return Result(host=task.host, result=f"boot_mode={mode} (image file: {image_file})")


def check_free_space(task: Task) -> Result:
    out = task.run(task=netmiko_send_command, command_string="dir flash: | include bytes free", read_timeout=30).result
    m = re.search(r"\((?P<free>\d+) bytes free\)", out)
    if not m:
        return Result(host=task.host, failed=True, result="could not parse free space")
    free = int(m.group("free"))
    required = task.host["min_free_bytes"]
    ok = free >= required
    task.host["free_bytes"] = free
    task.host["free_required"] = required
    task.host["free_ok"] = ok
    return Result(
        host=task.host,
        failed=not ok,
        result=f"{free} bytes free (need {required})",
    )


def summarize_pass_fail(agg_result, label):
    failed = [h for h, m in agg_result.items() if m.failed]
    if failed:
        print(f"{label}: FAILED on {', '.join(failed)}")
        for h in failed:
            print(f"  {h}: {agg_result[h][0].result}")
    else:
        print(f"{label}: OK on all {len(agg_result)} switches.")


def check_all(task: Task) -> Result:
    """Runs both checks and stores a plain summary dict for print_check_summary()."""
    task.run(task=check_boot_mode)
    task.run(task=check_free_space)
    return Result(
        host=task.host,
        result={
            "mode": task.host.get("boot_mode", "unknown"),
            "free_bytes": task.host.get("free_bytes"),
            "free_required": task.host.get("free_required"),
            "free_ok": task.host.get("free_ok", False),
        },
    )


def print_check_summary(agg_result):
    LOW_MARGIN_BYTES = 300_000_000  # flag switches within ~300MB of the minimum
    rows = []
    all_ok = True
    needs_detail = False
    for host, multi in agg_result.items():
        r = multi[0].result
        if not isinstance(r, dict):
            # A sub-task (check_boot_mode/check_free_space) failed, which
            # makes nornir's Task.run() raise NornirSubTaskError immediately
            # - check_all's own dict-returning line never executes, and
            # multi[0].result becomes nornir's generic wrapper string instead.
            # Don't try to guess the real detail out of nornir's internal
            # result tree; just print it properly below via print_result().
            all_ok = False
            needs_detail = True
            rows.append((host, "?", "?", "FAIL (see detail below)"))
            continue
        free_gb = (r["free_bytes"] or 0) / 1e9
        req_gb = (r["free_required"] or 0) / 1e9
        if r["mode"] == "unknown" or not r["free_ok"]:
            status = "FAIL"
            all_ok = False
        elif r["free_bytes"] is not None and (r["free_bytes"] - r["free_required"]) < LOW_MARGIN_BYTES:
            status = "OK (tight)"
        else:
            status = "OK"
        rows.append((host, r["mode"], f"{free_gb:.2f} / {req_gb:.2f} GB", status))

    name_w = max(len(r[0]) for r in rows) + 2
    print(f"\n{'Site':<{name_w}}{'Mode':<10}{'Free / Min':<18}Status")
    print("-" * (name_w + 40))
    for name, mode, space, status in rows:
        print(f"{name:<{name_w}}{mode:<10}{space:<18}{status}")
    print("-" * (name_w + 40))
    ready = sum(1 for r in rows if r[3].startswith("OK"))
    print(f"{ready}/{len(rows)} switches ready to stage."
          + ("" if all_ok else "  Fix the FAILs above before staging."))

    if needs_detail:
        print("\n--- full detail for the FAIL(s) above ---")
        print_result(agg_result)


def _transfer_progress():
    """scp progress callback that emits one marker per whole percent."""
    last = [-1]

    def cb(filename, size, sent, *_):
        pct = int(sent * 100 / size) if size else 0
        if pct != last[0]:
            last[0] = pct
            progress(f"{pct}% ({sent / 1e6:.0f}/{size / 1e6:.0f} MB)")
    return cb


def stage_image(task: Task) -> Result:
    """Ensure the SCP server is enabled, then copy image to flash via SCP and verify its MD5."""
    local_path = task.host["local_image_path"]
    remote_file = task.host["image_filename"]

    # A multi-hundred-MB transfer can take well past IOS's default 10-minute
    # exec-timeout. The transfer itself runs on its own SCP channel, but the
    # original control channel (used for enable/config, and reused right
    # after for the MD5 verify) sits idle the whole time and gets killed by
    # the switch mid-verify unless we push a generous exec-timeout first.
    # 120 min leaves room for transfers over slow WAN links.
    # Not 0 (never): upgrade_install_mode's write memory saves this permanently.
    progress("enabling SCP, setting exec-timeout")
    task.run(
        task=netmiko_send_config,
        config_commands=[
            "ip scp server enable",
            "line vty 0 4",
            "exec-timeout 120 0",
            "line vty 5 15",
            "exec-timeout 120 0",
        ],
        read_timeout=30,
    )

    xfer = task.run(
        task=netmiko_file_transfer,
        source_file=local_path,
        dest_file=remote_file,
        file_system="flash:",
        direction="put",
        overwrite_file=False,
        progress=_transfer_progress(),
    )
    # netmiko_file_transfer's Result.result is a plain bool (file present and
    # MD5-valid), not the dict scp_result it's derived from - .changed tells
    # us whether it actually copied the file or found it already there.
    if not xfer.result:
        return Result(host=task.host, failed=True, result="file transfer or MD5 verification failed")
    transfer_note = "image transferred" if xfer.changed else "image already present on flash, skipped transfer"

    expected_md5 = task.host["image_md5"]
    if not expected_md5:
        return Result(host=task.host, result=f"{transfer_note}; no image_md5 set, skipping verification")

    progress("verifying MD5 on flash")
    md5_out = task.run(
        task=netmiko_send_command,
        command_string=f"verify /md5 flash:{remote_file}",
        read_timeout=180,
    ).result
    m = re.search(r"=\s*([0-9a-f]{32})", md5_out)
    actual_md5 = m.group(1) if m else None
    ok = actual_md5 == expected_md5.lower()
    note(("image transferred" if xfer.changed else "image already on flash") + (", MD5 OK" if ok else ", MD5 MISMATCH"))
    return Result(
        host=task.host,
        failed=not ok,
        result=f"{transfer_note}; md5 expected={expected_md5} actual={actual_md5}",
    )


def upgrade_install_mode(task: Task) -> Result:
    remote_file = task.host["image_filename"]
    # prompt-level none suppresses every interactive prompt IOS-XE would otherwise show,
    # including the "This will reload the system, proceed? [confirm]" one - which is the
    # only place the literal text "reload" was ever going to appear. Waiting on
    # expect_string="reload" here was structurally guaranteed to time out on every run,
    # success or failure, since prompt-level none removes the one line it was looking for.
    # The install operation runs as its own process on the switch independent of this SSH
    # session, so a dropped connection or a client-side timeout here is expected, not an
    # error - verify_version() below is what actually confirms success or failure.
    # install add/activate/commit refuses to run if running-config differs from
    # startup-config (fails with "System configuration has been modified. Please
    # save configuration and resubmit command."). stage_image() pushes exec-timeout
    # changes via config mode but never saves them, so save here first.
    progress("saving config")
    task.run(task=netmiko_send_command, command_string="write memory", read_timeout=60)
    progress("install add/activate/commit running")
    # Deliberately NOT task.run() below: task.run() appends the subtask's Result to
    # this task's own results list, and raises, *before* control ever reaches our
    # except - so even though we catch the exception and return a clean Result,
    # Nornir still sees a failed entry in the results list and marks the host
    # failed (MultiResult.failed is `any(r.failed for r in results)`). Going
    # straight at the netmiko connection keeps the expected session-drop out of
    # Nornir's result tree entirely.
    conn = task.host.get_connection("netmiko", task.nornir.config)
    try:
        out = conn.send_command(
            f"install add file flash:{remote_file} activate commit prompt-level none",
            read_timeout=120,
        )
    except Exception as exc:
        note("install issued, switch will reload on its own")
        return Result(
            host=task.host,
            result=f"install add/activate/commit issued; session ended ({exc.__class__.__name__}) - "
                   f"expected once the switch actually reloads, verify separately",
        )
    # Getting the prompt back means IOS-XE finished the command without reloading,
    # which normally means it rejected it outright (e.g. "System configuration
    # has been modified" FAILED). Don't report that as a reload.
    if "FAILED" in out:
        return Result(
            host=task.host,
            failed=True,
            result=f"install add/activate/commit FAILED, switch is NOT reloading:\n{out}",
        )
    note("install issued, switch is reloading")
    return Result(host=task.host, result=f"install add/activate/commit issued; switch is reloading\n{out}")


def upgrade_bundle_mode(task: Task) -> Result:
    remote_file = task.host["image_filename"]
    task.run(
        task=netmiko_send_config,
        config_commands=[f"boot system flash:{remote_file}"],
        read_timeout=30,
    )
    task.run(task=netmiko_send_command, command_string="write memory", read_timeout=60)
    # Same reasoning as upgrade_install_mode: go straight at the connection so a
    # session drop from the reload doesn't leave a failed subtask Result behind
    # for Nornir to key its failure detection off.
    conn = task.host.get_connection("netmiko", task.nornir.config)
    try:
        conn.send_command("reload", expect_string=r"confirm")
        conn.send_command_timing("\n")
    except Exception as exc:
        return Result(
            host=task.host,
            result=f"boot statement set, config saved, reload issued; session ended ({exc.__class__.__name__}) - "
                   f"expected once the switch actually reloads, verify separately",
        )
    return Result(host=task.host, result="boot statement set, config saved, reload issued")


def do_upgrade(task: Task) -> Result:
    mode = task.host.get("boot_mode")
    if mode is None:
        mode = check_boot_mode(task).result and task.host["boot_mode"]
    if mode == "install":
        return upgrade_install_mode(task)
    return upgrade_bundle_mode(task)


def _ping(addr: str) -> bool:
    return subprocess.run(
        ["ping", "-c", "1", "-W", "2", addr],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def _ssh_up(addr: str, port: int = 22) -> bool:
    """True once the switch answers on SSH with a protocol banner.

    A plain socket check, so a switch that is still booting produces no
    paramiko tracebacks on screen.
    """
    try:
        with socket.create_connection((addr, port), timeout=5) as sock:
            sock.settimeout(5)
            return sock.recv(64).startswith(b"SSH-")
    except OSError:
        return False


DOWN_CONFIRM_SECONDS = 30


def wait_for_reload(host: str, addr: str, status_every: int = 60) -> None:
    """Block until the switch has reloaded and is back on SSH. No time limit.

    Install add/activate on a 9200L can run well past 15 minutes before the
    reload starts, so there is deliberately no timeout here: it waits until the
    switch is back, or until the operator presses Ctrl+C. Runs in the main
    thread (not inside a Nornir task) so Ctrl+C stops it straight away.

    The switch only counts as "down" once ping has failed continuously for
    DOWN_CONFIRM_SECONDS. The CPU is busy during install add/activate and can
    drop the odd ping: one site lost a single ping mid-install, it was
    wrongly treated as reloading, and verify then hit the old image.
    """
    start = time.monotonic()
    elapsed = lambda: time.strftime("%M:%S" if time.monotonic() - start < 3600 else "%H:%M:%S",
                                    time.gmtime(time.monotonic() - start))
    last_status = time.monotonic()

    def say(msg):
        # Milestones: always shown (as a progress marker under run-upgrade.sh).
        if _progress.ENABLED:
            progress(msg)
        else:
            print(f"[{host}] {msg}", flush=True)

    def status(msg):
        # Repeating status: every loop under run-upgrade.sh (it only redraws one line),
        # otherwise once every status_every seconds.
        nonlocal last_status
        if _progress.ENABLED:
            progress(msg)
        elif time.monotonic() - last_status >= status_every:
            print(f"[{host}] {msg} ({elapsed()} elapsed, Ctrl+C to stop)", flush=True)
            last_status = time.monotonic()

    say("waiting for switch to reload (Ctrl+C to stop)")
    down_since = None
    while True:
        if _ping(addr):
            if down_since is not None:
                say(f"ping blip ({int(time.monotonic() - down_since)}s), not counting as a reload")
                down_since = None
            status("waiting for switch to reload")
            time.sleep(3)
            continue
        if down_since is None:
            down_since = time.monotonic()
        if time.monotonic() - down_since >= DOWN_CONFIRM_SECONDS:
            break
        status(f"ping lost, confirming reload ({int(time.monotonic() - down_since)}s)")
        time.sleep(1)
    say(f"switch went down at {elapsed()}, reloading")

    say("waiting for switch to be online")
    while not _ping(addr):
        status("waiting for switch to be online")
        time.sleep(5)
    while not _ssh_up(addr):
        status("waiting for switch to be online")
        time.sleep(10)
    say(f"switch is online at {elapsed()}, giving it 60s to settle")
    time.sleep(60)


def verify_version(task: Task) -> Result:
    out = task.run(
        task=netmiko_send_command,
        command_string="show version | include Version",
        read_timeout=60,
    ).result
    target = task.host["target_version"]
    ok = target in out
    m = re.search(r"Cisco IOS XE Software, Version (\S+)", out)
    note(f"running {m.group(1) if m else 'unknown'}" + ("" if ok else f", expected {target}"))
    return Result(host=task.host, failed=not ok, result=out.strip())


INTERRUPTED_MSG = """
[{host}] Stopped by Ctrl+C while waiting. Nothing was sent to the switch.
  If it hasn't reloaded yet, the install may still be running on the switch:
  do NOT re-run 'upgrade'. Check 'show install summary' / 'show install log'.
  Once it is back on the new version, finish with: ./post-verify.sh {host}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("phase", choices=["check", "stage", "upgrade", "verify"])
    parser.add_argument("--host", help="limit to a single inventory host (recommended for 'upgrade')")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--no-wait", action="store_true", help="verify only: skip waiting for the reload and check the version straight away")
    args = parser.parse_args()

    nr = load_inventory(num_workers=args.workers)
    if args.host:
        nr = nr.filter(F(name=args.host))

    if args.phase == "verify" and not args.no_wait:
        # Wait before opening any SSH session: a session opened now would die
        # with the reload.
        for host in nr.inventory.hosts.values():
            try:
                wait_for_reload(host.name, host.hostname)
            except KeyboardInterrupt:
                print(INTERRUPTED_MSG.format(host=host.name), flush=True)
                sys.exit(130)

    progress("connecting")
    enable_result = nr.run(task=enable_mode)
    summarize_pass_fail(enable_result, "Enable mode")
    if enable_result.failed_hosts:
        sys.exit(
            f"Aborting: enable mode failed on {', '.join(sorted(enable_result.failed_hosts))}, "
            "not safe to continue."
        )

    if args.phase == "check":
        check_result = nr.run(task=check_all)
        print_check_summary(check_result)
        for multi in check_result.values():
            r = multi[0].result
            if isinstance(r, dict) and r["free_bytes"] is not None:
                note(f"{r['mode']} mode, {r['free_bytes'] / 1e9:.2f} GB free (min {r['free_required'] / 1e9:.2f})")
        if check_result.failed_hosts:
            sys.exit(1)
    elif args.phase == "stage":
        result = nr.run(task=stage_image)
        print_result(result)
        if result.failed_hosts:
            sys.exit(1)
    elif args.phase == "upgrade":
        if not args.host:
            sys.exit("Refusing to reload all 10 switches at once - pass --host <name> and go one at a time.")
        result = nr.run(task=do_upgrade)
        print_result(result)
        if result.failed_hosts:
            sys.exit(1)
    elif args.phase == "verify":
        result = nr.run(task=verify_version)
        print_result(result)
        if result.failed_hosts:
            sys.exit(1)


if __name__ == "__main__":
    main()
