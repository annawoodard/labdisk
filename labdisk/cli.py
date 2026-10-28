"""Storage on your shares: how full, how fast it is filling, and what is using it.

Every command answers from results the scan already saved, in seconds, and never
starts a scan.

  labdisk                   how full each share is, how fast it is growing, any warnings
  labdisk top SHARE         biggest top-level folders on one share, with owners
  labdisk mine              your biggest folders on every share
  labdisk grew --days 7     what grew most over the last 7 days
  labdisk trim              candidates to delete or move, and why (never deletes)
  labdisk report            rewrite the Markdown reports now

Getting started:

  labdisk init              write a starter config (~/.config/labdisk/config.toml)
  labdisk schedule --account A --partition P
                            print the scrontab entries that keep results current

The full report is report.md in the results directory named in your config.
"""

from __future__ import annotations

import argparse
import os
import shutil
import textwrap
import sys
import time

from . import alert, config as config_mod, quota, report, scan, trim
from .report import size, when
from .store import read_json


def cmd_tick(cfg, args) -> None:
    quota.sample(cfg)
    if (cfg.results / "units").exists():
        scan.snapshot(cfg, rotate=False)
    grew = scan.recheck_big_files(cfg)
    for subject in alert.check(cfg, grew):
        print("sent:", subject)
    for path in report.write_all(cfg, trim.candidates(cfg)):
        print("wrote", path)


def cmd_scan(cfg, args) -> None:
    if not scan.run(cfg, args.hours, args.share):
        return
    scan.snapshot(cfg, rotate=True)
    for path in report.write_all(cfg, trim.candidates(cfg)):
        print("wrote", path)


def cmd_status(cfg, args) -> None:
    print("\nQuotas\n")
    print(report.table(report.QUOTA_HEADER, report.quota_rows(cfg, cfg.shares), text=True))
    lines = [w for s in cfg.shares for w in report.warnings(cfg, s, quota.outlook(cfg, s.name))]
    if lines:
        print("\nWarnings\n")
        for line in lines:
            print(textwrap.fill(line, width=100, initial_indent="  ! ", subsequent_indent="    "))
    _scan_notes(cfg, _tables(cfg)[0])
    print(f"Full report: {cfg.results / 'report.md'}\n")


def _scan_notes(cfg, latest: dict, shares: list[str] | None = None) -> None:
    """Say which shares' folder numbers are still incomplete, before or after showing them."""
    block = report.first_scan_lines(cfg, latest, [s for s in cfg.shares if not shares or s.name in shares])
    if block:
        print("\nFirst scan\n")
        for line in block:
            print(textwrap.fill(line, width=100, subsequent_indent="  ") if line else "")
    print()


def _tables(cfg):
    return read_json(cfg.results / "snapshots" / "latest.json", {}), \
        read_json(cfg.results / "snapshots" / "previous.json", {})


def cmd_top(cfg, args) -> None:
    latest, previous = _tables(cfg)
    _scan_notes(cfg, latest, [args.share])
    if args.share not in latest:
        raise SystemExit(f"no scan of {args.share!r} yet; shares: {', '.join(latest) or 'none'}")
    print(report.top_table(latest[args.share], previous.get(args.share), limit=args.n, text=True))


def cmd_mine(cfg, args) -> None:
    latest, _ = _tables(cfg)
    _scan_notes(cfg, latest)
    rows = sorted((r for t in latest.values() for r in t.get("rows", [])), key=lambda r: -r["alloc"])
    print(report.table(["on disk", "files", "last change", "folder"],
                       [[size(r["alloc"]), f"{r['files']:,}", when(r["newest"])[:10], r["path"]]
                        for r in rows[:args.n]], text=True))


def cmd_grew(cfg, args) -> None:
    latest, _ = _tables(cfg)
    _scan_notes(cfg, latest)
    cutoff = time.time() - args.days * 86400
    weeks = sorted((cfg.results / "snapshots").glob("week-*.json"))
    base = None
    for week in weeks:
        if week.stat().st_mtime <= cutoff:
            base = week
    if base is None:
        raise SystemExit(f"no saved table is {args.days} days old yet")
    before = read_json(base, {})
    changes = []
    for name, t in latest.items():
        old = {r["path"]: r["alloc"] for r in before.get(name, {}).get("rows", [])}
        old.update({f"{name}:{k}": v["alloc"] for k, v in before.get(name, {}).get("top", {}).items()})
        for k, v in t.get("top", {}).items():
            changes.append((v["alloc"] - old.get(f"{name}:{k}", 0), f"{name}: {k} (top level)"))
        for r in t.get("rows", []):
            changes.append((r["alloc"] - old.get(r["path"], 0), r["path"]))
    print(f"change since {base.name}:")
    print(report.table(["change", "folder"],
                       [[size(delta), label] for delta, label in sorted(changes, reverse=True)[:args.n]], text=True))


def cmd_trim(cfg, args) -> None:
    _scan_notes(cfg, _tables(cfg)[0])
    print(report.table(["on disk", "last change", "folder", "why listed"],
                       [[size(c["alloc"]), when(c["newest"])[:10], c["path"], c["why"]]
                        for c in trim.candidates(cfg)[:args.n]], text=True))


def cmd_report(cfg, args) -> None:
    for path in report.write_all(cfg, trim.candidates(cfg)):
        print("wrote", path)


def cmd_init(args) -> None:
    path = config_mod.write_starter(config_mod.Path(args.config))
    print(f"wrote {path}; edit its [[share]] entries, then run `labdisk tick` and `labdisk schedule`")


def cmd_schedule(cfg, args) -> None:
    """Print scrontab entries that run this installation of labdisk."""
    exe = shutil.which("labdisk") or f"{sys.executable} -m labdisk"
    where = f"--account={args.account} --partition={args.partition}" + (f" --qos={args.qos}" if args.qos else "")
    config = f" --config {args.config}" if args.config != str(config_mod.DEFAULT_CONFIG) else ""
    print(f"""# Add these to your Slurm cron table: run `scrontab -e` and paste them in.
# labdisk: quota readings, big-file recheck, alerts and reports every 15 minutes.
#SCRON {where} --job-name=labdisk_tick
#SCRON --time=00:10:00 --mem=1G --cpus-per-task=1
#SCRON --output={cfg.results}/tick.log --open-mode=append
*/15 * * * * {exe}{config} tick

# labdisk: scan due folders daily, for up to 23 hours with {cfg.readers} readers (one scan at a time).
#SCRON {where} --job-name=labdisk_scan
#SCRON --time=23:30:00 --mem=8G --cpus-per-task={cfg.readers}
#SCRON --output={cfg.results}/scan-%j.log
7 1 * * * {exe}{config} scan --hours 23""")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="labdisk", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=os.path.expanduser, default=str(config_mod.DEFAULT_CONFIG),
                        help="configuration file (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", metavar="command", help=argparse.SUPPRESS)
    sub.add_parser("help", description="show this help")
    sub.add_parser("status", description="how full each share is and how fast it is filling (default)")
    p = sub.add_parser("top", description="largest top-level folders of one share")
    p.add_argument("share")
    p.add_argument("-n", type=int, default=25)
    p = sub.add_parser("mine", description="your largest folders on every share")
    p.add_argument("-n", type=int, default=30)
    p = sub.add_parser("grew", description="what grew most over the last N days")
    p.add_argument("--days", type=float, default=7)
    p.add_argument("-n", type=int, default=25)
    p = sub.add_parser("trim", description="candidates to remove or move; never deletes")
    p.add_argument("-n", type=int, default=40)
    sub.add_parser("report", description="rewrite the Markdown reports from saved results")
    sub.add_parser("init", description="write a starter configuration file")
    p = sub.add_parser("schedule", description="print the scrontab entries that keep results current")
    p.add_argument("--account", required=True, help="Slurm account to charge")
    p.add_argument("--partition", required=True)
    p.add_argument("--qos")
    # Subcommands carry a description (shown by `labdisk <command> -h`) but no `help`, so the
    # main help screen shows only the examples in this module's docstring.
    sub.add_parser("tick", description="15-minute job: quota readings, big-file recheck, alerts, reports")
    p = sub.add_parser("scan", description="daily job: scan due folders for up to --hours (Slurm only)")
    p.add_argument("--hours", type=float, required=True)
    p.add_argument("--share", action="append", help="limit to these shares")
    args = parser.parse_args(argv)
    if args.command == "help":
        parser.print_help()
        return
    if args.command == "init":
        cmd_init(args)
        return
    cfg = config_mod.load(config_mod.Path(args.config))
    {"status": cmd_status, None: cmd_status, "top": cmd_top, "mine": cmd_mine, "grew": cmd_grew,
     "trim": cmd_trim, "report": cmd_report, "tick": cmd_tick, "scan": cmd_scan, "schedule": cmd_schedule}[args.command](cfg, args)


if __name__ == "__main__":
    main()
