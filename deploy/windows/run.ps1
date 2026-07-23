# One script, one Task Scheduler entry -- starts the Windows bots.
#
# Windows runs exactly two bots: the printer (`print`) and the
# Downloads watcher (`catch`); every other bot runs in Docker on the
# NAS. It loads the same single .env the NAS uses (paths are validated
# for either OS), refreshes the Python requirements (reinstalls the
# minion_core package only when pyproject.toml changed, so logon stays
# fast), and launches both bots.
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
        $name = $pair[0].Trim()
        $value = $pair[1].Trim()
        # Strip one layer of surrounding single or double quotes, so a
        # path with spaces may be written quoted or bare -- both
        # DRIVE=C:\Users\a\My Drive and DRIVE='C:\Users\a\My Drive' work
        # (unquoted, the value would otherwise carry the quotes and read
        # as a non-absolute path).
        if ($value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or
             ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value)
    }
}

# TELEMETRY: pre-logging crashes (a bad_config from settings.load, a
# missing python) would vanish into the task's void otherwise, so
# each bot's launcher streams land next to its <bot>.log.
$logDir = Join-Path $env:DRIVE 'bots\_data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Keep the Python env in step with the checked-out code: (re)install
# the editable package + its requirements when pyproject.toml changed
# since the last successful install, or when minion_core cannot be
# imported at all (a fresh or broken env). A no-op on an unchanged env,
# so logon stays fast. This is an editable install, not a compile --
# every dependency ships a Windows wheel. We install the FULL runtime
# stack `.[ml,llm,links,tg]`: ml (torch/transformers/facenet, several
# GB) for local vision, llm (google-genai) for Gemini, links (yt-dlp),
# and tg (telethon) for the aggregator's session/login tool
# (python -m minions.aggregator.login). Only `dev` is left out. The
# first install is heavy but it re-runs only when pyproject.toml
# changes. A failed install is logged, never fatal: the bots still
# start on whatever is already installed.
$stateDir = Join-Path $env:DRIVE 'bots\_data\state'
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$stamp = Join-Path $stateDir 'windows-deps.stamp'
$depsLog = Join-Path $logDir 'deps.log'
$pyproject = Join-Path $repo 'pyproject.toml'
$hash = (Get-FileHash $pyproject -Algorithm SHA256).Hash
$prev = if (Test-Path $stamp) { (Get-Content $stamp -Raw).Trim() } else { '' }

# Probe the current env; a non-zero exit must not abort the script.
$importOk = $true
try {
    & python -c 'import minion_core' 2>$null
    $importOk = ($LASTEXITCODE -eq 0)
} catch {
    $importOk = $false
}

if ($hash -ne $prev -or -not $importOk) {
    Write-Output 'requirements changed: installing minion_core (.[ml,llm,links,tg])'
    try {
        & python -m pip install -e '.[ml,llm,links,tg]' 2>&1 |
            Out-File -FilePath $depsLog -Encoding utf8
        if ($LASTEXITCODE -eq 0) {
            Set-Content -Path $stamp -Value $hash
            Write-Output 'requirements up to date'
        } else {
            Write-Output 'pip install failed; starting on the existing env'
        }
    } catch {
        Write-Output "pip install error: $_; starting on the existing env"
    }
}

# Zero-config printing: if PRINT_SPOOLER is not set in .env, auto-locate
# SumatraPDF (the Windows equivalent of the NAS's `lp`) and point the
# print bot at it with the usual silent-print flags. An explicit
# PRINT_SPOOLER in .env always wins; the core never reads the host OS
# (REQ-PRT-001) -- the deployment (this launcher) chooses the spooler.
$configuredExe = if ($env:PRINT_SPOOLER) {
    ($env:PRINT_SPOOLER -split ';')[0]
} else { $null }
if (-not $configuredExe -or -not (Test-Path $configuredExe)) {
    $bases = @(
        $env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)}
    ) | Where-Object { $_ }
    $sumatra = $null
    foreach ($base in $bases) {
        $candidate = Join-Path $base 'SumatraPDF\SumatraPDF.exe'
        if (Test-Path $candidate) { $sumatra = $candidate; break }
    }
    if (-not $sumatra) {
        $cmd = Get-Command 'SumatraPDF.exe' -ErrorAction SilentlyContinue
        if ($cmd) { $sumatra = $cmd.Source }
    }
    if ($sumatra) {
        [Environment]::SetEnvironmentVariable(
            'PRINT_SPOOLER', "$sumatra;-print-to-default;-silent")
        Write-Output "print spooler: $sumatra"
    } else {
        Write-Output ('SumatraPDF not found -- install it or set ' +
            'PRINT_SPOOLER in .env; print will report printer_missing ' +
            'until then')
    }
}

# Start each bot as its own long-running process; the kernel
# supervises the belt inside each one.
foreach ($bot in @('print', 'catch')) {
    $package = $bot.Replace('-', '_')
    $log = Join-Path $logDir "$bot.launcher.log"
    Start-Process -FilePath 'python' `
        -ArgumentList '-m', "minions.bots.$package.main" `
        -WorkingDirectory $repo `
        -RedirectStandardOutput $log `
        -RedirectStandardError "$log.err" `
        -WindowStyle Hidden
    Write-Output "started $bot"
}
