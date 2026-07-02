# Generic bot runner for Windows (BLUEPRINT 14: Task Scheduler,
# bare Python). Usage:
#   powershell -ExecutionPolicy Bypass -File run-bot.ps1 -Bot sort
# Bot names use the runtime spelling (censor-blur, week-clean);
# the package name derives by replacing '-' with '_'.

param(
    [Parameter(Mandatory = $true)][string]$Bot,
    [string]$EnvFile = (Join-Path $PSScriptRoot '..\..\.env')
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'env.ps1')

Import-MinionEnv -EnvFile $EnvFile -Bot $Bot
$package = $Bot.Replace('-', '_')
$repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
Set-Location $repo

python -m "minions.$package.main"
exit $LASTEXITCODE
