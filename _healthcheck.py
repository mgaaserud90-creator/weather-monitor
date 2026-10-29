#!/usr/bin/env python3
"""_healthcheck.py — SLA watchdog for the vær monitor pipeline (B2 + B4 + B3).

Two independent classes of failure are asserted, because they fail differently:

1. SLA (B2) — an artifact stopped being refreshed, or the heartbeat stopped
   arriving. Sources of truth, in order:
     * `_heartbeat.json`, written by `_heartbeat.py` as the penultimate step of
       the pipeline job (records the mtime each artifact had *inside the run
       that produced it*, plus the moment the run reached its final steps).
     * the committed state of each artifact, via `git log -1 --format=%cI`.
   The heartbeat mtime alone is not sufficient on its own for artifacts that
   *another* workflow writes: a fresh `actions/checkout` sets every file's
   mtime to "now", so `_market_prices.json` would look fresh inside every
   pipeline run even if `fetch_market_prices` had been dead for days. The
   committed-state timestamp is the truthful "this artifact last changed at T"
   signal, so staleness is the *oldest* of the available sources.

2. Dropped slots (B4) — GitHub delivers this repo's crons 3–7 hours late and
   sometimes not at all (measured: not one of the four daily crons fired in its
   intended hour; the 08:30 slot for 29 Oct had still not fired at 11:12Z).
   A nominal-cron check would therefore be permanently red or permanently
   lying. Instead, assert on *delivered* schedule runs in the trailing 24 h:
       full_auto_pipeline      >= 2
       model_quality_pipeline  >= 4
   Both thresholds come from the measured delivery rate (last 100 runs: 4 and
   5–10 schedule runs/24 h respectively), not from the cron spec.

A missing, unparseable or stale `_heartbeat.json` is itself a failure: absence
must be a signal, never a silent pass.

Exit codes: 0 = healthy, 1 = breach, 2 = usage/config error.

Usage
-----
    python3 _healthcheck.py                     # full check (what CI runs)
    python3 _healthcheck.py --simulate-stale-hours 48
    python3 _healthcheck.py --simulate-missing _market_prices.json
    python3 _healthcheck.py --skip-run-counters # offline / local runs
    python3 _healthcheck.py --deliver-only      # send the alert written above
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

HEARTBEAT = "_heartbeat.json"
REPORT = "_healthcheck_report.json"
ALERT_FILE = "_healthcheck_alert.txt"

# Artifact staleness thresholds (B2), in hours.
STALE_HOURS: dict[str, float] = {
    "_model_quality_log.json": 6,
    "_market_prices.json": 6,
    "_resolved_markets_log.json": 12,
}

# Wunderground: every one of the last N *completed* UTC days must be covered.
# "Completed" is deliberate — WU has no row for the current UTC day until that
# day is over, so including D-0 would make the check red for ~24 h out of every
# 24. Measured on 2026-10-29T11:16Z: rows exist through 2026-10-28 only.
WU_FILE = "_wunderground_real.json"
WU_DAYS = 3

# The heartbeat is rewritten by every pipeline run (4 crons/day + dispatches).
# 16 h tolerates the measured 3–7 h cron lateness with margin while still
# catching a full day of silence.
HEARTBEAT_MAX_AGE_HOURS = 16

# Dropped-slot detector (B4).
MIN_SCHEDULE_RUNS: dict[str, int] = {
    "full_auto_pipeline.yml": 2,
    "model_quality_pipeline.yml": 4,
}
RUN_WINDOW_HOURS = 24
RUN_LIST_LIMIT = 200

ISSUE_LABEL = "pipeline-alert"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(tz=timezone.utc)


def _iso(dt: datetime) -> str:
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_iso(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive values are assumed UTC.

    Tolerates git's `%cI` output (`2026-10-29T10:35:36+00:00`), our own `Z`
    format, and git's older `2026-10-29 10:35:36 +0000` form.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600.0, 2)


class Breach:
    """One failed assertion: stable identity + human message."""

    def __init__(self, check: str, subject: str, detail: str):
        self.check = check        # e.g. "stale", "missing", "dropped-slots"
        self.subject = subject    # artifact name or workflow name
        self.detail = detail      # measured staleness etc.

    @property
    def key(self) -> str:
        return f"{self.check}:{self.subject}"

    def as_dict(self) -> dict:
        return {"check": self.check, "subject": self.subject, "detail": self.detail}


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------

def load_heartbeat(root: Path, now: datetime, breaches: list[Breach]) -> dict | None:
    path = root / HEARTBEAT
    if not path.exists():
        breaches.append(
            Breach(
                "heartbeat",
                HEARTBEAT,
                "missing — no pipeline run has ever reported in (absence is a signal)",
            )
        )
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        breaches.append(
            Breach("heartbeat", HEARTBEAT, f"unparseable ({type(exc).__name__}: {exc})")
        )
        return None
    if not isinstance(data, dict):
        breaches.append(Breach("heartbeat", HEARTBEAT, "unparseable (not an object)"))
        return None

    stamp = _parse_iso(data.get("utc"))
    if stamp is None:
        breaches.append(
            Breach("heartbeat", HEARTBEAT, "unparseable (no valid 'utc' field)")
        )
        return None

    age = _hours(now - stamp)
    if age > HEARTBEAT_MAX_AGE_HOURS:
        breaches.append(
            Breach(
                "heartbeat-stale",
                HEARTBEAT,
                f"last heartbeat {age} h ago (utc={data.get('utc')}, "
                f"run_id={data.get('run_id') or '-'}); limit "
                f"{HEARTBEAT_MAX_AGE_HOURS} h",
            )
        )
    return data


def _api_committed_timestamp(name: str, repo: str | None = None) -> datetime | None:
    """Last commit touching `name`, via the GitHub API (one cheap call).

    Deliberately NOT `git log --name-only` over local history: the repository
    has 20 492 commits and a ~500 MB `.git` (measured 2026-10-29), so a
    `fetch-depth: 0` clone inside a */30 min watchdog would itself become an
    outage. `GET /repos/{repo}/commits?path=...&per_page=1` answers the same
    question in one request.
    """
    repo = repo or os.getenv("GITHUB_REPOSITORY", "")
    if not repo:
        return None
    try:
        proc = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/commits?path={name}&per_page=1",
                "--jq",
                ".[0].commit.committer.date",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _parse_iso(proc.stdout.strip())


def _git_committed_timestamp(root: Path, name: str) -> datetime | None:
    """Local fallback for offline runs against a full clone."""
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", name],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _parse_iso(proc.stdout.strip())


def committed_timestamp(
    root: Path, name: str, repo: str | None = None
) -> tuple[datetime | None, str]:
    """`(when the artifact last changed in main, which source answered)`."""
    api_ts = _api_committed_timestamp(name, repo)
    if api_ts is not None:
        return api_ts, "github-api"
    git_ts = _git_committed_timestamp(root, name)
    if git_ts is not None:
        return git_ts, "git-log"
    return None, "unavailable"


# ---------------------------------------------------------------------------
# SLA checks
# ---------------------------------------------------------------------------

def check_artifacts(
    root: Path,
    heartbeat: dict | None,
    now: datetime,
    breaches: list[Breach],
    simulate_stale_hours: float = 0.0,
    simulate_missing: set[str] | None = None,
    repo: str | None = None,
) -> dict[str, dict]:
    """Staleness for every thresholded artifact. Returns the measured map."""
    simulate_missing = simulate_missing or set()
    hb_artifacts = {}
    if isinstance(heartbeat, dict) and isinstance(heartbeat.get("artifacts"), dict):
        hb_artifacts = heartbeat["artifacts"]

    measured: dict[str, dict] = {}

    for name, limit in STALE_HOURS.items():
        if name in simulate_missing:
            breaches.append(
                Breach("missing", name, "reported missing (simulated)")
            )
            measured[name] = {"simulated_missing": True}
            continue

        heartbeat_ts = _parse_iso(hb_artifacts.get(name))
        commit_ts, commit_source = committed_timestamp(root, name, repo)

        candidates: list[tuple[datetime, str]] = []
        if heartbeat_ts is not None:
            candidates.append((heartbeat_ts, "heartbeat"))
        if commit_ts is not None:
            candidates.append((commit_ts, commit_source))

        if not candidates:
            breaches.append(
                Breach(
                    "missing",
                    name,
                    "no mtime in _heartbeat.json and no commit touching it",
                )
            )
            measured[name] = {"missing": True}
            continue

        # Oldest source wins: a fresh `actions/checkout` stamps every file with
        # "now", so the *youngest* timestamp is the one most likely to be a lie
        # (e.g. _market_prices.json looks fresh inside every pipeline run even
        # if fetch_market_prices has been dead for days).
        reference, source = min(candidates, key=lambda item: item[0])
        age = _hours(now - reference)
        if simulate_stale_hours > 0:
            age = float(simulate_stale_hours)

        measured[name] = {
            "last_change_utc": _iso(reference),
            "source": source,
            "staleness_hours": age,
            "limit_hours": limit,
        }

        if age > limit:
            breaches.append(
                Breach(
                    "stale",
                    name,
                    f"stale {age} h (last change {_iso(reference)} via {source}); "
                    f"limit {limit} h",
                )
            )
    return measured


def check_wunderground(
    root: Path,
    now: datetime,
    breaches: list[Breach],
    simulate_missing: set[str] | None = None,
) -> dict:
    """Every one of the last WU_DAYS completed UTC days must have rows."""
    simulate_missing = simulate_missing or set()
    path = root / WU_FILE
    if WU_FILE in simulate_missing:
        breaches.append(Breach("missing", WU_FILE, "reported missing (simulated)"))
        return {"simulated_missing": True}
    if not path.exists():
        breaches.append(Breach("missing", WU_FILE, "file does not exist"))
        return {"missing": True}

    expected = [
        (now - timedelta(days=d)).date().isoformat()
        for d in range(1, WU_DAYS + 1)   # D-1 .. D-3 (completed days only)
    ]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cities = payload.get("cities", {})
        seen: set[str] = set()
        for city in cities.values():
            for row in city.get("rows", []) or []:
                day = row.get("date")
                if isinstance(day, str):
                    seen.add(day)
    except (OSError, ValueError, AttributeError) as exc:
        breaches.append(
            Breach("unparseable", WU_FILE, f"{type(exc).__name__}: {exc}")
        )
        return {"unparseable": True}

    missing = [d for d in expected if d not in seen]
    if missing:
        breaches.append(
            Breach(
                "missing-dates",
                WU_FILE,
                f"no rows for {', '.join(missing)} "
                f"(expected the last {WU_DAYS} completed UTC days)",
            )
        )
    return {"expected_days": expected, "missing_days": missing}


# ---------------------------------------------------------------------------
# dropped-slot detector (B4)
# ---------------------------------------------------------------------------

def count_schedule_runs(
    workflow: str, window_hours: float, now: datetime
) -> tuple[int | None, str]:
    """Count schedule-triggered runs of `workflow` inside the trailing window.

    Returns `(count, note)`; `count is None` means the question could not be
    answered (gh missing/unauthenticated/offline) — which is reported as a
    breach by the caller rather than silently passing.
    """
    cmd = [
        "gh",
        "run",
        "list",
        "--workflow",
        workflow,
        "--limit",
        str(RUN_LIST_LIMIT),
        "--json",
        "createdAt,event",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return None, "gh CLI not found"
    except subprocess.SubprocessError as exc:
        return None, f"gh failed: {type(exc).__name__}"
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or "gh failed").strip().splitlines()[-1][:300]
    try:
        runs = json.loads(proc.stdout or "[]")
    except ValueError:
        return None, "gh returned non-JSON"

    cutoff = now - timedelta(hours=window_hours)
    count = 0
    for run in runs:
        if run.get("event") != "schedule":
            continue
        created = _parse_iso(run.get("createdAt"))
        if created is not None and created >= cutoff:
            count += 1
    return count, f"{count} schedule run(s) in the trailing {window_hours:g} h"


def check_dropped_slots(
    now: datetime, breaches: list[Breach]
) -> dict[str, int | None]:
    measured: dict[str, int | None] = {}
    for workflow, minimum in MIN_SCHEDULE_RUNS.items():
        count, note = count_schedule_runs(workflow, RUN_WINDOW_HOURS, now)
        measured[workflow] = count
        if count is None:
            breaches.append(
                Breach(
                    "dropped-slots",
                    workflow,
                    f"cannot verify schedule delivery: {note}",
                )
            )
        elif count < minimum:
            breaches.append(
                Breach(
                    "dropped-slots",
                    workflow,
                    f"only {count} schedule run(s) in the trailing "
                    f"{RUN_WINDOW_HOURS} h; delivery minimum is {minimum} "
                    f"(derived from the measured GitHub cron delivery rate)",
                )
            )
    return measured


# ---------------------------------------------------------------------------
# alert delivery (B3)
# ---------------------------------------------------------------------------

def _twilio_configured() -> bool:
    return all(
        os.getenv(name, "").strip()
        for name in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "ALERT_PHONE")
    )


def _try_sms(message: str) -> bool:
    """Send via Twilio when fully configured. Never raises."""
    if not _twilio_configured():
        print(
            "[alert] SMS skipped — TWILIO_SID/TWILIO_TOKEN/TWILIO_FROM/ALERT_PHONE "
            "not all set (check `gh secret list`; no recipient is ever assumed)."
        )
        return False
    try:
        sys.path.insert(0, str(ROOT))
        from _sms_alert import send_alert   # local import: needs env first

        sent = send_alert(message)
        if sent:
            print("[alert] delivered via SMS (Twilio).")
            return True
        print("[alert] Twilio send returned False — falling back.")
        return False
    except SystemExit as exc:            # ALERT_PHONE unset => SystemExit(2)
        print(f"[alert] SMS refused (SystemExit {exc.code}) — falling back.")
        return False
    except Exception as exc:             # noqa: BLE001 - never swallow the alert
        print(f"[alert] SMS error: {type(exc).__name__}: {exc} — falling back.")
        return False


def alert_title(message: str) -> str:
    """Stable, dedup-friendly issue title (no measurements in it)."""
    first = message.strip().splitlines()[0] if message.strip() else "pipeline breach"
    if first.startswith("[pipeline-alert]"):
        return first[:250]
    return f"[pipeline-alert] {first}"[:250]


def _open_github_issue(message: str, *, dry_run: bool = False) -> bool:
    """Durable fallback: one open issue per distinct breach set, no spam."""
    title = alert_title(message)
    if dry_run:
        print(f"[alert] DRY-RUN would open issue: {title}")
        return True

    def gh(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            input=input_text,
            timeout=120,
        )

    # Idempotent label ensure (`--force` updates if it already exists).
    gh(
        "label",
        "create",
        ISSUE_LABEL,
        "--color",
        "B60205",
        "--description",
        "Automated pipeline SLA watchdog alert",
        "--force",
    )

    listed = gh(
        "issue",
        "list",
        "--label",
        ISSUE_LABEL,
        "--state",
        "open",
        "--limit",
        "200",
        "--json",
        "title",
    )
    if listed.returncode == 0:
        try:
            open_titles = {item.get("title") for item in json.loads(listed.stdout or "[]")}
        except ValueError:
            open_titles = set()
        if title in open_titles:
            print(
                f"[alert] not duplicating: an open issue already tracks this breach "
                f"({title})"
            )
            return True

    body = (
        message
        + "\n\n---\n"
        + "Opened by `.github/workflows/sla_watchdog.yml`. One open issue per "
        + "distinct breach set; close it once the pipeline is green again.\n"
    )
    created = gh(
        "issue",
        "create",
        "--title",
        title,
        "--body",
        body,
        "--label",
        ISSUE_LABEL,
    )
    if created.returncode == 0:
        print(f"[alert] delivered via GitHub issue: {created.stdout.strip()}")
        return True
    print(f"[alert] issue creation FAILED: {(created.stderr or '').strip()[:400]}")
    return False


def deliver_alert(message: str, *, dry_run: bool = False) -> bool:
    """Deliver an alert; SMS when configured, else a deduplicated issue."""
    if _try_sms(message):
        return True
    return _open_github_issue(message, dry_run=dry_run)


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def build_report(
    now: datetime,
    breaches: list[Breach],
    measured_artifacts: dict[str, dict],
    measured_wu: dict,
    measured_slots: dict[str, int | None],
    simulate_stale_hours: float,
    simulate_missing: set[str],
) -> dict:
    return {
        "checked_utc": _iso(now),
        "healthy": not breaches,
        "breaches": [b.as_dict() for b in breaches],
        "artifacts": measured_artifacts,
        "wunderground": measured_wu,
        "schedule_runs_24h": measured_slots,
        "thresholds": {
            "stale_hours": STALE_HOURS,
            "wunderground_completed_days": WU_DAYS,
            "heartbeat_max_age_hours": HEARTBEAT_MAX_AGE_HOURS,
            "min_schedule_runs": MIN_SCHEDULE_RUNS,
            "run_window_hours": RUN_WINDOW_HOURS,
        },
        "simulation": {
            "stale_hours": simulate_stale_hours or None,
            "missing": sorted(simulate_missing) or None,
        },
    }


def build_alert_message(report: dict) -> str:
    keys = sorted({b["check"] + ":" + b["subject"] for b in report["breaches"]})
    header = "[pipeline-alert] " + ", ".join(keys)
    lines = [
        header,
        "",
        f"Checked at {report['checked_utc']}. {len(report['breaches'])} breach(es):",
        "",
    ]
    for breach in report["breaches"]:
        lines.append(
            f"- {breach['subject']} — {breach['check']}: {breach['detail']}"
        )
    lines.append("")
    lines.append(
        "Schedule runs in the trailing 24 h: "
        + ", ".join(
            f"{wf}={count if count is not None else 'unverifiable'}"
            for wf, count in report["schedule_runs_24h"].items()
        )
    )
    run_url = (
        f"https://github.com/{os.getenv('GITHUB_REPOSITORY', '')}/actions/runs/"
        f"{os.getenv('GITHUB_RUN_ID', '')}"
        if os.getenv("GITHUB_REPOSITORY")
        else None
    )
    if run_url:
        lines.append(f"Watchdog run: {run_url}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SLA watchdog for the weather pipeline (stale artifacts + dropped slots).",
    )
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument(
        "--simulate-stale-hours",
        type=float,
        default=0.0,
        help="Force the measured staleness of all thresholded artifacts (proof aid).",
    )
    parser.add_argument(
        "--simulate-missing",
        default="",
        help="Comma-separated artifacts to report as missing (proof aid).",
    )
    parser.add_argument(
        "--skip-run-counters",
        action="store_true",
        help="Skip the `gh run list` dropped-slot check (offline/local testing).",
    )
    parser.add_argument(
        "--repo",
        default=os.getenv("GITHUB_REPOSITORY", ""),
        help="owner/name used for the GitHub API; defaults to $GITHUB_REPOSITORY.",
    )
    parser.add_argument(
        "--deliver-only",
        action="store_true",
        help=f"Deliver the alert already written to {ALERT_FILE}.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not create a GitHub issue (SMS still skipped unless configured).",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)

    if args.deliver_only:
        path = root / ALERT_FILE
        if not path.exists():
            print(f"[alert] nothing to deliver: {ALERT_FILE} does not exist")
            return 0
        message = path.read_text(encoding="utf-8")
        ok = deliver_alert(message, dry_run=args.dry_run)
        if not ok:
            print("[alert] DELIVERY FAILED — the alert was not swallowed, this "
                  "job is red on purpose.")
            return 1
        return 0

    now = _now()
    simulate_missing = {
        part.strip() for part in (args.simulate_missing or "").split(",") if part.strip()
    }
    breaches: list[Breach] = []

    heartbeat = load_heartbeat(root, now, breaches)
    measured_artifacts = check_artifacts(
        root, heartbeat, now, breaches,
        simulate_stale_hours=args.simulate_stale_hours,
        simulate_missing=simulate_missing,
        repo=args.repo or None,
    )
    measured_wu = check_wunderground(root, now, breaches, simulate_missing)

    measured_slots: dict[str, int | None] = {}
    if args.skip_run_counters:
        print("[healthcheck] dropped-slot detector skipped (--skip-run-counters)")
    else:
        measured_slots = check_dropped_slots(now, breaches)

    report = build_report(
        now, breaches, measured_artifacts, measured_wu, measured_slots,
        args.simulate_stale_hours, simulate_missing,
    )
    (root / REPORT).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    for name, info in measured_artifacts.items():
        print(f"[healthcheck] {name}: {json.dumps(info, sort_keys=True)}")
    print(f"[healthcheck] wunderground: {json.dumps(measured_wu, sort_keys=True)}")
    for workflow, count in measured_slots.items():
        print(f"[healthcheck] schedule runs {workflow}: {count}")

    if not breaches:
        print(f"[healthcheck] HEALTHY — {REPORT} written, no alert sent.")
        stale = root / ALERT_FILE
        if stale.exists():
            stale.unlink()   # a previous outage is over; do not deliver it later
        return 0

    message = build_alert_message(report)
    (root / ALERT_FILE).write_text(message, encoding="utf-8")
    for breach in breaches:
        print(f"::error title={breach.key}::{breach.subject} — {breach.detail}")
    print(message)
    return 1


if __name__ == "__main__":
    sys.exit(main())
