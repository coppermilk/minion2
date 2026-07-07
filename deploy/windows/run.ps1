# One script, one Task Scheduler entry -- starts the Windows bots.
#
# Windows runs exactly two bots: the printer (`print`) and the
# Downloads watcher (`catch`); every other bot runs in Docker on the
# NAS. It loads the same single .env the NAS uses (paths are validated
# for either OS) and launches both bots.
#
# DON'T run this .ps1 directly -- PowerShell's execution policy blocks
# it ("running scripts is disabled on this system"). Use the sibling
# `run.cmd` instead: just double-click it, or in Task Scheduler ->
# Create Task -> Trigger: At log on -> Action: Start a program ->
# Program: C:\path\to\minion2\deploy\windows\run.cmd  (run.cmd calls
# this script with the policy bypassed for that one process).

param(
    [string]$EnvFile = (Join-Path $PSScriptRoot '..\..\.env')
)

$ErrorActionPreference = 'Stop'
$repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $repo

# Load .env into the process environment (KEY=VALUE, '#' comments);
# child processes started below inherit it. Precedence stays "the
# mapping you pass" (BLUEPRINT 7): the bots read os.environ.
if (-not (Test-Path $EnvFile)) {
    throw "bad_config: $EnvFile not found; copy .env.example to .env"
}
foreach ($line in Get-Content $EnvFile) {
    $trimmed = $line.Trim()
    if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
    $pair = $trimmed.Split('=', 2)
    if ($pair.Count -eq 2) {
        [Environment]::SetEnvironmentVariable($pair[0], $pair[1])
    }
}

# TELEMETRY: pre-logging crashes (a bad_config from settings.load, a
# missing python) would vanish into the task's void otherwise, so
# each bot's launcher streams land next to its <bot>.log.
$logDir = Join-Path $env:DRIVE 'bots\_data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Start each bot as its own long-running process; the kernel
# supervises the belt inside each one.
foreach ($bot in @('print', 'catch')) {
    $package = $bot.Replace('-', '_')
    $log = Join-Path $logDir "$bot.launcher.log"
    Start-Process -FilePath 'python' `
        -ArgumentList '-m', "minions.$package.main" `
        -WorkingDirectory $repo `
        -RedirectStandardOutput $log `
        -RedirectStandardError "$log.err" `
        -WindowStyle Hidden
    Write-Output "started $bot"
}
