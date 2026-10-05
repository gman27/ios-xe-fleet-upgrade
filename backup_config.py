#!/usr/bin/env python3
"""
Pull `show running-config` from every switch in inventory/hosts.yaml and save
one file per host: configs/<HOSTNAME>.cfg

Credentials come from env vars, same as upgrade.py:
    export NET_USER=admin
    export NET_PASS='...'
    export NET_ENABLE='...'   # enable secret

Usage:
    python3 backup_config.py                # all hosts
    python3 backup_config.py --host BRANCH-WEST # one host
    python3 backup_config.py --workers 5    # parallelise (read-only, safe)
"""
import argparse
import datetime
import os
import re
import sys

import warnings

# nornir's __init__ imports pkg_resources, which newer setuptools flags as
# deprecated. Harmless noise; filter only that exact message.
warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API")

from nornir import InitNornir
from nornir.core.filter import F
from nornir.core.task import Result, Task
from nornir_netmiko.tasks import netmiko_send_command
from nornir_utils.plugins.functions import print_result

from progress import note, progress

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs")


def safe_filename_part(name: str) -> str:
    """Sanitize a hostname before it goes into a filename. Defense in depth
    against a malformed inventory host key (e.g. containing '/' or '..')
    writing outside OUT_DIR."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def load_inventory(num_workers=1):
    nr = InitNornir(config_file="config.yaml")
    nr.config.runner.options["num_workers"] = num_workers
    user = os.environ.get("NET_USER")
    pwd = os.environ.get("NET_PASS")
    enable_secret = os.environ.get("NET_ENABLE")
    if not user or not pwd or not enable_secret:
        sys.exit("Set NET_USER, NET_PASS, and NET_ENABLE environment variables first.")
    nr.inventory.defaults.username = user
    nr.inventory.defaults.password = pwd
    for group in nr.inventory.groups.values():
        conn_opts = group.connection_options.get("netmiko")
        if conn_opts is not None:
            conn_opts.extras["secret"] = enable_secret
    return nr


def backup(task: Task) -> Result:
    conn = task.host.get_connection("netmiko", task.nornir.config)
    if not conn.check_enable_mode():
        conn.enable()
    progress("show running-config")
    running = task.run(
        task=netmiko_send_command,
        command_string="show running-config",
        read_timeout=120,
    ).result
    if "Invalid input" in running or not running.strip():
        return Result(host=task.host, failed=True, result="empty / bad output")

    now = datetime.datetime.now()
    header = f"! {task.host.name}  ({task.host.hostname})\n! captured {now:%Y-%m-%d %H:%M:%S}\n"
    path = os.path.join(OUT_DIR, f"{safe_filename_part(task.host.name)}-{now:%Y-%m-%d}.cfg")
    with open(path, "w") as fh:
        fh.write(header + running.rstrip() + "\n")
    note(f"{len(running.splitlines())} lines saved to {os.path.relpath(path)}")
    return Result(host=task.host, result=f"saved {path} ({len(running.splitlines())} lines)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", help="limit to a single inventory host")
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    nr = load_inventory(num_workers=args.workers)
    if args.host:
        nr = nr.filter(F(name=args.host))

    result = nr.run(task=backup)
    print_result(result)

    failed = sorted(result.failed_hosts)
    print(f"\n{len(nr.inventory.hosts) - len(failed)}/{len(nr.inventory.hosts)} succeeded")
    if failed:
        print("failed: " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
