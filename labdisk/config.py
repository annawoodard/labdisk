"""Reading the configuration file and the sizes written in it."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = Path(os.environ.get("LABDISK_CONFIG", "~/.config/labdisk/config.toml")).expanduser()

_UNITS = {"": 1, "b": 1, "kib": 2**10, "mib": 2**20, "gib": 2**30, "tib": 2**40,
          "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12}


def parse_bytes(text: str | int) -> int:
    """'10 GiB' -> 10737418240. Binary and decimal units are both accepted."""
    if isinstance(text, int):
        return text
    match = re.fullmatch(r"\s*([\d.]+)\s*([A-Za-z]*)\s*", text)
    if not match or match.group(2).lower() not in _UNITS:
        raise ValueError(f"not a size: {text!r}")
    return int(float(match.group(1)) * _UNITS[match.group(2).lower()])


@dataclass(frozen=True)
class Share:
    """One storage area with its own quota."""

    name: str
    path: Path
    quota_args: list[str]  # passed to mmlsquota before --block-size, e.g. ["-j", "lab-a", "data1"]
    group: str | None = None  # group that may read this share's report; None means no shared report


@dataclass(frozen=True)
class Config:
    owner: str  # account whose folders get detailed rows
    results: Path  # private results directory
    email: str | None
    shares: list[Share]
    min_row_bytes: int = 10 * 2**30
    big_file_bytes: int = 2**30
    readers: int = 4
    max_files_per_second: int = 2000
    own_rescan_days: float = 1.0
    other_rescan_days: float = 7.0
    split_files: int = 200_000  # a unit holding more files than this is split into its subfolders next pass
    max_split_depth: int = 6
    soft_warn_fraction: float = 0.9
    publish_lab_reports: bool = False  # False: lab reports are written as previews in the results directory


def load(path: Path = DEFAULT_CONFIG) -> Config:
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    shares = [Share(name=s["name"], path=Path(s["path"]), quota_args=list(s["quota"]), group=s.get("group"))
              for s in raw.pop("share")]
    if len({s.name for s in shares}) != len(shares):
        raise ValueError("share names must be unique")
    sizes = {k: parse_bytes(raw.pop(k)) for k in ("min_row_bytes", "big_file_bytes") if k in raw}
    raw.setdefault("email", None)
    known = {f for f in Config.__dataclass_fields__} - {"shares"}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    raw["results"] = Path(raw["results"]).expanduser()
    return Config(shares=shares, **raw, **sizes)
