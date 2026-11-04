"""A summary of how named data folders are laid out, safe to review in place of their file names.

Data folders are often named after patients, exams or dates, so a list of their files
cannot be shown to anyone who may not see those. This walks only the folders it is
given, file by file, and writes one short text file in which:

- every name is replaced by its shape: `A` for a run of capital letters, `a` for
  lower-case letters, `9` for digits, `{n}` for a run of n, other characters as they
  are. `IM00001.dcm` becomes `A{2}9{5}.a{3}`; `SMITH^JOHN` becomes `A{5}^A{4}`;
- siblings that share a shape become one line with a count, and their contents merge,
  so a folder of 2,000 patient folders is one line, followed by what a patient folder
  typically holds;
- a name is shown as written only when it appears in at least `literal_min_folders`
  different folders (`DICOM`, `T1_pre`), contains a letter, and has no run of four or
  more digits. Names that recur across many folders describe structure, while a
  patient or exam name does not recur; a long number may be a date or an identifier,
  which can identify someone however often it recurs. File extensions are shown on
  the recurrence rule.

A person reads the result and decides whether it can be released; this produces it,
nothing more. It writes only to the path it is given, readable by its owner only, and
never into labdisk's results or reports.

On a machine with Slurm it runs only inside a job, because it walks directory
trees and a login node is shared; elsewhere it runs directly.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

from .report import size

NAMES_TRACKED = 5000  # distinct names counted per shape; a name first seen after this is never shown as written
RUN = re.compile(r"[A-Z]+|[a-z]+|[0-9]+|[^A-Za-z0-9]")


def shape(name: str) -> str:
    out = []
    for run in RUN.findall(name):
        mark = "A" if run[0].isupper() else "a" if run[0].islower() else "9" if run[0].isdigit() else None
        out.append(run if mark is None else mark if len(run) == 1 else f"{mark}{{{len(run)}}}")
    return "".join(out)


def extension(name: str) -> str | None:
    stem, dot, ext = name.rpartition(".")
    return "." + ext.lower() if dot and stem and 0 < len(ext) <= 8 and ext.isalnum() else None


LONG_NUMBER = re.compile(r"[0-9]{4,}")


def may_show(name: str) -> bool:
    """A name with no letters, or with four or more digits in a row, may hold a date or an
    identifier, which can identify someone however often it recurs, so it is never shown."""
    return any(c.isalpha() for c in name) and not LONG_NUMBER.search(name)


def plural(n: int, noun: str) -> str:
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


def new_node() -> dict:
    return {"count": 0, "bytes": 0, "names": Counter(), "exts": Counter(), "unreadable": 0, "children": {}}


def add_entries(folder: str, node: dict, descend) -> None:
    """Add the entries of `folder` to `node`, the shape node `folder` belongs to.

    `descend(path, child)` is called for each subfolder with the shape node it joins.
    """
    try:
        entries = list(os.scandir(folder))
    except OSError:
        node["unreadable"] += 1
        return
    exts_here: dict[str, set] = {}
    for entry in entries:
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            node["unreadable"] += 1
            continue
        is_dir = stat.S_ISDIR(st.st_mode)
        token = shape(entry.name) + ("/" if is_dir else "@" if stat.S_ISLNK(st.st_mode) else "")
        child = node["children"].setdefault(token, new_node())
        child["count"] += 1
        names = child["names"]
        if entry.name in names or len(names) < NAMES_TRACKED:
            names[entry.name] += 1
        if is_dir:
            descend(entry.path, child)
        else:
            child["bytes"] += st.st_size
            ext = extension(entry.name)
            if ext:
                exts_here.setdefault(token, set()).add(ext)
    for token, exts in exts_here.items():  # an extension counts once per folder it appears in
        node["children"][token]["exts"].update(exts)


def walk(folder: str, node: dict) -> None:
    add_entries(folder, node, walk)


def walk_one(folder: str) -> dict:
    node = new_node()
    walk(folder, node)
    return node


def merge(into: dict, other: dict) -> None:
    for key in ("count", "bytes", "unreadable"):
        into[key] += other[key]
    for key in ("names", "exts"):
        for name, n in other[key].items():
            if name in into[key] or len(into[key]) < NAMES_TRACKED:
                into[key][name] += n
    for token, child in other["children"].items():
        if token in into["children"]:
            merge(into["children"][token], child)
        else:
            into["children"][token] = child


def walk_root(root: str, readers: int) -> dict:
    """The shape tree of one data folder, with its subfolders shared among `readers` processes."""
    node = new_node()
    pending: list[tuple[str, dict]] = []
    add_entries(root, node, lambda path, child: pending.append((path, child)))
    with ProcessPoolExecutor(max_workers=readers) as pool:
        for (_, child), below in zip(pending, pool.map(walk_one, [p for p, _ in pending], chunksize=16)):
            merge(child, below)
    return node


def totals(node: dict) -> tuple[int, int, int]:
    """(folders, files, bytes) below a node."""
    folders = files = nbytes = 0
    for token, child in node["children"].items():
        sub = totals(child)
        if token.endswith("/"):
            folders += child["count"] + sub[0]
            files += sub[1]
        else:
            files += child["count"]
        nbytes += child["bytes"] + sub[2]
    return folders, files, nbytes


def render(node: dict, depth: int, literal_min: int, max_shapes: int, lines: list[str]) -> None:
    pad = "  " * depth
    ranked = sorted(node["children"].items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    for token, child in ranked[:max_shapes]:
        is_dir = token.endswith("/")
        if is_dir:
            _, files, nbytes = totals(child)
            what = f"{plural(child['count'], 'folder')}, {plural(files, 'file')}, {size(nbytes)}"
        else:
            what = f"{plural(child['count'], 'file')}, {size(child['bytes'])}"
        notes = []
        literal = [(n, c) for n, c in child["names"].most_common() if c >= literal_min and may_show(n)]
        if literal:
            notes.append("named " + ", ".join(f"{n} in {c:,} folders" for n, c in literal[:8]))
        exts = [(e, c) for e, c in child["exts"].most_common() if c >= literal_min]
        if exts:
            notes.append("extension " + ", ".join(f"{e} in {c:,} folders" for e, c in exts[:4]))
        if child["unreadable"]:
            notes.append(f"{child['unreadable']:,} unreadable")
        lines.append(f"{pad}{token}  {what}" + (f"  ({'; '.join(notes)})" if notes else ""))
        if is_dir:
            render(child, depth + 1, literal_min, max_shapes, lines)
    rest = ranked[max_shapes:]
    if rest:
        lines.append(f"{pad}... {len(rest):,} more shapes, {sum(c['count'] for _, c in rest):,} entries")


def run(folders: list[str], out: str, readers: int, literal_min: int, max_shapes: int) -> int:
    if shutil.which("sbatch") and not os.environ.get("SLURM_JOB_ID"):
        raise SystemExit("labdisk layout walks directory trees; on a Slurm cluster, run it inside a job")
    lines = [
        "# Layout of the data folders named below, with every name replaced by its shape",
        "#",
        "# A = capital letters, a = lower-case letters, 9 = digits, {n} = n of them; other",
        "# characters as written. / ends a folder, @ marks a link. A name or extension is",
        f"# shown as written only when it appears in at least {literal_min:,} different folders",
        "# and has a letter and no run of four or more digits.",
        "# Before releasing this, check that every name shown as written is safe to share.",
        "",
    ]
    for folder in folders:
        root = os.path.realpath(folder)
        node = walk_root(root, readers)
        folders_n, files_n, nbytes = totals(node)
        lines.append(f"{root}/  {plural(folders_n, 'folder')}, {plural(files_n, 'file')}, {size(nbytes)}"
                     + (f"  ({node['unreadable']:,} unreadable)" if node["unreadable"] else ""))
        render(node, 1, literal_min, max_shapes, lines)
        lines.append("")
    tmp = out + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(tmp, out)
    return len(lines)
