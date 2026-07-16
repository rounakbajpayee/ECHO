# test-ci.ps1 - Run full CI gate (lint + format + unit tests)
# Mirrors the GitHub Actions pipeline so you can catch failures locally.
# Usage: .\scripts\test-ci.ps1

param(
    [switch]$Fix   # If passed, ruff will auto-fix issues
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir   = Split-Path -Parent $ScriptDir

Write-Host "[ci] Activating virtual environment..." -ForegroundColor Cyan
$VenvActivate = Join-Path $RootDir ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $VenvActivate)) {
    Write-Error "[ci] .venv not found. Run: python -m venv .venv && .venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt"
    exit 1
}
. $VenvActivate

Set-Location $RootDir

# -----------------------------------------------------------
# Step 1: Ruff Format Check
# -----------------------------------------------------------
Write-Host "`n[ci] Step 1/3 - Ruff format check..." -ForegroundColor Yellow
if ($Fix) {
    ruff format src/ tests/
} else {
    ruff format --check src/ tests/
    if ($LASTEXITCODE -ne 0) {
        Write-Error "[ci] Format check failed. Run with -Fix to auto-fix."
        exit 1
    }
}

# -----------------------------------------------------------
# Step 2: Ruff Lint Check
# -----------------------------------------------------------
Write-Host "`n[ci] Step 2/3 - Ruff lint check..." -ForegroundColor Yellow
if ($Fix) {
    ruff check --fix src/ tests/
} else {
    ruff check src/ tests/
    if ($LASTEXITCODE -ne 0) {
        Write-Error "[ci] Lint check failed. Run with -Fix to auto-fix."
        exit 1
    }
}

# -----------------------------------------------------------
# Step 3: Unit Tests
# -----------------------------------------------------------
Write-Host "`n[ci] Step 3/3 - pytest unit tests..." -ForegroundColor Yellow
pytest tests/ -v --tb=short
if ($LASTEXITCODE -ne 0) {
    Write-Error "[ci] Tests failed."
    exit 1
}

Write-Host "`n[ci] All checks passed. " -ForegroundColor Green
