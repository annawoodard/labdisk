"""Candidates to trim among the owner's folders. Nothing here deletes anything.

Each rule says why a folder is listed; the folder's owner decides what to do with it.
"""

from __future__ import annotations

import os
import re
import time

from .config import Config
from .store import read_json

CAP_LINE = re.compile(r"^\s*Size cap:\s*\S", re.IGNORECASE | re.MULTILINE)


def has_size_cap(folder: str) -> bool:
    readme = os.path.join(folder, "README.md")
    try:
        with open(readme, errors="replace") as handle:
            return bool(CAP_LINE.search(handle.read(200_000)))
    except OSError:
        return False


def candidates(config: Config) -> list[dict]:
    latest = read_json(config.results / "snapshots" / "latest.json", {})
    now = time.time()
    out: dict[str, dict] = {}
    for table in latest.values():
        rows = table.get("rows", [])
        paths = {r["path"] for r in rows}
        for row in rows:
            path, name = row["path"], os.path.basename(row["path"]).lower()
            # Only the outermost listed folder of a nested run of matches, so one cache is one line.
            parent_listed = any(path.startswith(p + os.sep) for p in paths if p in out)
            if parent_listed:
                continue
            why = []
            if "cache" in name and not has_size_cap(path):
                why.append("a cache whose README.md declares no `Size cap:`")
            if name in ("worktrees",) or name.endswith("-worktrees"):
                why.append("git worktrees; remove those no queued or running job uses")
            if name in ("pkgs", ".cache", "pip-cache", "uv-cache", "conda-pkgs"):
                why.append("package cache; rebuilt on demand")
            if now - row["newest"] > config.stale_days * 86400:
                why.append(f"nothing inside changed in {int((now - row['newest']) / 86400)} days")
            if why:
                out[path] = {"path": path, "alloc": row["alloc"], "newest": row["newest"], "why": "; ".join(why)}
    return sorted(out.values(), key=lambda c: -c["alloc"])
