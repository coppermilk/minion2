# Registers the Windows deployment in Task Scheduler (BLUEPRINT 14).
# Run from an elevated PowerShell. Default set is the BLUEPRINT 14
# Windows row: sort (Downloads axis) every 5 minutes, week-clean on
# Monday mornings, print and catch at logon as always-on streamers.
# The wall clock lives here, in the scheduler -- never in the bots
# (BLUEPRINT 11).
#
#   .\register-tasks.ps1                 # the default set
#   .\register-tasks.ps1 -Bots inbox     # add any streaming bot

param(
    [string[]]$Bots = @()
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'run-bot.ps1'

function New-BotCommand {
    param([string]$Bot)
    return ('powershell -NoProfile -ExecutionPolicy Bypass ' +
        "-File `"$runner`" -Bot $Bot")
}

function Register-Streaming {
    param([string]$Bot)
    # Streaming bots behave like services: start at logon; the
    # kernel supervises restarts (OPERATIONS 5 liveness model).
    schtasks /Create /F /TN "bananaland\$Bot" /SC ONLOGON `
        /TR (New-BotCommand $Bot)
}

# Batch: cadence is safe at 5 minutes because of the per-bot lock
# (REQ-RES-003) and the idle fast-exit (OPERATIONS 5).
schtasks /Create /F /TN 'bananaland\sort' /SC MINUTE /MO 5 `
    /TR (New-BotCommand 'sort')
schtasks /Create /F /TN 'bananaland\week-clean' /SC WEEKLY /D MON `
    /ST 06:00 /TR (New-BotCommand 'week-clean')

Register-Streaming 'print'
Register-Streaming 'catch'
foreach ($bot in $Bots) { Register-Streaming $bot }

Write-Output 'Registered. Review: schtasks /Query /TN bananaland\'
