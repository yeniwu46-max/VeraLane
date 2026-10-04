param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
foreach ($toolName in @('uv','npm')) {
    if (-not (Get-Command $toolName -ErrorAction SilentlyContinue)) { throw "Missing required tool: $toolName" }
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.env'))) {
    Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination (Join-Path $projectRoot '.env')
}
Push-Location (Join-Path $projectRoot 'frontend')
try {
    npm ci
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency install failed' }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
} finally { Pop-Location }
Push-Location (Join-Path $projectRoot 'backend')
try {
    uv sync --frozen
    if ($LASTEXITCODE -ne 0) { throw 'Backend dependency install failed' }
    Write-Host "Open http://127.0.0.1:$Port (local fictional banking demo). Ctrl+C stops the server."
    uv run --frozen --env-file ../.env uvicorn app.main:app --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'Demo server stopped with an error' }
} finally { Pop-Location }
