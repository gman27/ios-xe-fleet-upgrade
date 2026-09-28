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
import re
import sys
import time

from nornir import InitNornir
from nornir.core.filter import F
from nornir.core.task import Result, Task
from nornir_netmiko.tasks import netmiko_send_command, netmiko_send_config, netmiko_file_transfer
from nornir_utils.plugins.functions import print_result

import creds

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
    # extras in place rather than replacing it, or the conn_timeout/read_timeout
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
    out = task.run(task=netmiko_send_command, command_string="show version | include image").result
    m = BOOT_MODE_RE.search(out)
    if not m:
        return Result(host=task.host, failed=True, result="could not parse 'show version' output")
    image_file = m.group("file")
    mode = "install" if image_file == "packages.conf" else "bundle"
    task.host["boot_mode"] = mode
    return Result(host=task.host, result=f"boot_mode={mode} (image file: {image_file})")


def check_free_space(task: Task) -> Result:
    out = task.run(task=netmiko_send_command, command_string="dir flash: | include bytes free").result
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


def stage_image(task: Task) -> Result:
    """Ensure the SCP server is enabled, then copy image to flash via SCP and verify its MD5."""
    local_path = task.host["local_image_path"]
    remote_file = task.host["image_filename"]

    # A multi-hundred-MB transfer can take well past IOS's default 10-minute
    # exec-timeout. The transfer itself runs on its own SCP channel, but the
    # original control channel (used for enable/config, and reused right
    # after for the MD5 verify) sits idle the whole time and gets killed by
    # the switch mid-verify unless we push a generous exec-timeout first.
    task.run(
        task=netmiko_send_config,
        config_commands=[
            "ip scp server enable",
            "line vty 0 4",
            "exec-timeout 30 0",
            "line vty 5 15",
            "exec-timeout 30 0",
        ],
    )

    xfer = task.run(
        task=netmiko_file_transfer,
        source_file=local_path,
        dest_file=remote_file,
        file_system="flash:",
        direction="put",
        overwrite_file=False,
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

    md5_out = task.run(
        task=netmiko_send_command,
        command_string=f"verify /md5 flash:{remote_file}",
        read_timeout=180,
    ).result
    m = re.search(r"=\s*([0-9a-f]{32})", md5_out)
    actual_md5 = m.group(1) if m else None
    ok = actual_md5 == expected_md5.lower()
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
    task.run(task=netmiko_send_command, command_string="write memory")
    # Deliberately NOT task.run() below: task.run() appends the subtask's Result to
    # this task's own results list, and raises, *before* control ever reaches our
    # except - so even though we catch the exception and return a clean Result,
    # Nornir still sees a failed entry in the results list and marks the host
    # failed (MultiResult.failed is `any(r.failed for r in results)`). Going
    # straight at the netmiko connection keeps the expected session-drop out of
    # Nornir's result tree entirely.
    conn = task.host.get_connection("netmiko", task.nornir.config)
    try:
        conn.send_command(
            f"install add file flash:{remote_file} activate commit prompt-level none",
            read_timeout=120,
        )
    except Exception as exc:
        return Result(
            host=task.host,
            result=f"install add/activate/commit issued; session ended ({exc.__class__.__name__}) - "
                   f"expected once the switch actually reloads, verify separately",
        )
    return Result(host=task.host, result="install add/activate/commit issued; switch is reloading")


def upgrade_bundle_mode(task: Task) -> Result:
    remote_file = task.host["image_filename"]
    task.run(
        task=netmiko_send_config,
        config_commands=[f"boot system flash:{remote_file}"],
    )
    task.run(task=netmiko_send_command, command_string="write memory")
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


def verify_version(task: Task, wait_seconds: int) -> Result:
    time.sleep(wait_seconds)
    out = task.run(
        task=netmiko_send_command,
        command_string="show version | include Version",
        read_timeout=60,
    ).result
    target = task.host["target_version"]
    ok = target in out
    return Result(host=task.host, failed=not ok, result=out.strip())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("phase", choices=["check", "stage", "upgrade", "verify"])
    parser.add_argument("--host", help="limit to a single inventory host (recommended for 'upgrade')")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--wait", type=int, default=300, help="seconds to wait before post-upgrade verify")
    args = parser.parse_args()

    nr = load_inventory(num_workers=args.workers)
    if args.host:
        nr = nr.filter(F(name=args.host))

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
        result = nr.run(task=verify_version, wait_seconds=args.wait)
        print_result(result)
        if result.failed_hosts:
            sys.exit(1)


if __name__ == "__main__":
    main()
