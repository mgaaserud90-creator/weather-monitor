#!/usr/bin/env python3
"""Timeout-budget lint for the GitHub Actions workflows (A3).

Why this exists
---------------
`timeout-minutes` on a step is not a duration estimate, it is a hang detector.
Before this lint existed the `full_auto_pipeline.yml` `pipeline` job advertised
`timeout-minutes: 90` while its per-step timers summed to ~328 minutes, so every
individual timer was decorative: the *job* cap is what actually killed the
2026-10-27 run, and it killed it before the commit step. The invariant that
makes the timers meaningful is arithmetic:

    for every job:  sum(step.timeout-minutes) <= job.timeout-minutes
    for every step: step.timeout-minutes  <  job.timeout-minutes

The second rule stops a single step from being allowed to eat the whole budget.

This script walks every workflow in `.github/workflows/`, applies both rules and
exits non-zero when one is violated, naming the offending workflow, job and
step. It also flags (warns, does not fail) `run:` steps in the three critical
and/or failing fast: it is wired into `test_smoke.yml` as a real gate.

Usage
-----
    python _check_timeout_budget.py                 # lint .github/workflows
    python _check_timeout_budget.py --dir some/dir  # lint another directory
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_WORKFLOW_DIR = BASE_DIR / ".github" / "workflows"

# The jobs the pipeline cannot afford to lose: they own the commit steps, the
# history rebuild and the deploy. Steps here should not be able to hang
# unbounded, so a missing timer is worth surfacing.
CRITICAL_JOBS: dict[str, tuple[str, ...]] = {
    "full_auto_pipeline.yml": ("pipeline", "peak-rebuild", "deploy"),
}

# Steps that are third-party actions we do not own; they carry no timer by
# convention and their runtime is GitHub's problem, not ours.
EXEMPT_ACTION_PREFIXES = (
    "actions/",
    "docker://",
)


def _annotation(kind: str, message: str) -> str:
    return f"::{kind}::{message}"


def _as_int(value: Any) -> int | None:
    """Return an int timeout, or None when absent / not a literal number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _step_label(step: dict[str, Any], index: int) -> str:
    name = step.get("name")
    if name:
        return str(name)
    uses = step.get("uses")
    if uses:
        return f"uses: {uses}"
    return f"<unnamed step #{index}>"


def _is_exempt(step: dict[str, Any]) -> bool:
    uses = str(step.get("uses") or "")
    return uses.startswith(EXEMPT_ACTION_PREFIXES)


def lint_workflow(path: Path, yaml_module: Any) -> tuple[list[str], list[str], list[str]]:
    """Return (errors, warnings, notes) for one workflow file."""
    errors: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []

    try:
        document = yaml_module.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any parse failure is a lint failure
        errors.append(_annotation("error", f"{path.name}: cannot parse YAML — {exc}"))
        return errors, warnings, notes

    if not isinstance(document, dict):
        errors.append(_annotation("error", f"{path.name}: not a YAML mapping at the top level"))
        return errors, warnings, notes

    jobs = document.get("jobs") or {}
    if not isinstance(jobs, dict):
        errors.append(_annotation("error", f"{path.name}: `jobs` is not a mapping"))
        return errors, warnings, notes

    critical = CRITICAL_JOBS.get(path.name, ())

    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps") or []
        if not isinstance(steps, list):
            continue

        job_timeout = _as_int(job.get("timeout-minutes"))
        total = 0
        detailed: list[tuple[int, str]] = []
        unbounded: list[str] = []

        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            label = _step_label(step, index)
            step_timeout = _as_int(step.get("timeout-minutes"))
            if step_timeout is None:
                if (
                    job_id in critical
                    and "run" in step
                    and not step.get("continue-on-error")
                ):
                    unbounded.append(label)
                continue
            total += step_timeout
            detailed.append((step_timeout, label))
            if job_timeout is not None and step_timeout >= job_timeout:
                errors.append(
                    _annotation(
                        "error",
                        f"{path.name}: job '{job_id}' step '{label}' has "
                        f"timeout-minutes {step_timeout}, which is not smaller than "
                        f"the job timeout {job_timeout}",
                    )
                )

        if job_timeout is None:
            if job_id in critical:
                notes.append(
                    f"{path.name}: job '{job_id}' declares no job-level "
                    f"timeout-minutes, so the step budget cannot be checked"
                )
            continue

        if total > job_timeout:
            biggest = sorted(detailed, reverse=True)[:5]
            breakdown = ", ".join(f"{label}={value}" for value, label in biggest)
            errors.append(
                _annotation(
                    "error",
                    f"{path.name}: job '{job_id}' step timeouts sum to {total} min "
                    f"but the job timeout is {job_timeout} min "
                    f"(over by {total - job_timeout}). Largest: {breakdown}",
                )
            )

        if unbounded:
            warnings.append(
                _annotation(
                    "warning",
                    f"{path.name}: job '{job_id}' has {len(unbounded)} run step(s) "
                    f"without timeout-minutes and without continue-on-error: "
                    + "; ".join(unbounded),
                )
            )

    return errors, warnings, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        default=str(DEFAULT_WORKFLOW_DIR),
        help="Directory holding the workflow YAML files",
    )
    args = parser.parse_args(argv)

    workflow_dir = Path(args.dir)
    if not workflow_dir.is_dir():
        print(f"::error::timeout-budget lint: no such directory: {workflow_dir}")
        return 2

    try:
        import yaml  # type: ignore
    except ImportError:
        print(
            "::error::timeout-budget lint needs PyYAML (pip install pyyaml). "
            "Refusing to pass without checking."
        )
        return 2

    files = sorted(
        list(workflow_dir.glob("*.yml")) + list(workflow_dir.glob("*.yaml"))
    )
    if not files:
        print(f"::error::timeout-budget lint: no workflow files in {workflow_dir}")
        return 2

    all_errors: list[str] = []
    all_warnings: list[str] = []
    all_notes: list[str] = []

    for path in files:
        errors, warnings, notes = lint_workflow(path, yaml)
        all_errors.extend(errors)
        all_warnings.extend(warnings)
        all_notes.extend(notes)

    print(f"Timeout budget lint — {len(files)} workflow file(s) in {workflow_dir.name}/")
    for note in all_notes:
        print(f"  note: {note}")
    for warning in all_warnings:
        print(f"  {warning}")
    for error in all_errors:
        print(f"  {error}")

    if all_errors:
        print(
            f"\nFAILED: {len(all_errors)} timeout-budget violation(s). "
            f"Step timers must sum to no more than the job timeout, and no single "
            f"step may be given the whole job budget."
        )
        return 1

    print(f"\nOK: every job's step timeouts fit inside its job timeout.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
