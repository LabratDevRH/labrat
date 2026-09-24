# Start the website's live-training feed (live/publish_training.py) in the background.
# It watches runs/ and streams any training run whose log.jsonl is being written to the relay; idle otherwise.
# The publish token is read from .env (LABRAT_PUBLISH_TOKEN) at run time; it is never printed or written anywhere.
# -Python: full path to python.exe (a process started outside your login shell may not have python on PATH).
param([string]$Python = 'python')
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root 'runs\publisher.log'
try {
    Set-Location $root
    $line = Select-String -Path (Join-Path $root '.env') -Pattern '^LABRAT_PUBLISH_TOKEN=' | Select-Object -First 1
    if (-not $line) { throw '.env has no LABRAT_PUBLISH_TOKEN' }
    $env:LABRAT_PUBLISH_TOKEN = $line.Line.Substring($line.Line.IndexOf('=') + 1).Trim()
    $env:PYTHONUNBUFFERED = '1'
    $relay = if ($env:LABRAT_RELAY_PUBLISH) { $env:LABRAT_RELAY_PUBLISH } else { 'wss://labrat-relay-production.up.railway.app/publish' }
    & $Python live/publish_training.py --watch runs --relay $relay 2>&1 | ForEach-Object { "$_" } |
        Out-File -FilePath $log -Append -Encoding utf8
} catch {
    "start_live_feed failed: $($_.Exception.Message)" | Out-File -FilePath $log -Append -Encoding utf8
}
