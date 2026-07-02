# Removes every bananaland task (symmetric to register-tasks.ps1).

param(
    [string[]]$Bots = @('sort', 'week-clean', 'print', 'catch')
)

foreach ($bot in $Bots) {
    schtasks /Delete /F /TN "bananaland\$bot" 2>$null
}
Write-Output 'Unregistered.'
