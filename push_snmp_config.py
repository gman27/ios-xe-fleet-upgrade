#!/usr/bin/env python3
"""
Push read-only SNMPv3 config to every switch in inventory/hosts.yaml, so your
NOC monitoring host (e.g. 10.0.0.50) can poll CPU/memory/interfaces.

Config pushed per host (idempotent — same commands can be re-run safely,
but see the note on stale USM state below):
    snmp-server view NOC-RO-VIEW iso included
    snmp-server group NOC-RO-GROUP v3 priv read NOC-RO-VIEW
    snmp-server user <SNMP_USER> NOC-RO-GROUP v3 auth <AUTH> <AUTH_PASS> priv aes <KEYSIZE> <PRIV_PASS>
    snmp-server location <site_name>
    snmp-server contact NOC Monitoring

Two-tier auth/priv depending on platform capability — confirmed by direct
testing across the fleet, not assumed:
  - SHA256_CAPABLE hosts (currently HQ-CORE, BRANCH-EAST — already
    upgraded to the target 17.18.x image): auth sha-2 256 + priv aes 256.
    This is the only combo that works on these; SHA-1 auth on this platform
    fails to derive a working AES key of ANY size.
  - Everyone else (older/pre-upgrade firmware, e.g. 17.9.4 on BRANCH-NORTH):
    "auth sha-2 256" is silently downgraded to SHA-1 by the switch itself
    regardless of what you type (visible via `show snmp user`), and SHA-1
    only derives a working key for AES-128, not AES-256 — so these get
    auth sha + priv aes 128. Once these switches are upgraded via
    upgrade.py to match HQ-CORE/BRANCH-EAST, move
    their name into SHA256_CAPABLE and re-run this script.

IMPORTANT: if a host's user was previously configured with a DIFFERENT
auth/priv combo (e.g. during earlier troubleshooting), just re-issuing
`snmp-server user ...` again with new parameters can leave stale USM key
state behind on some platforms. This script issues `no snmp-server user`
first to force a clean recreate every time, specifically to avoid that.

No ACL — v3 auth+priv is the only access control (any source that has the
username/auth/priv passwords can poll). If you want to scope this to the
collector's IP later, that's a one-line group/user change (add "access
<acl-name>").

SNMPv3 user/auth/priv passwords are never echoed back by `show running-config`
(IOS stores/displays them as localized keys), so re-running this script is
safe and won't show a diff on the user line once applied.

Credentials come from env vars, same as backup_config.py/upgrade.py:
    export NET_USER=admin
    export NET_PASS='...'
    export NET_ENABLE='...'   # enable secret

The SNMPv3 auth/priv passwords are read from SNMP_AUTH_PASS / SNMP_PRIV_PASS
env vars (not hardcoded here) so they don't end up sitting in this file.

Usage:
    python3 push_snmp_config.py                # all hosts, saves config after
    python3 push_snmp_config.py --host BRANCH-WEST  # one host
    python3 push_snmp_config.py --no-save       # push but skip "write memory"
"""
import argparse
import os
import sys

from nornir import InitNornir
from nornir.core.filter import F
from nornir.core.task import Result, Task
from nornir_netmiko.tasks import netmiko_send_config, netmiko_save_config
from nornir_utils.plugins.functions import print_result

SNMP_USER        = "svc-noc-monitor"
SNMP_GROUP       = "NOC-RO-GROUP"
SNMP_VIEW        = "NOC-RO-VIEW"

# Switches confirmed (by direct SNMPv3 testing) to actually support SHA-2/256
# + AES-256 — everyone else falls back to SHA-1 + AES-128 (see module
# docstring). Update this set as more switches get upgraded to the target
# firmware via upgrade.py.
SHA256_CAPABLE = {"HQ-CORE", "BRANCH-EAST"}


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


def build_config(task: Task, auth_pass: str, priv_pass: str) -> list:
    site_name = task.host.get("site_name", task.host.name)
    if task.host.name in SHA256_CAPABLE:
        auth_clause = f"sha-2 256 {auth_pass}"
        priv_clause = f"aes 256 {priv_pass}"
    else:
        auth_clause = f"sha {auth_pass}"
        priv_clause = f"aes 128 {priv_pass}"
    return [
        f"snmp-server view {SNMP_VIEW} iso included",
        f"snmp-server group {SNMP_GROUP} v3 priv read {SNMP_VIEW}",
        # Clean recreate — redefining an existing user in place can leave
        # stale localized keys behind on some platforms (observed on
        # HQ-CORE after switching its auth protocol mid-troubleshooting).
        f"no snmp-server user {SNMP_USER} {SNMP_GROUP} v3",
        f"snmp-server user {SNMP_USER} {SNMP_GROUP} v3 auth {auth_clause} priv {priv_clause}",
        f"snmp-server location {site_name}",
        "snmp-server contact NOC Monitoring",
    ]


def push(task: Task, auth_pass: str, priv_pass: str, save: bool) -> Result:
    conn = task.host.get_connection("netmiko", task.nornir.config)
    if not conn.check_enable_mode():
        conn.enable()
    commands = build_config(task, auth_pass, priv_pass)
    out = task.run(
        task=netmiko_send_config,
        config_commands=commands,
        read_timeout=60,
    ).result
    if "Invalid input" in out or "% " in out:
        return Result(host=task.host, failed=True, result=out)
    if save:
        task.run(task=netmiko_save_config)
    return Result(host=task.host, result=f"applied {len(commands)} lines" + (" + saved" if save else ""))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", help="limit to a single inventory host")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--no-save", action="store_true", help="skip 'write memory' after pushing")
    args = parser.parse_args()

    auth_pass = os.environ.get("SNMP_AUTH_PASS")
    priv_pass = os.environ.get("SNMP_PRIV_PASS")
    if not auth_pass or not priv_pass:
        sys.exit("Set SNMP_AUTH_PASS and SNMP_PRIV_PASS environment variables first.")
    if len(auth_pass) < 8 or len(priv_pass) < 8:
        sys.exit("SNMPv3 auth/priv passwords must be at least 8 characters (IOS minimum).")

    nr = load_inventory(num_workers=args.workers)
    if args.host:
        nr = nr.filter(F(name=args.host))

    result = nr.run(task=push, auth_pass=auth_pass, priv_pass=priv_pass, save=not args.no_save)
    print_result(result)

    failed = sorted(result.failed_hosts)
    print(f"\n{len(nr.inventory.hosts) - len(failed)}/{len(nr.inventory.hosts)} succeeded")
    if failed:
        print("failed: " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
