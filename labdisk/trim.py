"""Candidates to trim among the owner's folders. Nothing here deletes anything.

A folder is listed when its name matches one of the configured kinds (caches, git
worktrees, package caches) or nothing inside it has changed for `stale_days`. Each line
says why; the folder's owner decides what to do with it.
"""

from __future__ import annotations

import fnmatch
import os
import time

from .config import Config
from .store import read_json


def _matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern.lower()) for pattern in patterns)


def candidates(config: Config) -> list[dict]:
    latest = read_json(config.results / "snapshots" / "latest.json", {})
    now = time.time()
    kinds = [(config.cache_names, "named like a cache; check whether anything current reads it"),
             (config.worktree_names, "git worktrees; remove those no queued or running job uses"),
             (config.package_cache_names, "a package cache; rebuilt on demand")]
    out: dict[str, dict] = {}
    for table in latest.values():
        for row in table.get("rows", []):  # largest first, so a folder comes before anything inside it
            path, name = row["path"], os.path.basename(row["path"]).lower()
            if any(path.startswith(listed + os.sep) for listed in out):
                continue  # one line per cache or worktree folder, not one per subfolder
            why = [reason for patterns, reason in kinds if _matches(name, patterns)]
            if now - row["newest"] > config.stale_days * 86400:
                why.append(f"nothing inside changed in {int((now - row['newest']) / 86400)} days")
            if why:
                out[path] = {"path": path, "alloc": row["alloc"], "newest": row["newest"], "why": "; ".join(why)}
    return sorted(out.values(), key=lambda c: -c["alloc"])
