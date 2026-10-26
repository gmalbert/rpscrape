# Daily GB flat results. Fills any days missing since the earliest covered day
# and rotates the Proton WireGuard exit when Racing Post throttles the current
# egress. Run daily from Task Scheduler with "Run with highest privileges" so
# WireGuard rotation is permitted.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\daily_scrape.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\daily_scrape.ps1 -Date 2026-10-22

param(
    [string]$Date
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root 'venv\Scripts\python.exe'

Set-Location (Join-Path $root 'scripts')

if ($Date) {
    & $python -u daily_run.py --date $Date
} else {
    & $python -u daily_run.py
}

exit $LASTEXITCODE
