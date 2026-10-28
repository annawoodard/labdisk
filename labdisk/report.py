"""Markdown reports: one private review report for the owner, one report per lab share.

A lab report covers its own share only. It lists every top-level folder with its owner,
size and recent change; the owner's folders appear there as top-level totals only, and
their detailed rows stay in the private report.
"""

from __future__ import annotations

import grp
import os
import time
from pathlib import Path

from .config import Config, Share
from .quota import outlook
from .store import read_json, write_text

# Sizes print in binary units (TiB, GiB) unless the config asks for decimal (TB, GB);
# `use_units` sets this once per run, before anything is printed or written.
_UNITS = {"decimal": (10**12, "TB", 10**9, "GB"), "binary": (2**40, "TiB", 2**30, "GiB")}
_unit = _UNITS["binary"]


def use_units(name: str) -> None:
    global _unit
    if name not in _UNITS:
        raise ValueError(f"units must be one of {sorted(_UNITS)}, not {name!r}")
    _unit = _UNITS[name]


def size(n: float | None) -> str:
    if n is None:
        return "—"
    big, big_name, small, small_name = _unit
    return f"{n / big:,.2f} {big_name}" if abs(n) >= big else f"{n / small:,.1f} {small_name}"


def rate(n: float | None) -> str:
    return "—" if n is None else f"{'+' if n >= 0 else '−'}{size(abs(n))}/h"


def hours(h: float | None) -> str:
    if h is None:
        return "not filling"
    return f"{h:.0f} h" if h < 48 else f"{h / 24:.0f} days"


def when(t: float | None) -> str:
    return "never" if not t else time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def table(header: list[str], rows: list[list], text: bool = False) -> str:
    """A Markdown table for the reports, or with `text` an aligned one for the terminal.

    In the terminal, backticks are dropped and columns whose cells all start with a digit
    or a sign (sizes, counts, changes) are right-aligned.
    """
    if not text:
        out = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
        out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
        return "\n".join(out)
    cells = [[str(c).replace("`", "") for c in row] for row in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(header)]
    numeric = [bool(cells) and all(r[i][:1] in "0123456789+-−—" for r in cells) for i in range(len(header))]

    def line(values: list[str]) -> str:
        return "  ".join(v.rjust(w) if n else v.ljust(w) for v, w, n in zip(values, widths, numeric)).rstrip()

    return "\n".join([line(header), line(["─" * w for w in widths])] + [line(r) for r in cells])


def warnings(config: Config, share: Share, look: dict) -> list[str]:
    """The lines that also go out as alerts; see alert.py."""
    if not look:
        return []
    r = look["reading"]
    out = []
    if look.get("hours_to_hard_1h") is not None and look["hours_to_hard_1h"] < 24:
        out.append(f"URGENT: at the last hour's rate ({rate(look['per_hour_1h'])}) {share.name} reaches its "
                   f"hard limit in about {hours(look['hours_to_hard_1h'])}; {size(look['left'])} left.")
    if r.used > r.soft:
        out.append(f"{share.name} is over its soft quota ({size(r.used)} of {size(r.soft)}); grace left: "
                   f"{r.grace}. After the grace period, writes fail until usage is back under the soft quota.")
    elif r.used >= config.soft_warn_fraction * r.soft:
        out.append(f"{share.name} is at {r.used / r.soft:.0%} of its soft quota.")
    if look.get("hours_to_hard_24h") is not None and look["hours_to_hard_24h"] < 24 * 7:
        out.append(f"At the last day's rate ({rate(look['per_hour_24h'])}) {share.name} reaches its hard limit "
                   f"in about {hours(look['hours_to_hard_24h'])}.")
    if r.file_hard and r.files >= config.soft_warn_fraction * r.file_hard:
        out.append(f"{share.name} holds {r.files:,} files of its {r.file_hard:,} limit.")
    return out


def first_scan_eta(config: Config, latest: dict) -> float | None:
    """Hours until the first scan of every share finishes, at the scan's recent pace.

    The files still to read are each unfinished share's file count from its quota reading
    minus the files measured so far; the pace is files measured per hour over the last
    few hours. None when the scan is finished or has no recent pace to go on.
    """
    remaining, recent, since = 0, 0, None
    for share in config.shares:
        t = latest.get(share.name) or {}
        recent += t.get("recent_files", 0)
        if t.get("recent_since"):
            since = t["recent_since"] if since is None else min(since, t["recent_since"])
        if t and t.get("measured_units", 0) >= t.get("layout_units", 0):
            continue
        look = outlook(config, share.name)
        if not look:
            return None  # a share with no quota file count: nothing to estimate from
        remaining += max(look["reading"].files - t.get("files_measured", 0), 0)
    if not remaining:
        return None
    if not recent or since is None or time.time() - since < 600:
        return None
    return remaining / (recent / ((time.time() - since) / 3600))


def eta_text(hours_left: float | None) -> str:
    if hours_left is None:
        return "not enough progress yet to estimate when it finishes"
    done = time.strftime("%a %b %-d %H:%M", time.localtime(time.time() + hours_left * 3600))
    return (f"about {hours(hours_left)} of scanning left at its recent pace: around {done} if it runs "
            "without a break (maintenance windows and job time limits add to that)")


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] + "."


def scan_progress(table: dict | None) -> str | None:
    """How far the first scan of a share has got, or None when it is done."""
    if not table:
        return "not reached yet"
    measured, total = table.get("measured_units", 0), table.get("layout_units", 0)
    return f"{measured:,} of {total:,} parts measured" if measured < total else None


def first_scan_lines(config: Config, latest: dict, shares: list[Share] | None = None) -> list[str]:
    """The first-scan block: one estimate, then each unfinished share; empty when all are done."""
    rows = [(s.name, p) for s in (shares or config.shares) if (p := scan_progress(latest.get(s.name)))]
    if not rows:
        return []
    width = max(len(name) for name, _ in rows)
    return ([f"  {name.ljust(width)}  {progress}" for name, progress in rows]
            + ["", _sentence(eta_text(first_scan_eta(config, latest)))])


def quota_header(config: Config, mine: bool) -> list[str]:
    """The quota table's columns; `mine` adds the owner's own usage, which lab reports leave out."""
    return (["share", "used"] + ([f"{config.owner}'s files"] if mine else [])
            + ["soft quota", "hard limit", "left before hard limit", "last hour", "last day", "files"])


def quota_rows(config: Config, shares: list[Share], mine: bool) -> list[list]:
    rows = []
    for share in shares:
        look = outlook(config, share.name)
        if not look:
            rows.append([share.name, "no quota configured" if not share.quota else "no reading yet"]
                        + [""] * (7 if mine else 6))
            continue
        r = look["reading"]
        files = f"{r.files / 1e6:.2f} M" + (f" of {r.file_hard / 1e6:.2f} M" if r.file_hard else "")
        own = [size(r.owner_used)] if mine else []
        rows.append([share.name, size(r.used)] + own + [size(r.soft), size(r.hard), size(look["left"]),
                     rate(look["per_hour_1h"]), rate(look["per_hour_24h"]), files])
    return rows


def quota_note() -> str:
    units = ("decimal units (1 TB = 1,000 GB)" if _unit[1] == "TB"
             else "binary units (1 TiB = 1,024 GiB = 1.10 TB)")
    return ("The soft quota is the level past which writes keep working only for a grace period; the hard "
            f"limit is where writes fail at once. Sizes are what the quota charges, in {units}.")


def change(path_key: str, now: dict, before: dict | None) -> str:
    if not before or path_key not in before:
        return "—"
    return size(now["alloc"] - before[path_key]["alloc"])


def top_table(table_now: dict, table_before: dict | None, limit: int | None = None, text: bool = False) -> str:
    before = (table_before or {}).get("top")
    items = sorted(table_now["top"].items(), key=lambda kv: -kv[1]["alloc"])[:limit]
    return table(["folder", "owner", "on disk", "change since previous scan", "files", "last change",
                  "scanned"],
                 [[f"`{name}`" + ("" if e.get("readable", True) else " (unreadable)")
                  + (f" ({e['errors']:,} entries unreadable)" if e.get("errors") else ""), e.get("owner", "—"),
                   size(e["alloc"]), change(name, e, before), f"{e['files']:,}", when(e["newest"])[:10],
                   when(e["scanned_at"])[:10]] for name, e in items], text=text)


def lab_report(config: Config, share: Share, latest: dict, previous: dict) -> str:
    t = latest.get(share.name)
    look = outlook(config, share.name)
    lines = [f"# Storage report: {share.name}", "",
             f"Updated {when(time.time())} by `labdisk`, run by {config.owner}. This report covers "
             f"`{share.path}` only and is refreshed every 15 minutes; folder sizes come from a slower scan.", ""]
    for w in warnings(config, share, look):
        lines += [f"> **{w}**", ""]
    lines += ["## Quota", "", quota_note(), "", table(quota_header(config, False), quota_rows(config, [share], False)), ""]
    if t:
        total = sum(e["alloc"] for e in t["top"].values())
        if scan_progress(t):
            summary = (f"**The first scan is still running**: {scan_progress(t)}, so these totals are incomplete "
                       f"({size(total)} measured so far). {_sentence(eta_text(first_scan_eta(config, latest)))}")
        else:
            unattributed = look["reading"].used - total if look else None
            summary = (f"Scanned folders add up to {size(total)}"
                       + (f"; {size(unattributed)} of the quota is in folders the scan could not read."
                          if unattributed is not None else "."))
        lines += ["## Top-level folders", "",
                  "One row per top-level folder; each folder is re-measured at least weekly, so a row can be up "
                  f"to a week old (see 'scanned'). {summary}", "", top_table(t, previous.get(share.name)), ""]
    else:
        lines += ["No folder scan has finished yet.", ""]
    return "\n".join(lines)


def private_report(config: Config, latest: dict, previous: dict, trim: list[dict]) -> str:
    lines = ["# Storage on all configured shares", "", f"Updated {when(time.time())} by `labdisk`.", ""]
    for share in config.shares:
        for w in warnings(config, share, outlook(config, share.name)):
            lines += [f"> **{w}**", ""]
    lines += ["## Quotas", "", quota_note(), "", table(quota_header(config, True), quota_rows(config, config.shares, True)), ""]
    block = first_scan_lines(config, latest)
    if block:
        lines += ["## First scan in progress: folder totals are incomplete", ""] + block + [""]
    for share in config.shares:
        t = latest.get(share.name)
        lines += [f"## {share.name}: `{share.path}`", ""]
        progress = scan_progress(t)
        if progress:
            lines += [f"> **First scan still running here: {progress}; totals below are incomplete.**", ""]
        if not t:
            continue
        lines += ["Largest top-level folders:", "", top_table(t, previous.get(share.name), limit=20), ""]
        before = {r["path"]: r for r in (previous.get(share.name) or {}).get("rows", [])}
        rows = [[f"`{os.path.relpath(r['path'], share.path)}`", size(r["alloc"]),
                 change(r["path"], r, before), f"{r['files']:,}", when(r["newest"])[:10]]
                for r in t["rows"][:40]]
        if rows:
            lines += [f"Your folders of {size(config.min_row_bytes)} or more (largest 40 of {len(t['rows'])}):", "",
                      table(["folder", "on disk", "change since previous scan", "files", "last change"], rows), ""]
    if trim:
        lines += ["## Candidates to trim", "", "Listed, never deleted. Each needs a decision by its owner.", "",
                  table(["folder", "on disk", "last change", "why listed"],
                        [[f"`{c['path']}`", size(c["alloc"]), when(c["newest"])[:10], c["why"]] for c in trim]),
                  ""]
    return "\n".join(lines)


def write_all(config: Config, trim: list[dict]) -> list[Path]:
    snapshots = config.results / "snapshots"
    latest = read_json(snapshots / "latest.json", {})
    previous = read_json(snapshots / "previous.json", {})
    written = []
    path = config.results / "report.md"
    write_text(path, private_report(config, latest, previous, trim))
    written.append(path)
    for share in config.shares:
        if not share.group:
            continue
        if not config.publish_lab_reports:
            path = config.results / "lab_report_previews" / f"{share.name}.md"
            path.parent.mkdir(mode=0o700, exist_ok=True)
            write_text(path, lab_report(config, share, latest, previous))
            written.append(path)
            continue
        folder = share.path / "storage_report"
        folder.mkdir(exist_ok=True)
        gid = grp.getgrnam(share.group).gr_gid
        os.chown(folder, -1, gid)
        os.chmod(folder, 0o2750)
        path = folder / "README.md"
        write_text(path, lab_report(config, share, latest, previous), mode=0o640)
        os.chown(path, -1, gid)
        written.append(path)
    return written
