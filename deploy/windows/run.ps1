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
# every dependency ships a Windows wheel. The .[llm] extra adds
# google-genai (catch classifies via Gemini on Windows, since the NAS
# ollama is not exposed); the heavy ml/links extras are not needed
# here. A failed install is logged, never fatal: the bots still start
# on whatever is already installed.
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
    Write-Output 'requirements changed: installing minion_core (.[llm])'
    try {
        & python -m pip install -e '.[llm]' 2>&1 |
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
