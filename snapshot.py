#!/usr/bin/env python3
"""
Capture a fixed set of `show` commands from a switch (for pre/post-upgrade
comparison), parsed into structured data via TextFSM/ntc-templates, and diff
two captures against each other with colored, field-level output.

Usage:
    python3 snapshot.py capture --host BRANCH-EAST --label pre
    python3 snapshot.py capture --host BRANCH-EAST --label post
    python3 snapshot.py diff --host BRANCH-EAST                 # latest pre vs latest post
    python3 snapshot.py diff --host BRANCH-EAST --pre <path> --post <path>
    python3 snapshot.py diff --host BRANCH-EAST --no-color
    python3 snapshot.py diff --host BRANCH-EAST --html /tmp/diff.html  # side-by-side HTML report

Credentials come from env vars, same as upgrade.py:
    export NET_USER=admin
    export NET_PASS='...'
    export NET_ENABLE='...'
"""
import argparse
import datetime
import difflib
import glob
import json
import os
import re
import sys

from nornir.core.filter import F
from nornir.core.task import Result, Task
from nornir_netmiko.tasks import netmiko_send_command
from nornir_utils.plugins.functions import print_result

import upgrade  # reuse load_inventory() / credential handling

SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")


def safe_filename_part(name: str) -> str:
    """Sanitize a hostname before it goes into a filename. Defense in depth
    against a malformed inventory host key (e.g. containing '/' or '..')
    writing outside SNAPSHOT_DIR."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)

SNAPSHOT_COMMANDS = [
    "show ip interface brief",
    "show vlan",
    "show interfaces status",
    "show interfaces description",
    "show ip route",
    "show arp",
    "show mac address-table",
    "show version",
]

# Field(s) that uniquely identify a row for each table command, so pre/post
# rows get paired up correctly instead of diffed as an unordered blob. Field
# names match the lowercased ntc-templates Value names for cisco_ios.
ROW_KEY = {
    "show ip interface brief": ("interface",),
    "show vlan": ("vlan_id",),
    "show interfaces status": ("port",),
    "show interfaces description": ("port",),
    "show ip route": ("network", "prefix_length", "nexthop_ip", "nexthop_if"),
    "show arp": ("address",),
    "show mac address-table": ("vlan_id", "destination_address"),
}
# "show version" is handled as a single record (field-by-field), not a table.

RED, GREEN, YELLOW, CYAN, BOLD, RESET = (
    "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[1m", "\033[0m",
)


def capture_snapshot(task: Task, label: str) -> Result:
    conn = task.host.get_connection("netmiko", task.nornir.config)
    if not conn.check_enable_mode():
        conn.enable()

    captured = {}
    for cmd in SNAPSHOT_COMMANDS:
        data = task.run(
            task=netmiko_send_command,
            command_string=cmd,
            read_timeout=60,
            use_textfsm=True,
        ).result
        # netmiko returns parsed list-of-dicts when a template matched, or
        # the raw string unchanged when it didn't (never raises).
        captured[cmd] = {"parsed": not isinstance(data, str), "data": data}

    now = datetime.datetime.now()
    path = os.path.join(SNAPSHOT_DIR, f"{safe_filename_part(task.host.name)}-{label}-{now:%Y%m%d_%H%M%S}.json")
    with open(path, "w") as fh:
        json.dump(captured, fh, indent=2, default=str)
    return Result(host=task.host, result=f"saved {path}")


def latest_snapshot(host, label):
    matches = sorted(glob.glob(os.path.join(SNAPSHOT_DIR, f"{host}-{label}-*.json")))
    if not matches:
        sys.exit(f"No '{label}' snapshot found for {host} in {SNAPSHOT_DIR}")
    return matches[-1]


def c(text, color, use_color):
    return f"{color}{text}{RESET}" if use_color else text


def diff_single_record(pre_row, post_row, use_color):
    pre_row = pre_row or {}
    post_row = post_row or {}
    lines = []
    for key in sorted(set(pre_row) | set(post_row)):
        pv, nv = pre_row.get(key), post_row.get(key)
        if pv != nv:
            lines.append(c(f"  {key}: {pv!r} -> {nv!r}", YELLOW, use_color))
    return lines


def diff_table(cmd, pre_rows, post_rows, use_color):
    key_fields = ROW_KEY.get(cmd)
    sample = (pre_rows[0] if pre_rows else post_rows[0]) if (pre_rows or post_rows) else {}
    lines = []

    if key_fields and all(kf in sample for kf in key_fields):
        def rowkey(r):
            return tuple(r.get(kf) for kf in key_fields)

        pre_map = {rowkey(r): r for r in pre_rows}
        post_map = {rowkey(r): r for r in post_rows}
        for k in sorted(set(pre_map) | set(post_map), key=lambda t: tuple(str(x) for x in t)):
            pr, po = pre_map.get(k), post_map.get(k)
            label = " ".join(f"{kf}={v}" for kf, v in zip(key_fields, k))
            if pr is None:
                lines.append(c(f"+ {label}: {po}", GREEN, use_color))
            elif po is None:
                lines.append(c(f"- {label}: {pr}", RED, use_color))
            elif pr != po:
                lines.append(f"  {label}:")
                for kk in sorted(set(pr) | set(po)):
                    if pr.get(kk) != po.get(kk):
                        lines.append(c(f"    {kk}: {pr.get(kk)!r} -> {po.get(kk)!r}", YELLOW, use_color))
    else:
        # No usable key for this table - fall back to whole-row set diff.
        def norm(r):
            return json.dumps(r, sort_keys=True, default=str)

        pre_set = {norm(r): r for r in pre_rows}
        post_set = {norm(r): r for r in post_rows}
        for nk, r in pre_set.items():
            if nk not in post_set:
                lines.append(c(f"- {r}", RED, use_color))
        for nk, r in post_set.items():
            if nk not in pre_set:
                lines.append(c(f"+ {r}", GREEN, use_color))
    return lines


def diff_raw_text(pre_text, post_text, use_color):
    pre_lines = pre_text.splitlines()
    post_lines = post_text.splitlines()
    out = []
    for line in difflib.unified_diff(pre_lines, post_lines, fromfile="pre", tofile="post", lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            out.append(c(line, GREEN, use_color))
        elif line.startswith("-") and not line.startswith("---"):
            out.append(c(line, RED, use_color))
        elif line.startswith("@@"):
            out.append(c(line, CYAN, use_color))
        else:
            out.append(line)
    return out


def entry_to_lines(cmd, entry):
    """Flatten one captured command's data into plain text lines for
    line-based diffing (used by the HTML report). Structured data is
    rendered one row per line, sorted by its ROW_KEY so equivalent rows
    from pre/post line up positionally instead of diffing as reordered
    noise just because the switch returned rows in a different order."""
    if not entry["parsed"]:
        text = entry["data"] if isinstance(entry["data"], str) else str(entry["data"])
        return text.splitlines()

    data = entry["data"]
    if cmd == "show version":
        row = data[0] if data else {}
        return [f"{key}: {row[key]!r}" for key in sorted(row)]

    key_fields = ROW_KEY.get(cmd)
    sample = data[0] if data else {}

    def render_row(r):
        rest = ", ".join(f"{k}={v!r}" for k, v in sorted(r.items()))
        return rest

    if key_fields and all(kf in sample for kf in key_fields):
        rows = sorted(data, key=lambda r: tuple(str(r.get(kf)) for kf in key_fields))
    else:
        rows = data
    return [render_row(r) for r in rows]


# CSS class names below (diff_header/diff_next/diff_add/diff_chg/diff_sub)
# come from difflib.HtmlDiff itself, not chosen here - make_table() emits
# spans/cells with exactly these classes, so the styling has to target them
# to take effect. Colors follow the same green/red/yellow convention
# Notepad++'s Compare plugin uses (insert/delete/change).
HTML_STYLE = """
<style>
  :root {
    --bg: #ffffff; --fg: #1a1a1a; --muted: #6b7280; --border: #d8dee4;
    --add: #d8f5d0; --add-fg: #14532d;
    --sub: #ffd9d9; --sub-fg: #7f1d1d;
    --chg: #fff2b2; --chg-fg: #713f12;
    --header-bg: #f3f4f6;
  }
  body { font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
         background: var(--bg); color: var(--fg); margin: 0; padding: 24px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .meta { color: var(--muted); font-size: 13px; margin-bottom: 16px; }
  .meta code { background: var(--header-bg); padding: 1px 5px; border-radius: 4px; }
  .banner { font-weight: 600; padding: 10px 14px; border-radius: 6px; margin-bottom: 18px; }
  .banner.flagged { background: var(--chg); color: var(--chg-fg); }
  .banner.clean { background: var(--add); color: var(--add-fg); }
  .legend { display: flex; gap: 16px; font-size: 13px; color: var(--muted); margin-bottom: 18px; }
  .legend span.swatch { display: inline-block; width: 12px; height: 12px; border-radius: 2px;
                         margin-right: 5px; vertical-align: middle; }
  nav.toc { border: 1px solid var(--border); border-radius: 8px; padding: 10px 16px; margin-bottom: 24px; }
  nav.toc ul { list-style: none; margin: 0; padding: 0; columns: 2; }
  nav.toc li { padding: 2px 0; }
  nav.toc a { text-decoration: none; color: var(--fg); font-size: 13px; }
  nav.toc a.changed { color: var(--sub-fg); font-weight: 600; }
  nav.toc a.unchanged { color: var(--muted); }
  h2 { font-size: 15px; border-top: 1px solid var(--border); padding-top: 18px; margin-top: 28px;
       font-family: SFMono-Regular, Consolas, monospace; }
  .badge { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 10px; margin-left: 8px; }
  .badge.changed { background: var(--sub); color: var(--sub-fg); }
  .badge.unchanged { background: var(--header-bg); color: var(--muted); }
  table.diff { border-collapse: collapse; width: 100%; font-family: SFMono-Regular, Consolas, monospace;
               font-size: 12.5px; margin-bottom: 8px; }
  table.diff td, table.diff th { padding: 1px 6px; }
  .diff_header { background: var(--header-bg); color: var(--muted); text-align: right;
                 user-select: none; border-right: 1px solid var(--border); }
  .diff_next { background: var(--header-bg); width: 1%; text-align: center; }
  .diff_next a { color: var(--muted); text-decoration: none; }
  table.diff td[nowrap] { white-space: pre-wrap; word-break: break-word; }
  span.diff_add { background: var(--add); color: var(--add-fg); }
  span.diff_sub { background: var(--sub); color: var(--sub-fg); }
  span.diff_chg { background: var(--chg); color: var(--chg-fg); }
</style>
"""


def build_html_diff(host, pre, post, pre_path, post_path):
    """Assemble a single-page, side-by-side HTML diff across all
    SNAPSHOT_COMMANDS, styled like Notepad++'s Compare plugin (green =
    added, red = removed, yellow = changed). Built on difflib.HtmlDiff,
    which already produces that table layout; this just themes it and
    stitches one table per command into one report with a jump-to-section
    summary, so it's viewable in any browser with no GUI app needed."""
    differ = difflib.HtmlDiff(wrapcolumn=100)
    sections = []
    any_diff = False
    for cmd in SNAPSHOT_COMMANDS:
        pre_entry = pre.get(cmd, {"parsed": False, "data": ""})
        post_entry = post.get(cmd, {"parsed": False, "data": ""})
        pre_lines = entry_to_lines(cmd, pre_entry)
        post_lines = entry_to_lines(cmd, post_entry)
        changed = pre_lines != post_lines
        any_diff = any_diff or changed
        if pre_lines or post_lines:
            table = differ.make_table(
                pre_lines, post_lines, fromdesc="pre", todesc="post", context=True, numlines=3,
            )
        else:
            table = "<p><em>no data captured on either side</em></p>"
        anchor = re.sub(r"[^a-z0-9]+", "-", cmd.lower()).strip("-")
        sections.append({"cmd": cmd, "anchor": anchor, "changed": changed, "table": table})

    nav = "\n".join(
        f'<li><a href="#{s["anchor"]}" class="{"changed" if s["changed"] else "unchanged"}">'
        f'{"CHANGED" if s["changed"] else "unchanged"} - {s["cmd"]}</a></li>'
        for s in sections
    )
    body = "\n".join(
        f'<h2 id="{s["anchor"]}">{s["cmd"]}'
        f'<span class="badge {"changed" if s["changed"] else "unchanged"}">'
        f'{"CHANGED" if s["changed"] else "unchanged"}</span></h2>\n{s["table"]}'
        for s in sections
    )
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    banner_class = "flagged" if any_diff else "clean"
    banner_text = (
        "FLAGGED: differences found below, review before closing out the change."
        if any_diff else "No differences found across any of the captured commands."
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Snapshot diff: {host}</title>
{HTML_STYLE}
</head>
<body>
  <h1>Snapshot diff: {host}</h1>
  <div class="meta">
    generated {now}<br>
    pre: <code>{pre_path}</code><br>
    post: <code>{post_path}</code>
  </div>
  <div class="banner {banner_class}">{banner_text}</div>
  <div class="legend">
    <span><span class="swatch" style="background:var(--add)"></span>added</span>
    <span><span class="swatch" style="background:var(--sub)"></span>removed</span>
    <span><span class="swatch" style="background:var(--chg)"></span>changed</span>
  </div>
  <nav class="toc"><ul>
{nav}
  </ul></nav>
{body}
</body>
</html>
"""


def diff_snapshots(host, pre_path, post_path, use_color=True):
    with open(pre_path) as fh:
        pre = json.load(fh)
    with open(post_path) as fh:
        post = json.load(fh)

    out = [f"\nComparing {host}:", f"  pre:  {pre_path}", f"  post: {post_path}", ""]
    any_diff = False
    for cmd in SNAPSHOT_COMMANDS:
        pre_entry = pre.get(cmd, {"parsed": False, "data": ""})
        post_entry = post.get(cmd, {"parsed": False, "data": ""})

        if pre_entry["parsed"] and post_entry["parsed"]:
            pre_data, post_data = pre_entry["data"], post_entry["data"]
            if cmd == "show version":
                pre_row = pre_data[0] if pre_data else {}
                post_row = post_data[0] if post_data else {}
                lines = diff_single_record(pre_row, post_row, use_color)
            else:
                lines = diff_table(cmd, pre_data, post_data, use_color)
        else:
            # One or both sides weren't parseable (no matching template),
            # fall back to a plain text diff of the two payloads.
            lines = diff_raw_text(str(pre_entry["data"]), str(post_entry["data"]), use_color)

        if lines:
            any_diff = True
            out.append(c(f"=== CHANGED: {cmd} ===", BOLD, use_color))
            out.extend(lines)
            out.append("")
        else:
            out.append(f"=== unchanged: {cmd} ===")

    out.append(
        c("\nFLAGGED: differences found above - review before closing out the change.", BOLD + YELLOW, use_color)
        if any_diff
        else "\nNo differences found across any of the captured commands."
    )
    # Single write: some dependency in the netmiko/nornir chain initializes
    # colorama with autoreset=True, which appends its own reset code after
    # *every* stdout.write() - harmless when rendered, but many small
    # print() calls turn into a lot of stray-looking reset noise in the raw
    # bytes. One write keeps that down to a single trailing reset.
    print("\n".join(out))
    return any_diff


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)

    cap = sub.add_parser("capture")
    cap.add_argument("--host", required=True)
    cap.add_argument("--label", required=True, choices=["pre", "post"])

    dif = sub.add_parser("diff")
    dif.add_argument("--host", required=True)
    dif.add_argument("--pre", help="path to a specific pre-snapshot (default: latest for --host)")
    dif.add_argument("--post", help="path to a specific post-snapshot (default: latest for --host)")
    dif.add_argument("--no-color", action="store_true", help="disable ANSI colors (e.g. for plain log files)")
    dif.add_argument("--html", metavar="PATH",
                      help="also write a side-by-side HTML diff report to PATH "
                           "(Notepad++ Compare style: green/red/yellow highlighting), "
                           "viewable in any browser")

    args = parser.parse_args()
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)

    if args.action == "capture":
        nr = upgrade.load_inventory(num_workers=1)
        nr = nr.filter(F(name=args.host))
        result = nr.run(task=capture_snapshot, label=args.label)
        print_result(result)
        if result.failed_hosts:
            sys.exit(1)
    elif args.action == "diff":
        pre_path = args.pre or latest_snapshot(args.host, "pre")
        post_path = args.post or latest_snapshot(args.host, "post")
        use_color = not args.no_color
        # Differences are informational (e.g. `show version` is *expected*
        # to change) - always exit 0, never fails the run on its own.
        diff_snapshots(args.host, pre_path, post_path, use_color=use_color)

        if args.html:
            with open(pre_path) as fh:
                pre = json.load(fh)
            with open(post_path) as fh:
                post = json.load(fh)
            html = build_html_diff(args.host, pre, post, pre_path, post_path)
            with open(args.html, "w") as fh:
                fh.write(html)
            print(f"\nHTML diff report written to {args.html}")


if __name__ == "__main__":
    main()
