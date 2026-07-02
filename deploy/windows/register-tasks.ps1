# Registers the Windows deployment in Task Scheduler (BLUEPRINT 14).
# Run from an elevated PowerShell. Default set is the BLUEPRINT 14
# Windows row: sort (Downloads axis, SORT_WATCH=1 in .env -> new
# images sort instantly) at logon, week-clean on Monday mornings,
# print and catch at logon as always-on streamers. The wall clock
# lives here, in the scheduler -- never in the bots (BLUEPRINT 11).
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

schtasks /Create /F /TN 'bananaland\week-clean' /SC WEEKLY /D MON `
    /ST 06:00 /TR (New-BotCommand 'week-clean')

# sort is a watch daemon (set SORT_WATCH=1 in .env): new images in
# Downloads/_inbox sort instantly; the lock (REQ-RES-003) keeps any
# manual one-shot run safe alongside it.
Register-Streaming 'sort'
Register-Streaming 'print'
Register-Streaming 'catch'
foreach ($bot in $Bots) { Register-Streaming $bot }

Write-Output 'Registered. Review: schtasks /Query /TN bananaland\'
