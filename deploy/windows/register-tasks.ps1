# Registers the Windows deployment in Task Scheduler (BLUEPRINT 14).
# Run from an elevated PowerShell.
#
# Division of labour: Windows runs exactly two bots -- the printer
# (`print`, the machine the printer is attached to) and the
# Downloads watcher (`catch`). Every other bot runs in Docker on the
# NAS (docker-compose.yml); registering one here too would make it
# run in two places (a second print/ watcher prints every PDF
# twice).
#
#   .\register-tasks.ps1                 # print + catch
#   .\register-tasks.ps1 -Bots inbox     # add extras deliberately

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

Register-Streaming 'print'
Register-Streaming 'catch'
foreach ($bot in $Bots) { Register-Streaming $bot }

Write-Output 'Registered. Review: schtasks /Query /TN bananaland\'
