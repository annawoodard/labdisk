"""Reading the configuration file and the sizes written in it.

Only the shares must be configured. Everything else defaults to the person running
labdisk: their account, a results directory in their home, and no email.
"""

from __future__ import annotations

import getpass
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _xdg(variable: str, fallback: str) -> Path:
    return Path(os.environ.get(variable) or os.path.expanduser(fallback))


DEFAULT_CONFIG = Path(os.environ.get("LABDISK_CONFIG") or _xdg("XDG_CONFIG_HOME", "~/.config") / "labdisk" /
                      "config.toml")
DEFAULT_RESULTS = _xdg("XDG_DATA_HOME", "~/.local/share") / "labdisk"

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
    """One storage area, usually with its own quota."""

    name: str
    path: Path
    # What mmlsquota needs before --block-size, ending with the file system name:
    # ["-j", "<fileset>", "<filesystem>"] for a lab share, ["-u", "<account>", "<filesystem>"]
    # for a per-user quota. None: the share is scanned but has no quota readings or alerts.
    quota: list[str] | None = None
    group: str | None = None  # group that may read this share's report; None means no shared report


@dataclass(frozen=True)
class Config:
    shares: list[Share]
    owner: str = field(default_factory=getpass.getuser)  # whose folders get detailed rows
    results: Path = DEFAULT_RESULTS  # private results directory
    email: str | None = None  # where alerts go; None sends none
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
    stale_days: float = 180  # trim: folders unchanged this long are listed
    units: str = "decimal"  # how sizes print: "decimal" (TB, GB) or "binary" (TiB, GiB)


def load(path: Path = DEFAULT_CONFIG) -> Config:
    if not path.exists():
        raise SystemExit(f"no configuration at {path}; run `labdisk init` to write a starter one")
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    shares = []
    for s in raw.pop("share", []):
        unknown = set(s) - set(Share.__dataclass_fields__)
        if unknown:
            raise ValueError(f"share {s.get('name')!r}: unknown keys {sorted(unknown)}")
        shares.append(Share(**{**s, "path": Path(s["path"]).expanduser()}))
    if not shares:
        raise ValueError(f"{path} configures no [[share]]")
    if len({s.name for s in shares}) != len(shares):
        raise ValueError("share names must be unique")
    unknown = set(raw) - (set(Config.__dataclass_fields__) - {"shares"})
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    for key in ("min_row_bytes", "big_file_bytes"):
        if key in raw:
            raw[key] = parse_bytes(raw[key])
    if "results" in raw:
        raw["results"] = Path(raw["results"]).expanduser()
    return Config(shares=shares, **raw)


STARTER = """\
# labdisk configuration. Only the [[share]] entries are required; the commented
# values below are the defaults.

# Whose folders get detailed rows, and where private results go (readable by you only):
# owner = "{user}"
# results = "{results}"
# email = "you@uchicago.edu"             # where alerts go; unset sends none
# units = "decimal"                      # how sizes print: decimal (TB, GB) or binary (TiB, GiB)
# min_row_bytes = "10 GiB"               # smallest folder of yours that gets its own row
# readers = 4                            # parallel readers in the continuous scan
# max_files_per_second = 2000            # across all readers
# publish_lab_reports = false            # true: write each group's report into its share

# One [[share]] per storage area. `quota` is what mmlsquota needs before
# --block-size, ending with the file system name; leave it out for no quota readings.
# `group`, if set, gets a report about that share at <path>/storage_report/README.md.

[[share]]
name = "my-lab"
path = "/gpfs/data/my-lab"
quota = ["-j", "my-lab", "data1"]
# group = "cri-my_lab"

[[share]]
name = "scratch"
path = "/ess/scratch/scratch1/{user}"
quota = ["-u", "{user}", "scratch1"]
"""


def write_starter(path: Path = DEFAULT_CONFIG) -> Path:
    if path.exists():
        raise SystemExit(f"{path} already exists; edit it instead")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(STARTER.format(user=getpass.getuser(), results=DEFAULT_RESULTS))
    return path
