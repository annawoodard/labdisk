"""The slow, continuous folder scan.

GPFS keeps no running total per folder, so totals are added up file by file. The work
is cut into units that readers share:

- a *tree* unit is one folder and everything below it;
- a *files* unit is only the files directly inside one folder.

A share starts as a files unit for its top level, a files unit for each top-level
folder, and a tree unit for each folder one level below that. A tree unit that held
more than `split_files` files on its last scan is split into a files unit plus a tree
unit per subfolder, because one reader adds up one folder and a single huge folder
otherwise keeps the whole pass waiting.

Each unit's result is appended to `units/<share>.jsonl` as soon as it finishes, so an
interrupted pass resumes from what is already recorded. A unit is due again after
`own_rescan_days` if it lies inside one of the owner's folders, otherwise after
`other_rescan_days`.

What is kept: totals for every top-level folder; a row for every folder of the owner's
holding at least `min_row_bytes`; and the owner's files of at least `big_file_bytes`.
Smaller folders are folded into their parents, so patient- or exam-level folders,
which are a few GB each, never become rows. A file with several hard links is counted
once per unit, and once across the share when the folder table is assembled.

Run only inside a Slurm allocation; it walks directory trees.
"""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import stat
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from .config import Config, Share
from .store import append_jsonl, read_json, read_jsonl, write_json


def uid_of(name: str) -> int:
    return pwd.getpwnam(name).pw_uid


class Throttle:
    """Keep one reader under `per_second` directory entries per second."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self.count = 0
        self.start = time.monotonic()

    def tick(self, n: int = 1) -> None:
        self.count += n
        if self.interval and self.count % 256 == 0:
            ahead = self.count * self.interval - (time.monotonic() - self.start)
            if ahead > 0:
                time.sleep(ahead)


def scan_unit(path: str, mode: str, own_inherited: bool, owner_uid: int, min_row: int, big_file: int,
              per_second: float) -> dict:
    """Add up one unit. Returns its totals, the owner's rows inside it, big files and hard links."""
    throttle = Throttle(per_second)
    seen: set[tuple[int, int]] = set()
    links: list[list[int]] = []
    rows: list[dict] = []
    big: list[dict] = []
    errors = 0

    def add_file(entry_path: str, st: os.stat_result, own: bool, totals: list) -> None:
        if st.st_nlink > 1:
            key = (st.st_dev, st.st_ino)
            if key in seen:
                return
            seen.add(key)
            links.append([st.st_dev, st.st_ino, st.st_blocks * 512, st.st_size])
        totals[0] += st.st_blocks * 512
        totals[1] += st.st_size
        totals[2] += 1
        totals[3] = max(totals[3], st.st_mtime)
        if own and max(st.st_blocks * 512, st.st_size) >= big_file:
            big.append({"path": entry_path, "alloc": st.st_blocks * 512, "size": st.st_size,
                        "mtime": st.st_mtime})

    def visit(folder: str, own_parent: bool, recurse: bool) -> list:
        nonlocal errors
        totals = [0, 0, 0, 0.0]  # allocated bytes, apparent bytes, files, newest change
        try:
            folder_st = os.lstat(folder)
            entries = list(os.scandir(folder))
        except OSError:
            errors += 1
            return totals
        own = own_parent or folder_st.st_uid == owner_uid
        totals[0] += folder_st.st_blocks * 512  # a directory's own blocks count against the quota too
        totals[3] = folder_st.st_mtime
        for entry in entries:
            throttle.tick()
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                errors += 1
                continue
            if stat.S_ISDIR(st.st_mode):
                if recurse:
                    child = visit(entry.path, own, True)
                    for i in range(3):
                        totals[i] += child[i]
                    totals[3] = max(totals[3], child[3])
            elif stat.S_ISREG(st.st_mode):
                add_file(entry.path, st, own, totals)
        if recurse and own and totals[0] >= min_row:
            rows.append({"path": folder, "alloc": totals[0], "size": totals[1], "files": totals[2],
                         "newest": totals[3], "owner_uid": folder_st.st_uid})
        return totals

    if mode == "file":  # a single file sitting at the top of a share
        totals = [0, 0, 0, 0.0]
        try:
            add_file(path, os.lstat(path), own_inherited, totals)
        except OSError:
            errors += 1
    else:
        totals = visit(path, own_inherited, recurse=(mode == "tree"))
    return {"alloc": totals[0], "size": totals[1], "files": totals[2], "newest": totals[3], "rows": rows,
            "big": big, "links": links, "errors": errors}


def list_units(share: Share, splits: set[str], max_depth: int) -> list[tuple[str, str]]:
    """The share's current units, from a fresh listing of its top two levels and of split folders."""
    units: list[tuple[str, str]] = [(str(share.path), "files")]

    def children(folder: str) -> list[os.DirEntry]:
        try:
            return list(os.scandir(folder))
        except OSError:
            return []

    def place(folder: str, depth: int) -> None:
        if folder in splits and depth < max_depth:
            units.append((folder, "files"))
            for entry in children(folder):
                if entry.is_dir(follow_symlinks=False):
                    place(entry.path, depth + 1)
        else:
            units.append((folder, "tree"))

    for top in children(str(share.path)):
        if top.is_dir(follow_symlinks=False):
            units.append((top.path, "files"))
            for entry in children(top.path):
                if entry.is_dir(follow_symlinks=False):
                    place(entry.path, 2)
        elif top.is_file(follow_symlinks=False):
            units.append((top.path, "file"))
    return units


def latest_units(config: Config, share: Share) -> dict[str, dict]:
    """The most recent record for each unit, keyed by 'mode:path'."""
    out: dict[str, dict] = {}
    for record in read_jsonl(config.results / "units" / f"{share.name}.jsonl"):
        out[f"{record['mode']}:{record['path']}"] = record
    return out


def is_own(path: str, share: Share, owner_uid: int, cache: dict[str, bool]) -> bool:
    """True if this folder or any folder above it (inside the share) belongs to the owner."""
    if path in cache:
        return cache[path]
    root = str(share.path)
    if path == root or not path.startswith(root):
        cache[path] = False
        return False
    parent_own = is_own(os.path.dirname(path), share, owner_uid, cache)
    try:
        own = parent_own or os.lstat(path).st_uid == owner_uid
    except OSError:
        own = parent_own
    cache[path] = own
    return own


def run(config: Config, hours: float, shares: list[str] | None = None) -> bool:
    """Scan due units, owner's first, until `hours` have passed or nothing is due.

    Returns False without scanning if another scan is running.
    """
    if not os.environ.get("SLURM_JOB_ID"):
        raise SystemExit("labdisk scan walks directory trees; run it inside a Slurm job")
    # One scan at a time: two would rescan the same units and interleave their records.
    # The lock is released by the kernel when the process ends, so it cannot go stale.
    config.results.mkdir(parents=True, exist_ok=True)
    lock = open(config.results / "scan.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another labdisk scan holds the lock; exiting", flush=True)
        return False
    deadline = time.time() + hours * 3600
    owner_uid = uid_of(config.owner)
    per_reader = config.max_files_per_second / config.readers
    state_path = config.results / "scan_state.json"
    state = read_json(state_path, {"splits": {}})
    todo = []
    for share in config.shares:
        if shares and share.name not in shares:
            continue
        splits = set(state["splits"].get(share.name, []))
        done = latest_units(config, share)
        own_cache: dict[str, bool] = {}
        layout = list_units(share, splits, config.max_split_depth)
        state.setdefault("layout", {})[share.name] = [f"{m}:{p}" for p, m in layout]
        write_json(state_path, state)
        for path, mode in layout:
            own = is_own(path if mode != "file" else os.path.dirname(path), share, owner_uid, own_cache)
            age_days = (time.time() - done.get(f"{mode}:{path}", {}).get("scanned_at", 0)) / 86400
            due_after = config.own_rescan_days if own else config.other_rescan_days
            if age_days >= due_after:
                todo.append((not own, -age_days, share.name, path, mode, own))
    todo.sort()
    print(f"{len(todo)} units due", flush=True)
    with ProcessPoolExecutor(max_workers=config.readers) as pool:
        pending = {}
        queue = iter(todo)
        while True:
            while len(pending) < config.readers and time.time() < deadline:
                item = next(queue, None)
                if item is None:
                    break
                _, _, share_name, path, mode, own = item
                future = pool.submit(scan_unit, path, mode, own, owner_uid, config.min_row_bytes,
                                     config.big_file_bytes, per_reader)
                pending[future] = (share_name, path, mode)
            if not pending:
                break
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                share_name, path, mode = pending.pop(future)
                result = future.result()
                result.update(path=path, mode=mode, scanned_at=time.time())
                append_jsonl(config.results / "units" / f"{share_name}.jsonl", result)
                if mode == "tree" and result["files"] > config.split_files:
                    state["splits"].setdefault(share_name, [])
                    if path not in state["splits"][share_name]:
                        state["splits"][share_name].append(path)
                        write_json(state_path, state)
    compact(config)
    return True


def compact(config: Config) -> None:
    """Keep only the latest record of each unit that still exists in the current layout."""
    for share in config.shares:
        path = config.results / "units" / f"{share.name}.jsonl"
        if not path.exists():
            continue
        keep = current_records(config, share)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            for record in keep:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        os.replace(tmp, path)


def current_records(config: Config, share: Share) -> list[dict]:
    """The latest record of each unit in the current layout.

    When a big folder has just been split and some of its new units have not been scanned
    yet, the folder's last whole-folder record stands in for all of them, so the table
    never loses or double-counts that folder while the split pass is under way.
    """
    records = latest_units(config, share)
    layout = read_json(config.results / "scan_state.json", {}).get("layout", {}).get(share.name)
    if layout is None:
        return list(records.values())
    fallback: set[str] = set()
    for key in layout:
        if key in records:
            continue
        folder = key.split(":", 1)[1]
        while folder != str(share.path) and folder != "/":
            if f"tree:{folder}" in records and f"tree:{folder}" not in layout:
                fallback.add(folder)
                break
            folder = os.path.dirname(folder)

    def covered(path: str) -> bool:
        return any(path == f or path.startswith(f + os.sep) for f in fallback)

    out = [records[k] for k in layout if k in records and not covered(k.split(":", 1)[1])]
    out += [records[f"tree:{f}"] for f in fallback]
    return out


def folder_table(config: Config, share: Share) -> dict:
    """Assemble the share's folder table from its latest unit records."""
    owner_uid = uid_of(config.owner)
    units = {f"{r['mode']}:{r['path']}": r for r in current_records(config, share)}
    root = str(share.path)
    top: dict[str, dict] = {}
    agg: dict[str, list] = {}
    rows: dict[str, dict] = {}
    big: list[dict] = []
    seen_links: set[tuple[int, int]] = set()
    errors = 0
    oldest = None
    for record in units.values():
        alloc, size, files = record["alloc"], record["size"], record["files"]
        for dev, ino, link_alloc, link_size in record["links"]:
            if (dev, ino) in seen_links:
                alloc -= link_alloc
                size -= link_size
                files -= 1
            seen_links.add((dev, ino))
        errors += record["errors"]
        oldest = record["scanned_at"] if oldest is None else min(oldest, record["scanned_at"])
        big.extend(record["big"])
        for row in record["rows"]:
            rows[row["path"]] = row
        path = record["path"]
        # The folders above this unit whose totals it adds to: from the top-level folder down
        # to the unit's own folder for files units, or to its parent for tree units.
        rel = os.path.relpath(path, root)
        parts = [] if rel == "." else rel.split(os.sep)
        if record["mode"] == "file":
            parts = parts[:-1]
        upto = len(parts) if record["mode"] == "files" else len(parts) - 1
        name = parts[0] if parts else "(top-level files)"
        entry = top.setdefault(name, {"alloc": 0, "size": 0, "files": 0, "newest": 0.0, "scanned_at": None,
                                      "errors": 0})
        entry["alloc"] += alloc
        entry["size"] += size
        entry["files"] += files
        entry["newest"] = max(entry["newest"], record["newest"])
        entry["errors"] += record["errors"]
        entry["scanned_at"] = (record["scanned_at"] if entry["scanned_at"] is None
                               else min(entry["scanned_at"], record["scanned_at"]))
        for depth in range(2, upto + 1):  # depth 1 is the top-level table
            folder = os.path.join(root, *parts[:depth])
            a = agg.setdefault(folder, [0, 0, 0, 0.0])
            a[0] += alloc
            a[1] += size
            a[2] += files
            a[3] = max(a[3], record["newest"])
    own_cache: dict[str, bool] = {}
    for name, entry in top.items():
        folder = os.path.join(root, name)
        try:
            st = os.lstat(folder)
            entry["owner"] = pwd.getpwuid(st.st_uid).pw_name
        except (OSError, KeyError):
            entry["owner"] = "unknown"
        entry["readable"] = os.access(folder, os.R_OK | os.X_OK) if os.path.isdir(folder) else True
        if is_own(folder, share, owner_uid, own_cache) and entry["alloc"] >= config.min_row_bytes:
            rows.setdefault(folder, {"path": folder, "alloc": entry["alloc"], "size": entry["size"],
                                     "files": entry["files"], "newest": entry["newest"]})
    for folder, (alloc, size, files, newest) in agg.items():
        if alloc >= config.min_row_bytes and is_own(folder, share, owner_uid, own_cache):
            rows[folder] = {"path": folder, "alloc": alloc, "size": size, "files": files, "newest": newest}
    return {"share": share.name, "top": top, "rows": sorted(rows.values(), key=lambda r: -r["alloc"]),
            "big": sorted(big, key=lambda b: -b["alloc"]), "units": len(units), "errors": errors,
            "oldest_scan": oldest}


def snapshot(config: Config, rotate: bool) -> None:
    """Rebuild the folder tables from the unit records saved so far.

    With `rotate`, which a finished scan pass uses, the tables being replaced become
    `previous.json`, the baseline for "change since previous scan", and one copy per week
    is kept for 26 weeks. Without it (the 15-minute tick), only `latest.json` is refreshed,
    so a long pass shows its progress as it goes.
    """
    day = time.strftime("%Y-%m-%d")
    out = config.results / "snapshots"
    tables = {share.name: folder_table(config, share) for share in config.shares}
    if not rotate:
        write_json(out / "latest.json", tables)
        return
    if (out / "latest.json").exists():
        os.replace(out / "latest.json", out / "previous.json")
    write_json(out / "latest.json", tables)
    if time.strftime("%u") == "1" or not any(out.glob("week-*.json")):
        write_json(out / f"week-{day}.json", tables)
    for old in sorted(out.glob("week-*.json"))[:-26]:
        old.unlink()


def recheck_big_files(config: Config) -> list[dict]:
    """Re-measure the owner's big files; returns those that grew since the last check."""
    latest = read_json(config.results / "snapshots" / "latest.json", {})
    previous = read_json(config.results / "big_files_last.json", {})
    now, grew = {}, []
    for table in latest.values():
        for item in table.get("big", []):
            try:
                st = os.stat(item["path"])
            except OSError:
                continue
            alloc = st.st_blocks * 512
            now[item["path"]] = alloc
            before = previous.get(item["path"])
            if before is not None and alloc > before:
                grew.append({"path": item["path"], "alloc": alloc, "grew": alloc - before})
    write_json(config.results / "big_files_last.json", now)
    return sorted(grew, key=lambda g: -g["grew"])

