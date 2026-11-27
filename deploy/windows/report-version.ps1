# Version report for the site admin's "Versions" page (daetotyoma.com/admin/versions/): what runs on this Windows PC.
# The cloud cannot see the home PC, so it reports itself. Calls:
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\report-version.ps1 start      # from run.ps1 at logon
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\report-version.ps1 heartbeat  # daily Task Scheduler entry
# Parameters: event (deploy|rollback|heartbeat|start|stop|error), ok, message. Commit -- git rev-parse HEAD when git exists, else
# a hash of pyproject.toml. Settings from .env: VERSIONS_KEY (report key, the same VERSIONS_KEY secret as in Pages, required), VERSIONS_URL, VERSIONS_PROJECT
# (default minion2-windows). No network -- the report goes to %DRIVE%\bots\_data\state\versions-outbox.jsonl and is resent later.
# Never fails the caller: any error is one output line and exit code 0.
# ASCII only (BLUEPRINT 4): a BOM-less .ps1 is read as ANSI by Windows PowerShell 5.1, so non-ASCII text would print garbled.
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
    if (-not $key) { Write-Output 'versions: VERSIONS_KEY is not set in .env -- report not sent'; exit 0 }
    $url = if ($cfg['VERSIONS_URL']) { $cfg['VERSIONS_URL'] } else { 'https://daetotyoma.com/admin/versions' }
    $project = if ($cfg['VERSIONS_PROJECT']) { $cfg['VERSIONS_PROJECT'] } else { 'minion2-windows' }
    $drive = if ($cfg['DRIVE']) { $cfg['DRIVE'] } else { $env:DRIVE }
    $stateDir = if ($drive) { Join-Path $drive 'bots\_data\state' } else { $PSScriptRoot }
    New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
    $outbox = Join-Path $stateDir 'versions-outbox.jsonl'
    if (-not @('deploy', 'rollback', 'heartbeat', 'start', 'stop', 'error').Contains($Event)) { Write-Output "versions: unknown event $Event"; exit 0 }
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
            if ($status -and $status -ge 400 -and $status -lt 500) {
                # The site's reply names the cause (secret VERSIONS_KEY not set / key mismatch) -- show it, not only the code
                $reply = ''
                try { $reply = (New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())).ReadToEnd().Split("`n")[0].Trim() } catch { }
                try { if (-not $reply -and $_.ErrorDetails) { $reply = "$($_.ErrorDetails.Message)".Split("`n")[0].Trim() } } catch { }
                return "rejected:$status $reply"
            }
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
        'ok' { Write-Output "versions: sent $Event ok=$okVal $(if ($commit) { $commit.Substring(0, 7) }) -> $url/$project" }
        'rejected:401*' { Write-Output "versions: site rejected the report ($r) -- VERSIONS_KEY in .env must match the site's VERSIONS_KEY secret (Cloudflare -> Pages -> Variables and Secrets -> Production)" }
        'rejected:*' { Write-Output "versions: site rejected the report ($r) -- check VERSIONS_URL and VERSIONS_PROJECT" }
        default { Add-Content -Path $outbox -Value $body; Write-Output "versions: site unreachable -- report queued in $outbox" }
    }
} catch {
    Write-Output "versions: report error: $_"
}
exit 0
