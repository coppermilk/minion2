# Отчёт о версии на «Версии» админки сайта (daetotyoma.com/admin/versions/): что стоит на этом компьютере с Windows.
# Облако не видит домашний ПК, поэтому он сообщает о себе сам. Вызовы:
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\report-version.ps1 start      # из run.ps1 при входе в систему
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\report-version.ps1 heartbeat  # задача планировщика раз в день
# Параметры: событие (deploy|rollback|heartbeat|start|stop|error), ok, сообщение. Коммит — git rev-parse HEAD, если git есть, иначе
# хеш pyproject.toml. Настройки из .env: VERSIONS_KEY (ключ BOARD_KEY сайта, обязателен), VERSIONS_URL, VERSIONS_PROJECT
# (по умолчанию minion2-windows). Нет сети — отчёт ложится в %DRIVE%\bots\_data\state\versions-outbox.jsonl и досылается потом.
# Никогда не роняет вызывающего: любая ошибка — строка в вывод и выход с кодом 0.
param(
    [string]$Event = 'heartbeat',
    [string]$Ok = '',
    [string]$Message = '',
    [string]$EnvFile = (Join-Path $PSScriptRoot '..\..\.env')
)
$ErrorActionPreference = 'Continue'
try {
    $repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
    $cfg = @{}
    if (Test-Path $EnvFile) {
        foreach ($line in Get-Content $EnvFile) {
            $t = $line.Trim()
            if ($t -eq '' -or $t.StartsWith('#')) { continue }
            $pair = $t.Split('=', 2)
            if ($pair.Count -eq 2) {
                $v = $pair[1].Trim()
                if ($v.Length -ge 2 -and (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'")))) { $v = $v.Substring(1, $v.Length - 2) }
                $cfg[$pair[0].Trim()] = $v
            }
        }
    }
    $key = $cfg['VERSIONS_KEY']; if (-not $key) { $key = $env:VERSIONS_KEY }
    if (-not $key) { Write-Output 'версии: VERSIONS_KEY не задан в .env — отчёт не отправлен'; exit 0 }
    $url = if ($cfg['VERSIONS_URL']) { $cfg['VERSIONS_URL'] } else { 'https://daetotyoma.com/admin/versions' }
    $project = if ($cfg['VERSIONS_PROJECT']) { $cfg['VERSIONS_PROJECT'] } else { 'minion2-windows' }
    $drive = if ($cfg['DRIVE']) { $cfg['DRIVE'] } else { $env:DRIVE }
    $stateDir = if ($drive) { Join-Path $drive 'bots\_data\state' } else { $PSScriptRoot }
    New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
    $outbox = Join-Path $stateDir 'versions-outbox.jsonl'
    if (-not @('deploy', 'rollback', 'heartbeat', 'start', 'stop', 'error').Contains($Event)) { Write-Output "версии: неизвестное событие $Event"; exit 0 }
    if ($Ok -eq '') { $okVal = -not @('rollback', 'error').Contains($Event) } else { $okVal = ($Ok -eq '1' -or $Ok -eq 'true') }
    $commit = $null; $branch = $null
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        $commit = (& git -C $repo rev-parse HEAD 2>$null); if ($LASTEXITCODE -ne 0) { $commit = $null }
        $branch = (& git -C $repo rev-parse --abbrev-ref HEAD 2>$null); if ($LASTEXITCODE -ne 0) { $branch = $null }
    }
    $version = $null
    $pyproject = Join-Path $repo 'pyproject.toml'
    if (Test-Path $pyproject) { $version = 'pyproject-' + ((Get-FileHash $pyproject -Algorithm SHA256).Hash.Substring(0, 12).ToLower()) }
    $body = @{ event = $Event; ok = $okVal; host = "windows-$env:COMPUTERNAME"; commit = $commit; branch = $branch; version = $version; message = $Message; finished_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') } | ConvertTo-Json -Compress
    $headers = @{ 'X-Board-Key' = $key; 'Content-Type' = 'application/json; charset=utf-8' }
    function Send-Report([string]$json) {
        try {
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
            Invoke-RestMethod -Method Put -Uri "$url/$project" -Headers $headers -Body $bytes -TimeoutSec 20 | Out-Null
            return 'ok'
        } catch {
            $status = $null
            try { $status = [int]$_.Exception.Response.StatusCode } catch { }
            if ($status -and $status -ge 400 -and $status -lt 500) { return "rejected:$status" }
            return 'offline'
        }
    }
    if (Test-Path $outbox) {
        $rest = @()
        foreach ($line in Get-Content $outbox) { if ($line.Trim() -eq '') { continue }; if ((Send-Report $line) -eq 'offline') { $rest += $line } }
        if ($rest.Count) { Set-Content -Path $outbox -Value $rest } else { Remove-Item $outbox -ErrorAction SilentlyContinue }
    }
    $r = Send-Report $body
    switch -Wildcard ($r) {
        'ok' { Write-Output "версии: отправлено $Event ok=$okVal $(if ($commit) { $commit.Substring(0, 7) }) → $url/$project" }
        'rejected:*' { Write-Output "версии: сайт отверг отчёт ($r) — проверьте VERSIONS_KEY" }
        default { Add-Content -Path $outbox -Value $body; Write-Output "версии: сайт недоступен — отчёт отложен в $outbox" }
    }
} catch {
    Write-Output "версии: ошибка отчёта: $_"
}
exit 0
