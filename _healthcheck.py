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

Transient failures are NOT breaches. Every `gh` question is retried with
backoff; if it still cannot be answered (rate limit, 5xx, network) the
measurement is recorded as *inconclusive* in `_healthcheck_report.json` and in
the log, and the run stays green without alerting. Rationale: this watchdog
runs every 30 minutes against the GitHub API, and a false `pipeline-alert`
issue trains the operator to ignore the channel that must never be ignored.
A real outage is caught by the file-based checks, which cannot be flaky, and
persists across cycles anyway.

Simulations never page. `--simulate-stale-hours/--simulate-missing` make the
run red on purpose (that is the proof) but `--deliver-only` suppresses
delivery, so a proof run cannot open a real `pipeline-alert` issue.

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
import time
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


# ---------------------------------------------------------------------------
# transient-failure handling
# ---------------------------------------------------------------------------
# The watchdog runs every 30 minutes and asks the GitHub API two questions per
# artifact plus one per watched workflow. A rate limit, a 502 or a dropped
# connection is *not* a pipeline outage, and turning one into a
# `pipeline-alert` issue is worse than staying quiet: an alert that cries wolf
# trains the operator to ignore the one channel that must never be ignored.
#
# So every API question is asked up to GH_ATTEMPTS times with backoff, and the
# outcome is classified three ways:
#   ok          — we got an answer, use it;
#   unverified  — every attempt failed for a transient reason: log a warning,
#                 record it as inconclusive, never alert, never go red;
#   definitive  — gh answered with a non-transient error (e.g. a missing
#                 permission): a real, repeatable defect, allowed to alert.
# A genuine pipeline outage is caught by the file-based checks (heartbeat,
# artifact freshness, WU coverage), which cannot be flaky.

GH_ATTEMPTS = 3
GH_BACKOFF_SECONDS = (2.0, 5.0)

TRANSIENT_MARKERS = (
    "rate limit",
    "rate_limit",
    "secondary rate",
    "abuse detection",
    "502",
    "503",
    "504",
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "could not resolve host",
    "temporary failure",
    "i/o timeout",
    "unexpected eof",
)


def _is_transient(message: str) -> bool:
    """True when a `gh` failure looks like infrastructure, not a real answer."""
    low = (message or "").lower()
    return any(marker in low for marker in TRANSIENT_MARKERS)


def _run_gh(
    cmd: list[str], timeout: int = 120
) -> tuple[subprocess.CompletedProcess | None, str]:
    """Run a `gh` command, retrying transient failures with backoff.

    Returns `(proc, note)`:

      * `proc.returncode == 0` — success;
      * `proc.returncode != 0` — a *definitive* failure (gh answered with a
        non-transient error); the caller may treat it as a real defect;
      * `proc is None` — **unverified**: every attempt failed for a transient
        reason, or `gh` is not installed. Never a breach, never an alert.
    """
    last_note = "gh failed"
    for attempt in range(1, GH_ATTEMPTS + 1):
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
        except FileNotFoundError:
            return None, "gh CLI not found"
        except subprocess.SubprocessError as exc:
            last_note = f"gh failed: {type(exc).__name__}"
        else:
            if proc.returncode == 0:
                return proc, "ok"
            lines = (proc.stderr or proc.stdout or "gh failed").strip().splitlines()
            last_note = (lines or ["gh failed"])[-1][:300]
            if not _is_transient(last_note):
                return proc, last_note
            # Rollout note: a transient failure is retried, not reported.
        if attempt < GH_ATTEMPTS:
            time.sleep(
                GH_BACKOFF_SECONDS[min(attempt - 1, len(GH_BACKOFF_SECONDS) - 1)]
            )
    return None, f"unverifiable after {GH_ATTEMPTS} attempts: {last_note}"


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


def _api_committed_timestamp(
    name: str, repo: str | None = None
) -> tuple[datetime | None, str]:
    """Last commit touching `name`, via the GitHub API (one cheap call).

    Deliberately NOT `git log --name-only` over local history: the repository
    has 20 492 commits and a ~500 MB `.git` (measured 2026-10-29), so a
    `fetch-depth: 0` clone inside a */30 min watchdog would itself become an
    outage. `GET /repos/{repo}/commits?path=...&per_page=1` answers the same
    question in one request.

    Returns `(timestamp, status)` with status one of:
      * `ok`         — a commit date came back;
      * `empty`      — the API answered and no commit touches the path, which
                       is a *definitive* answer and therefore a real signal;
      * `unverified` — the API could not be asked (rate limit / 5xx / offline)
                       even after retries. Transient by construction.
    """
    repo = repo or os.getenv("GITHUB_REPOSITORY", "")
    if not repo:
        return None, "unverified"
    proc, note = _run_gh(
        [
            "gh",
            "api",
            f"repos/{repo}/commits?path={name}&per_page=1",
            "--jq",
            ".[0].commit.committer.date",
        ],
        timeout=60,
    )
    if proc is None:
        print(f"[healthcheck] {name}: commit date unverified — {note}")
        return None, "unverified"
    if proc.returncode != 0:
        print(f"[healthcheck] {name}: commit lookup refused — {note}")
        return None, "unverified"
    raw = (proc.stdout or "").strip()
    if not raw or raw == "null":
        return None, "empty"
    return _parse_iso(raw), "ok"


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
) -> tuple[datetime | None, str, str]:
    """`(when it last changed in main, which source answered, lookup status)`.

    The third element exists so that "nobody has ever committed this file"
    (definitive, a real signal) can be told apart from "we could not ask"
    (transient, must never alert).
    """
    api_ts, api_status = _api_committed_timestamp(name, repo)
    if api_ts is not None:
        return api_ts, "github-api", "ok"
    git_ts = _git_committed_timestamp(root, name)
    if git_ts is not None:
        return git_ts, "git-log", "ok"
    return None, "unavailable", api_status


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
    inconclusive: list[str] | None = None,
) -> dict[str, dict]:
    """Staleness for every thresholded artifact. Returns the measured map."""
    simulate_missing = simulate_missing or set()
    inconclusive = inconclusive if inconclusive is not None else []
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
        commit_ts, commit_source, lookup = committed_timestamp(root, name, repo)

        candidates: list[tuple[datetime, str]] = []
        if heartbeat_ts is not None:
            candidates.append((heartbeat_ts, "heartbeat"))
        if commit_ts is not None:
            candidates.append((commit_ts, commit_source))

        if not candidates:
            if lookup == "unverified":
                # No heartbeat entry and no local history, and the API could
                # not be asked. Transient by construction: this must never
                # produce a `pipeline-alert` issue, because on the next cycle
                # (30 min later) the question will very likely be answerable.
                print(
                    f"::warning::{name} — freshness unverified this cycle "
                    "(GitHub API unreachable after retries); not treated as a breach"
                )
                inconclusive.append(f"{name}: freshness unverified this cycle")
                measured[name] = {"unverified": True}
                continue
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
) -> tuple[int | None, str, str]:
    """Count schedule-triggered runs of `workflow` inside the trailing window.

    Returns `(count, note, status)`:

      * `status == "ok"`               — `count` is a real measurement;
      * `status == "unverified"`       — every attempt failed transiently
        (rate limit / 5xx / offline). Inconclusive: warn, never alert;
      * `status == "definitive-failure"` — gh answered with a real, repeatable
        error (bad workflow name, missing `actions: read`). That is a defect
        worth an alert, because the dropped-slot detector is blind until it is
        fixed.
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
    proc, note = _run_gh(cmd, timeout=120)
    if proc is None:
        return None, note, "unverified"
    if proc.returncode != 0:
        return None, f"gh failed: {note}", "definitive-failure"
    try:
        runs = json.loads(proc.stdout or "[]")
    except ValueError:
        return None, "gh returned non-JSON", "definitive-failure"

    cutoff = now - timedelta(hours=window_hours)
    count = 0
    for run in runs:
        if run.get("event") != "schedule":
            continue
        created = _parse_iso(run.get("createdAt"))
        if created is not None and created >= cutoff:
            count += 1
    return count, f"{count} schedule run(s) in the trailing {window_hours:g} h", "ok"


def check_dropped_slots(
    now: datetime, breaches: list[Breach], inconclusive: list[str] | None = None
) -> dict[str, int | None]:
    inconclusive = inconclusive if inconclusive is not None else []
    measured: dict[str, int | None] = {}
    for workflow, minimum in MIN_SCHEDULE_RUNS.items():
        count, note, status = count_schedule_runs(workflow, RUN_WINDOW_HOURS, now)
        measured[workflow] = count
        if status == "unverified":
            print(
                f"::warning::{workflow} — schedule delivery unverified this cycle "
                f"({note}); not treated as a breach"
            )
            inconclusive.append(
                f"{workflow}: schedule delivery unverified this cycle ({note})"
            )
        elif count is None:
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


def _simulation_json(root: Path) -> str | None:
    """Describe a simulation recorded in the report, or `None` for a real run.

    Read from the report rather than from argv because `--deliver-only` is a
    separate process invocation with no access to the dispatch inputs.
    """
    path = root / REPORT
    if not path.exists():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sim = report.get("simulation") if isinstance(report, dict) else None
    if not isinstance(sim, dict):
        return None
    parts: list[str] = []
    if sim.get("stale_hours"):
        parts.append(f"stale_hours={sim['stale_hours']}")
    if sim.get("missing"):
        parts.append("missing=" + ",".join(sim["missing"]))
    return ", ".join(parts) or None


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
    inconclusive: list[str] | None = None,
) -> dict:
    return {
        "checked_utc": _iso(now),
        "healthy": not breaches,
        "breaches": [b.as_dict() for b in breaches],
        # Measurements that could not be taken this cycle for a *transient*
        # reason. Deliberately separate from `breaches`: they never alert.
        "inconclusive": list(inconclusive or []),
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

        # A simulated breach exists to prove the alert *path*, not to page
        # anybody. Delivering it would open a real `pipeline-alert` issue for
        # an outage that the operator invented — exactly the cry-wolf failure
        # an alert channel must not have. The run still finishes red, because
        # that red is the proof; nothing is delivered.
        simulated = _simulation_json(root)
        if simulated:
            print(
                f"[alert] SIMULATION ({simulated}) — delivery suppressed on "
                "purpose: no SMS, no GitHub issue. The red run is the proof."
            )
            return 0

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
    # Measurements skipped for a transient reason. These are NOT breaches.
    inconclusive: list[str] = []

    heartbeat = load_heartbeat(root, now, breaches)
    measured_artifacts = check_artifacts(
        root, heartbeat, now, breaches,
        simulate_stale_hours=args.simulate_stale_hours,
        simulate_missing=simulate_missing,
        repo=args.repo or None,
        inconclusive=inconclusive,
    )
    measured_wu = check_wunderground(root, now, breaches, simulate_missing)

    measured_slots: dict[str, int | None] = {}
    if args.skip_run_counters:
        print("[healthcheck] dropped-slot detector skipped (--skip-run-counters)")
    else:
        measured_slots = check_dropped_slots(now, breaches, inconclusive)

    report = build_report(
        now, breaches, measured_artifacts, measured_wu, measured_slots,
        args.simulate_stale_hours, simulate_missing, inconclusive,
    )
    (root / REPORT).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    for name, info in measured_artifacts.items():
        print(f"[healthcheck] {name}: {json.dumps(info, sort_keys=True)}")
    print(f"[healthcheck] wunderground: {json.dumps(measured_wu, sort_keys=True)}")
    for workflow, count in measured_slots.items():
        print(f"[healthcheck] schedule runs {workflow}: {count}")

    if inconclusive:
        print(
            f"[healthcheck] {len(inconclusive)} measurement(s) inconclusive this "
            "cycle — transient, recorded in the report, deliberately not alerted:"
        )
        for item in inconclusive:
            print(f"[healthcheck]   - {item}")

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
