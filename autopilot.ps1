<#
================================================================================
 autopilot.ps1 - VaerMonitor no-approve / single-command autopilot runner
================================================================================

 WHAT IT DOES
 ------------
 Runs the full weather-monitor ("vaer monitor") data pipeline in a single,
 non-interactive, idempotent command and - only if the data-integrity gate
 passes - commits and pushes the generated artifacts back to `main`.

 Steps, in order (each aborts the run on failure unless marked SOFT):
   1. Git sync:      git fetch origin --prune  +  git pull --rebase --autostash origin main
   2. Dependencies:  pip install (pinned CI list, quiet)
   3. Ensure .env    (mirrors CI minimal env; only created if missing)
   4. Generators:
        python _fetch_market_prices.py
        python _fetch_resolved_markets.py
        python _model_quality_tracker.py --mode <Mode>
        python _pm_strat_results.py
        python _city_deviation_stats.py
        python _populate_peak_verify.py
        python _summarize_peak_verify.py
        python _compute_market_edge.py
        python _pnl_tracker.py
        python _consolidate_trading_data.py
        python _model_accuracy_tracker.py
        python _generate_quality_report.py --html          (CI-mirrored: separate invocations
        python _generate_quality_report.py --all-cities     because the generator uses an
        python _generate_quality_report.py --index          if/elif chain - combining the flags
        python _generate_quality_report.py --peak           would only render index.html)
        python _sms_alert.py --check-and-send               (SOFT - failures are ignored)
   5. GATE:          python _verify_all_data.py  -> non-zero exit ABORTS before commit/push
   6. Commit/push:   git add -A ; git commit (only if dirty) ; git push origin main --force-with-lease
   7. Actions check: single unauthenticated REST call (no tight polling; 60 req/hr limit)

 NO-APPROVE / NON-INTERACTIVE BEHAVIOR
 -------------------------------------
   * $env:GIT_TERMINAL_PROMPT = "0"     -> git never prompts for credentials
   * $env:GIT_PAGER          = "cat"    -> no pager waiting for input
   * $env:GIT_EDITOR         = "true"   -> no editor ever opens
   * git -c core.askPass=true ...       -> any credential helper would fail-fast, never prompt
   * Explicit remote/branch on fetch/pull/push; push uses --force-with-lease so the
     autopilot cannot clobber concurrent CI auto-commits.

 PARAMETERS
 ----------
   -Mode    One of: daily_bma | hourly_check | hourly_active | daily_close | full_report
            Passed through to `_model_quality_tracker.py --mode`. Default: hourly_active
            (the CI default - lightweight, ~50 API calls).
   -DryRun  Print every command the script WOULD run without executing anything.
            Never mutates git, never installs packages, never runs generators,
            and never commits or pushes.

 USAGE
 -----
   # Run (from the repo root, or anywhere - the script cd's to its own directory):
   powershell -ExecutionPolicy Bypass -File "C:\Users\PC\Desktop\vaer monitor\autopilot.ps1"
   powershell -ExecutionPolicy Bypass -File "C:\Users\PC\Desktop\vaer monitor\autopilot.ps1" -Mode daily_close
   powershell -ExecutionPolicy Bypass -File "C:\Users\PC\Desktop\vaer monitor\autopilot.ps1" -DryRun

   # If PowerShell/cmd struggle with the non-ASCII directory name, use the 8.3 short name:
   powershell -ExecutionPolicy Bypass -File "C:\Users\PC\Desktop\VRMONI~1\autopilot.ps1" -DryRun

 LIMITS (NOTES - this is a local/one-click runner, not a workflow)
 -----------------------------------------------------------------
   * GitHub cron floor is 5 minutes; a scheduled GitHub Actions cron can never run
     more often than every 5 min. This script is meant to be run manually/locally.
   * GitHub job ceiling is 6 hours; do not start a second concurrent rapid loop
     (the rapid market-price workflow already self-terminates at ~5h50m).
   * Unauthenticated GitHub API = 60 requests/hour: the Actions check below uses a
     SINGLE /actions/runs?per_page=5 call and never polls in a tight loop.
================================================================================
#>

[CmdletBinding()]
param(
    [ValidateSet("daily_bma", "hourly_check", "hourly_active", "daily_close", "full_report")]
    [string]$Mode = "hourly_active",

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# No-approve / non-interactive environment
# ---------------------------------------------------------------------------
$env:GIT_TERMINAL_PROMPT = "0"
$env:GIT_PAGER          = "cat"
$env:GIT_EDITOR         = "true"

# Repo root = the directory this script lives in (works even if invoked elsewhere).
$RepoRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = (Get-Location).Path }
Set-Location -LiteralPath $RepoRoot

$Timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "  VaerMonitor autopilot - no-approve runner" -ForegroundColor Cyan
Write-Host "  Mode    : $Mode" -ForegroundColor Cyan
Write-Host "  DryRun  : $DryRun" -ForegroundColor Cyan
Write-Host "  Started : $Timestamp" -ForegroundColor Cyan
Write-Host "  Repo    : $RepoRoot" -ForegroundColor Cyan
Write-Host "==================================================================" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Label)
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $Label) -ForegroundColor Yellow
}

function Write-Cmd {
    param([Parameter(Mandatory = $true)][string]$Cmd)
    Write-Host ("  > {0}" -f $Cmd) -ForegroundColor DarkGray
}

# Runs a single command. In -DryRun mode it only prints. Non-zero exit aborts
# the whole run (throws), unless the step is marked -Soft.
function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$File,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$Soft
    )

    $display = $Arguments -join " "
    Write-Step $Label
    Write-Cmd "$File $display"

    if ($DryRun) { return }

    & $File @Arguments
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        if ($Soft) {
            Write-Warning "[SOFT] '$File $display' exited with code $exitCode - ignoring failure (marked SOFT)."
            return
        }
        throw "[FATAL] '$File $display' exited with code $exitCode. Aborting autopilot (no commit/push)."
    }

    Write-Host "  [OK] $File $display" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 1. Git sync (non-interactive)
# ---------------------------------------------------------------------------
Invoke-Step "1/7 Git fetch"  git @("-c", "core.askPass=true", "fetch", "origin", "--prune")
Invoke-Step "1/7 Git pull (rebase)" git @("-c", "core.askPass=true", "pull", "--rebase", "--autostash", "origin", "main")

# ---------------------------------------------------------------------------
# 2. Dependencies (pinned CI list, quiet)
# ---------------------------------------------------------------------------
Invoke-Step "2/7 pip install dependencies" pip @(
    "install", "--disable-pip-version-check", "-q",
    "httpx", "structlog", "tenacity", "pydantic", "pydantic-settings",
    "python-dotenv", "orjson", "redis"
)

# ---------------------------------------------------------------------------
# 3. Ensure .env (mirrors CI; idempotent - only created when missing)
# ---------------------------------------------------------------------------
Write-Step "3/7 Ensure .env exists (mirrors CI minimal env)"
if ($DryRun) {
    Write-Cmd "Set-Content .env  (ENV=production / WEATHER_BMA_ENABLED=true / WEATHER_SATELLITE_ENABLED=false / WEATHER_ENSEMBLE_CONFIDENCE_FLOOR=0.5) - only if missing"
}
elseif (-not (Test-Path -LiteralPath ".env")) {
    @"
ENV=production
WEATHER_BMA_ENABLED=true
WEATHER_SATELLITE_ENABLED=false
WEATHER_ENSEMBLE_CONFIDENCE_FLOOR=0.5
"@ | Out-File -FilePath ".env" -Encoding UTF8
    Write-Host "  [OK] Created .env" -ForegroundColor Green
}
else {
    Write-Host "  [SKIP] .env already exists - leaving it untouched" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 4. Generators (fatal on failure unless marked SOFT)
# ---------------------------------------------------------------------------
Invoke-Step "4/7 Fetch market prices"           python @("_fetch_market_prices.py")
Invoke-Step "4/7 Fetch resolved markets"        python @("_fetch_resolved_markets.py")
Invoke-Step "4/7 Model quality tracker"         python @("_model_quality_tracker.py", "--mode", $Mode)
Invoke-Step "4/7 PM strategy results"           python @("_pm_strat_results.py")
Invoke-Step "4/7 Per-city deviation stats"      python @("_city_deviation_stats.py")
Invoke-Step "4/7 Populate peak verification"    python @("_populate_peak_verify.py")
Invoke-Step "4/7 Summarize peak verification"   python @("_summarize_peak_verify.py")
Invoke-Step "4/7 Compute market edge"           python @("_compute_market_edge.py")
Invoke-Step "4/7 PnL ledger"                    python @("_pnl_tracker.py")
Invoke-Step "4/7 Consolidate trading data"      python @("_consolidate_trading_data.py")
Invoke-Step "4/7 Model accuracy tracker"        python @("_model_accuracy_tracker.py")

# The report generator uses an if/elif chain, so each output is a separate
# invocation (mirroring CI) - combining the flags would render only index.html.
Invoke-Step "4/7 Quality report (html)"         python @("_generate_quality_report.py", "--html")
Invoke-Step "4/7 Quality report (all cities)"   python @("_generate_quality_report.py", "--all-cities")
Invoke-Step "4/7 Quality report (index)"        python @("_generate_quality_report.py", "--index")
Invoke-Step "4/7 Quality report (peak)"         python @("_generate_quality_report.py", "--peak")

Invoke-Step "4/7 SMS alerts" -Soft              python @("_sms_alert.py", "--check-and-send")

# ---------------------------------------------------------------------------
# 5. GATE - data integrity must pass before any commit/push
# ---------------------------------------------------------------------------
try {
    Invoke-Step "5/7 GATE: verify all data integrity" python @("_verify_all_data.py")
}
catch {
    Write-Host ""
    Write-Host "[GATE FAILED] _verify_all_data.py failed - ABORTING." -ForegroundColor Red
    Write-Host "No commit and no push were performed." -ForegroundColor Red
    Write-Host ""
    exit 1
}

# ---------------------------------------------------------------------------
# 6. Commit + push (only if the working tree has changes)
# ---------------------------------------------------------------------------
if ($DryRun) {
    Write-Step "6/7 Commit + push (would run only if the working tree has changes)"
    Write-Cmd "git add -A"
    Write-Cmd "git -c core.askPass=true commit -m `"Autopilot: $Mode $Timestamp`""
    Write-Cmd "git -c core.askPass=true push origin main --force-with-lease"
}
else {
    $changeCount = (@(& git status --porcelain) | Measure-Object).Count

    if ($changeCount -eq 0) {
        Write-Step "6/7 Commit + push"
        Write-Host "  [SKIP] Working tree is clean - nothing to commit or push." -ForegroundColor Green
    }
    else {
        Write-Host "  $changeCount file(s) changed - committing and pushing." -ForegroundColor Yellow
        Invoke-Step "6/7 Git add"     git @("add", "-A")
        Invoke-Step "6/7 Git commit"  git @("-c", "core.askPass=true", "commit", "-m", "Autopilot: $Mode $Timestamp")
        Invoke-Step "6/7 Git push"    git @("-c", "core.askPass=true", "push", "origin", "main", "--force-with-lease")
    }
}

# ---------------------------------------------------------------------------
# 7. GitHub Actions status (gh CLI not installed - single unauthenticated call)
# ---------------------------------------------------------------------------
$ApiHeaders = @{
    "User-Agent" = "autopilot"
    "Accept"     = "application/vnd.github+json"
}
$ApiUrl = "https://api.github.com/repos/mgaaserud90-creator/weather-monitor/actions/runs?per_page=5"

Write-Step "7/7 GitHub Actions status (single REST call, no polling)"
if ($DryRun) {
    Write-Cmd "Invoke-RestMethod -Headers @{ 'User-Agent'='autopilot'; 'Accept'='application/vnd.github+json' } -Uri '$ApiUrl'"
}
else {
    try {
        $runs = Invoke-RestMethod -Headers $ApiHeaders -Uri $ApiUrl
        if (-not $runs.workflow_runs) {
            Write-Host "  No workflow runs returned." -ForegroundColor Green
        }
        foreach ($run in $runs.workflow_runs) {
            $conclusion = if ($run.conclusion) { $run.conclusion } else { "n/a" }
            Write-Host ("  run {0} | {1} | status={2} conclusion={3} | created={4}" -f `
                $run.id, $run.name, $run.status, $conclusion, $run.created_at) -ForegroundColor White
        }
    }
    catch {
        Write-Warning "Actions status check failed (non-fatal): $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "  Autopilot finished: $((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))" -ForegroundColor Cyan
if ($DryRun) {
    Write-Host "  DRY RUN - no commands were executed; no commit/push performed." -ForegroundColor Cyan
}
Write-Host "==================================================================" -ForegroundColor Cyan
