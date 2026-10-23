# Runs the previous day's GB flat results. Intended to be run daily by Task Scheduler.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\daily_scrape.ps1
#
# Pass -Date 'yyyy/MM/dd' to override the day (useful for testing or catching up).

param(
    [string]$Date
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root 'venv\Scripts\python.exe'
$logDir = Join-Path $root '.cache\logs'

if (-not $Date) {
    $Date = (Get-Date).AddDays(-1).ToString('yyyy/MM/dd')
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ('daily_{0}.log' -f $Date.Replace('/', '_'))

Set-Location (Join-Path $root 'scripts')

# Feed stdin so an unexpected update prompt cannot hang an unattended run.
'' | & $python -u rpscrape.py -d $Date -r gb -t flat *>> $log
