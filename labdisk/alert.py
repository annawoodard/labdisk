"""Email alerts, sent by the job itself so they arrive when no one is watching.

An urgent alert (the last hour's growth reaches the hard limit within 24 hours) is sent
at most once every 3 hours per share; any other warning at most once a day per share.
"""

from __future__ import annotations

import os
import subprocess
import time

from .config import Config
from .quota import outlook
from .report import size, warnings
from .store import read_json, write_json

URGENT_EVERY, OTHER_EVERY = 3 * 3600, 24 * 3600


def send(config: Config, subject: str, body: str) -> None:
    if not config.email:
        return
    subprocess.run(["mail", "-s", subject, config.email], input=body, text=True, check=True, timeout=60)


def check(config: Config, grew: list[dict]) -> list[str]:
    """Send whatever alerts are due; `grew` is this tick's big-file growth. Returns the subjects sent."""
    state_path = config.results / "alert_state.json"
    state = read_json(state_path, {})
    sent = []
    for share in config.shares:
        lines = warnings(config, share, outlook(config, share.name))
        if not lines:
            continue
        urgent = lines[0].startswith("URGENT")
        key = f"{share.name}:{'urgent' if urgent else 'other'}"
        if time.time() - state.get(key, 0) < (URGENT_EVERY if urgent else OTHER_EVERY):
            continue
        body = "\n\n".join(lines)
        if urgent:
            # Folders, not files, and at most four levels below the share: deeper names can be
            # patient or exam identifiers, and this message leaves the cluster.
            by_folder: dict[str, int] = {}
            for g in grew:
                if g["path"].startswith(str(share.path) + "/"):
                    parts = os.path.relpath(os.path.dirname(g["path"]), share.path).split(os.sep)[:4]
                    by_folder["/".join(parts)] = by_folder.get("/".join(parts), 0) + g["grew"]
            top = sorted(by_folder.items(), key=lambda kv: -kv[1])[:10]
            if top:
                body += "\n\nFolders whose big files grew most since the last 15-minute check:\n" + "\n".join(
                    f"  +{size(n)}  {folder}" for folder, n in top)
        body += f"\n\nFull report: {config.results / 'report.md'}\n"
        subject = f"labdisk: {'URGENT ' if urgent else ''}{share.name} quota"
        send(config, subject, body)
        state[key] = time.time()
        sent.append(subject)
    write_json(state_path, state)
    return sent
