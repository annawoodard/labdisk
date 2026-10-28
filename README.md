# labdisk

When a shared storage quota fills up, the answer to "what is using it?" should already
be on disk. `labdisk` keeps that answer current on clusters where adding up a folder's
size means reading every file in it (GPFS keeps no running total per folder), so a
scan started after the quota is hit can take hours.

It does two things on a schedule:

- **Every 15 minutes it reads each quota**, which takes under a second, and keeps the
  history. That history says how fast each share is filling and when it will be full,
  and an email goes out when a share is about to fill.
- **A slow scan runs continuously in Slurm** and keeps a table of folder sizes. It uses
  a few readers and caps how many files it reads per second, so it puts little load on
  a file system everyone shares.

Asking a question never starts a scan; every answer comes from saved results in seconds.

```
labdisk               how full each share is, how fast it is growing, when it fills
labdisk top SHARE     the largest top-level folders of one share, with owners
labdisk mine          your largest folders on every share
labdisk grew --days 7 what grew most this week
labdisk trim          candidates to remove or move (it never deletes anything)
```

## What it records

- **One row per top-level folder of each share**, with its owner, size on disk,
  uncompressed size, file count and newest change. Other people's folders get only
  this row: you can see who holds what, and ask them.
- **One row per folder of yours holding at least 10 GiB** (configurable), however deep
  it sits. Smaller folders are added into their parents, so the table stays small and
  folders a few GB each never become rows.
- **Your files of at least 1 GiB**, which are re-measured every 15 minutes. A cache that
  grows by writing into files it already has never adds a new file, so a daily scan
  alone would see that growth a day late.
- A file with several hard links is counted once.

## Reports

- A private report covering every configured share, in the results directory.
- For each share with a `group` configured, a report about that share only, at
  `<share>/storage_report/README.md`, readable by that group. Your own folders appear
  there as top-level totals only.

## Alerts

Email, sent by the job itself, so it arrives when no one is watching:

- **Urgent**: the last hour's growth would reach the hard limit within 24 hours. It
  names the folders whose big files grew most. At most once every 3 hours per share.
- **Warning**: over the soft quota, or at 90% of it, or the last day's growth reaches
  the hard limit within 7 days, or 90% of a file-count limit. At most once a day.

## Trim candidates

`labdisk trim` lists folders of yours that are:

- caches (a name containing `cache`) whose `README.md` has no `Size cap:` line,
- git worktrees,
- package caches,
- or unchanged for 180 days.

It never deletes anything; each line says why the folder is listed.

## How the scan divides the work

One reader adds up one folder, so a single huge folder takes as long as it takes
however many readers are free. The scan therefore hands out work one level below each
top-level folder, and a folder that held more than 200,000 files on its last scan is
split into its subfolders on the next one. Each finished piece is saved at once, so an
interrupted scan resumes where it stopped. Your folders are rescanned daily and other
people's weekly.

## Setup

Written for a GPFS cluster with Slurm: quotas are read with `mmlsquota`, scans run as
Slurm jobs, and alerts go out with `mail`. Requires Python 3.11 or newer and nothing
else.

```
pip install --user git+https://github.com/annawoodard/labdisk   # or clone and pip install -e .
labdisk init                     # writes ~/.config/labdisk/config.toml
$EDITOR ~/.config/labdisk/config.toml   # list your shares; add email for alerts
labdisk tick                     # first quota reading; check the numbers look right
labdisk schedule --account ACCOUNT --partition PARTITION   # prints scrontab entries
scrontab -e                      # paste them in
```

Everything except the list of shares defaults to whoever runs it: their account, a
results directory at `~/.local/share/labdisk` readable only by them, and no email.
`examples/config.toml` shows every setting.

Results are small: a few tens of MB a year of quota history and under 1 MB per
folder table.
