# Dot-source me: loads the repo .env into the process environment
# and maps TG_TOKEN_<BOT> to TG_TOKEN for the given bot name.
# Precedence stays "the mapping you pass" (BLUEPRINT 7): the bots
# read os.environ, which this script populates per process.

function Import-MinionEnv {
    param(
        [Parameter(Mandatory = $true)][string]$EnvFile,
        [Parameter(Mandatory = $true)][string]$Bot
    )
    if (-not (Test-Path $EnvFile)) {
        throw "bad_config: $EnvFile not found; copy .env.example"
    }
    foreach ($line in Get-Content $EnvFile) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $pair = $trimmed.Split('=', 2)
        if ($pair.Count -ne 2) { continue }
        [Environment]::SetEnvironmentVariable($pair[0], $pair[1])
    }
    # One Telegram identity per bot (one getUpdates consumer per
    # token): TG_TOKEN_CENSOR_BLUR -> TG_TOKEN for censor-blur.
    $suffix = $Bot.ToUpper().Replace('-', '_')
    $token = [Environment]::GetEnvironmentVariable("TG_TOKEN_$suffix")
    if ($token) {
        [Environment]::SetEnvironmentVariable('TG_TOKEN', $token)
    }
}
