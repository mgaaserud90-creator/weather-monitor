#!/usr/bin/env python3
"""_heartbeat.py — write `_heartbeat.json`, the pipeline's liveness signal (B1).

Why this exists
---------------
Every way this pipeline has died so far has been *silent*: a job-level timeout
kill removes the steps that would have reported anything, and GitHub only shows
a red X that nobody watches. `_heartbeat.json` is a deliberately tiny,
machine-readable statement of "a pipeline run reached its final steps, and here
is what the artifacts looked like at that moment". `_healthcheck.py` (the SLA
watchdog) reads it, so a *missing or stale heartbeat* becomes a signal instead
of the absence of one.

Contract
--------
    {
      "utc":      "2026-10-29T06:07:41Z",   # when this file was written
      "run_id":   "40826895371",            # $GITHUB_RUN_ID, "" when absent
      "mode":     "pipeline",               # caller-supplied label
      "artifacts": {                        # fixed list, mtime only
        "_model_quality_log.json":   "2026-10-29T06:04:12Z",
        "_resolved_markets_log.json": "2026-10-29T06:04:30Z",
        "_market_prices.json":       null,  # tolerated: not generated here
        ...
      }
    }

Properties that matter
----------------------
* Crash-safe: written to a temp file in the same directory, fsync'ed, then
  `os.replace`d over the target. A kill mid-write can never leave a truncated
  `_heartbeat.json` behind (which the watchdog would have to read as a lie).
* `os.stat` only — the artifacts include a 7.5 MB JSON log; this must never
  read (let alone parse) them. Only `st_mtime` is used.
* Missing artifacts are recorded as `null` rather than omitted, so the key set
  is constant and a consumer can tell "absent" from "not checked".
* Zero dependencies, runs on any Python 3.9+.

Usage
-----
    python3 _heartbeat.py --mode pipeline
    python3 _heartbeat.py --mode pipeline --dry-run     # print, do not write
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_NAME = "_heartbeat.json"

# Fixed list — the watchdog's thresholds and this list must stay in sync.
ARTIFACTS: tuple[str, ...] = (
    "_model_quality_log.json",
    "_resolved_markets_log.json",
    "_market_prices.json",
    "_wunderground_real.json",
    "_daily_city_log.json",
    "_pnl_log.json",
)

DEFAULT_ROOT = Path(__file__).resolve().parent


def _iso(dt: datetime) -> str:
    """Format an aware datetime as `YYYY-MM-DDTHH:MM:SSZ` (seconds, UTC)."""
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def collect_artifact_mtimes(root: Path) -> dict[str, str | None]:
    """Return `{name: mtime-iso or None}` using `os.stat` only.

    A missing file is `None` — never an exception: the whole point is that the
    heartbeat gets written even when an upstream step was cut short.
    """
    out: dict[str, str | None] = {}
    for name in ARTIFACTS:
        try:
            st = os.stat(root / name)   # stat only; never open the payload
        except OSError:
            out[name] = None
            continue
        out[name] = _iso(datetime.fromtimestamp(st.st_mtime, tz=timezone.utc))
    return out


def build_heartbeat(
    mode: str,
    root: Path = DEFAULT_ROOT,
    run_id: str | None = None,
) -> dict:
    """Build the heartbeat payload (does not write anything)."""
    if run_id is None:
        run_id = os.getenv("GITHUB_RUN_ID", "")
    return {
        "utc": _iso(datetime.now(tz=timezone.utc)),
        "run_id": run_id,
        "mode": mode,
        "artifacts": collect_artifact_mtimes(root),
    }


def write_heartbeat(payload: dict, root: Path = DEFAULT_ROOT) -> Path:
    """Atomically write `_heartbeat.json`; returns the final path.

    temp file in the same directory → fsync → `os.replace`. `os.replace` is
    atomic on both POSIX and Windows and, unlike `os.rename`, overwrites an
    existing target on Windows.
    """
    target = root / HEARTBEAT_NAME
    fd, tmp_name = tempfile.mkstemp(
        prefix=".heartbeat-", suffix=".tmp", dir=str(root)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        # Never leave the temp file behind; re-raise so the step fails loudly.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write _heartbeat.json (pipeline liveness signal).",
    )
    parser.add_argument(
        "--mode",
        default="pipeline",
        help="Label recorded in the heartbeat (default: pipeline).",
    )
    parser.add_argument(
        "--root",
        default=str(DEFAULT_ROOT),
        help="Directory containing the artifacts (default: this script's dir).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Override $GITHUB_RUN_ID.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the payload instead of writing the file.",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    payload = build_heartbeat(args.mode, root, args.run_id)

    if args.dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    path = write_heartbeat(payload, root)
    present = sum(1 for v in payload["artifacts"].values() if v)
    print(
        f"[heartbeat] {path.name} written: "
        f"utc={payload['utc']} mode={payload['mode']} "
        f"run_id={payload['run_id'] or '-'} "
        f"artifacts={present}/{len(payload['artifacts'])} present"
    )
    for name, stamp in payload["artifacts"].items():
        print(f"[heartbeat]   {name}: {stamp or 'MISSING'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
