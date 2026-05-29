# dev.ps1 — Run ECHO locally for development
# Usage: .\scripts\dev.ps1 [--port 8001]

param(
    [int]$Port = 8001,
    [string]$LogLevel = "debug"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir   = Split-Path -Parent $ScriptDir

Write-Host "[dev] Starting ECHO development server..." -ForegroundColor Cyan

# Activate virtual environment
$VenvActivate = Join-Path $RootDir ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $VenvActivate)) {
    Write-Error "[dev] .venv not found. Run: python -m venv .venv && .venv\Scripts\pip install -r requirements.txt"
    exit 1
}
. $VenvActivate

# Ensure dev dependencies are installed
Write-Host "[dev] Verifying dev dependencies..." -ForegroundColor Yellow
pip install -q -r (Join-Path $RootDir "requirements-dev.txt")

# Change to src/ so config_defaults.json relative paths resolve correctly
Set-Location (Join-Path $RootDir "src")

Write-Host "[dev] Launching uvicorn on http://0.0.0.0:$Port (log-level: $LogLevel)" -ForegroundColor Green
uvicorn main:app --host 0.0.0.0 --port $Port --log-level $LogLevel --reload
