# Generic bot runner for Windows (BLUEPRINT 14: Task Scheduler,
# bare Python). Usage:
#   powershell -ExecutionPolicy Bypass -File run-bot.ps1 -Bot print
# Bot names use the runtime spelling (censor-blur, week-clean);
# the package name derives by replacing '-' with '_'.
#
# Every output stream is appended to
# DRIVE\bots\_data\logs\<bot>.launcher.log so failures that happen
# BEFORE the bot's own logger exists (bad_config from settings.load,
# a missing python, a runner error) never vanish into the scheduled
# task's void. Runtime events keep going to <bot>.log as usual.

param(
    [Parameter(Mandatory = $true)][string]$Bot,
    [string]$EnvFile = (Join-Path $PSScriptRoot '..\..\.env')
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'env.ps1')

# Before .env is loaded DRIVE is unknown, so a broken/missing .env
# is recorded next to this script instead of vanishing.
try {
    Import-MinionEnv -EnvFile $EnvFile -Bot $Bot
} catch {
    $boot = Join-Path $PSScriptRoot 'launcher-bootstrap.log'
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $boot -Value "[$stamp] $Bot : $_"
    exit 1
}
$package = $Bot.Replace('-', '_')
$repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $repo

# TELEMETRY: the launcher log lives next to the bot logs and rolls
# at ~10 MB (bounded, mirroring the compose json-file rotation).
$logDir = Join-Path $env:DRIVE 'bots\_data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "$Bot.launcher.log"
$rollBytes = 10MB
if ((Test-Path $log) -and (Get-Item $log).Length -gt $rollBytes) {
    Move-Item -Force $log (
        Join-Path $logDir "$Bot.launcher.old.log")
}

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $log -Value "[$stamp] launcher: starting $Bot"

python -m "minions.$package.main" *>> $log
$code = $LASTEXITCODE
if ($code -ne 0) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $log -Value "[$stamp] launcher: exit code $code"
}
exit $code
