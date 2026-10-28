"""Quota readings: one `mmlsquota` call per share, appended to a history file.

A reading takes under a second, so it can run every 15 minutes. The history is what
says how fast a share is filling and when it will be full.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import asdict, dataclass

from .config import Config, Share
from .store import append_jsonl, read_jsonl

GIB = 2**30


@dataclass
class Reading:
    share: str
    at: float  # unix time
    used: int  # bytes charged against the quota (on disk, after any compression)
    soft: int
    hard: int
    grace: str  # "none", or how long until the soft quota starts refusing writes, e.g. "2 days"
    files: int
    file_soft: int  # 0 means no file-count limit
    file_hard: int


def read_quota(share: Share) -> Reading:
    """Parse the last line of `mmlsquota ... --block-size G`, which is in GiB."""
    args = ["mmlsquota", *share.quota[:-1], "--block-size", "G", share.quota[-1]]
    out = subprocess.run(args, capture_output=True, text=True, timeout=120)
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    if out.returncode != 0 or len(lines) < 3:
        raise RuntimeError(f"{' '.join(args)} failed: {out.stderr.strip() or out.stdout.strip()}")
    fields = lines[-1].split()
    bar = fields.index("|")
    block, files = fields[:bar], fields[bar + 1:]
    # block: filesystem type used soft hard in_doubt grace...; grace is "none" or e.g. "2 days"
    grace = "none" if block[6] == "none" else " ".join(block[6:8])
    return Reading(share=share.name, at=time.time(), used=int(block[2]) * GIB, soft=int(block[3]) * GIB,
                   hard=int(block[4]) * GIB, grace=grace, files=int(files[0]), file_soft=int(files[1]),
                   file_hard=int(files[2]))


def sample(config: Config) -> list[Reading]:
    readings = [read_quota(share) for share in config.shares if share.quota]
    for reading in readings:
        append_jsonl(config.results / "quota.jsonl", asdict(reading))
    return readings


def history(config: Config, share: str, since: float = 0.0) -> list[Reading]:
    return [Reading(**r) for r in read_jsonl(config.results / "quota.jsonl")
            if r["share"] == share and r["at"] >= since]


def growth_per_hour(readings: list[Reading], window_hours: float) -> float | None:
    """Bytes per hour over the last `window_hours`, from the oldest and newest reading in it."""
    if not readings:
        return None
    end = readings[-1]
    inside = [r for r in readings if r.at >= end.at - window_hours * 3600]
    first = inside[0]
    if end.at - first.at < 0.4 * window_hours * 3600:
        return None
    return (end.used - first.used) / ((end.at - first.at) / 3600)


def outlook(config: Config, share: str) -> dict:
    """How full a share is, how fast it is growing, and when it reaches its hard limit."""
    readings = history(config, share, since=time.time() - 8 * 86400)
    if not readings:
        return {}
    now = readings[-1]
    out = {"reading": now, "left": now.hard - now.used}
    for name, hours in (("per_hour_1h", 1), ("per_hour_24h", 24), ("per_hour_7d", 168)):
        out[name] = growth_per_hour(readings, hours)
    for name in ("per_hour_1h", "per_hour_24h"):
        rate = out[name]
        out[name.replace("per_hour", "hours_to_hard")] = out["left"] / rate if rate and rate > 0 else None
    if now.file_hard:
        out["files_left"] = now.file_hard - now.files
    return out

