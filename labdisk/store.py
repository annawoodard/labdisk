"""Private result files: append-only JSON lines and atomically replaced JSON documents.

Everything is written readable by the owner only. Reports meant for a lab group are
written by `report.py` with their own permissions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def append_jsonl(path: Path, record: dict) -> None:
    _private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_json(path: Path, value) -> None:
    _private_dir(path.parent)
    write_text(path, json.dumps(value, indent=1))


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path) as handle:
        return json.load(handle)


def write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write to a temporary sibling and rename it into place, so a reader never sees half a file."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
