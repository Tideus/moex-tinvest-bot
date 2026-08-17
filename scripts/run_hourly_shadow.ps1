$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Project virtual environment is missing: $pythonPath"
}

Set-Location -LiteralPath $projectRoot
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$outputPath = Join-Path $projectRoot "artifacts\hourly_shadow-$stamp.json"
$geoPath = Join-Path $projectRoot "artifacts\geo_events-$stamp.json"
$logPath = Join-Path $projectRoot "logs\hourly_shadow-$stamp.log"
$outboxPath = Join-Path $projectRoot "data\notifications.sqlite3"

& $pythonPath -m moex_bot.cli geo-refresh `
    --sources "config\geo_sources.json" `
    --output $geoPath *>> $logPath

# A failed collector still writes a stale payload. The shadow cycle consumes it and reduces risk.
& $pythonPath -m moex_bot.cli session-check *>> $logPath
if ($LASTEXITCODE -eq 3) {
    exit 0
}
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $pythonPath -m moex_bot.cli hourly-shadow `
    --config "config\shadow.json" `
    --universe "config\universe.json" `
    --portfolio "examples\portfolio_empty.json" `
    --geo $geoPath `
    --output $outputPath `
    --outbox $outboxPath *>> $logPath

$shadowStatus = $LASTEXITCODE

# ALGOPACK flow loads local .env inside the CLI. Missing entitlement is logged and cannot stop
# the shadow/risk loop.
$flowPath = Join-Path $projectRoot "artifacts\algopack_flow-$stamp.json"
& $pythonPath -m moex_bot.cli algopack-flow `
    --secid "SBER" `
    --futures-ticker "SBERF" `
    --output $flowPath `
    --outbox $outboxPath *>> $logPath

# Delivery is isolated and may retry next hour. It never changes the shadow exit code.
& $pythonPath -m moex_bot.cli telegram-send --outbox $outboxPath *>> $logPath

exit $shadowStatus
