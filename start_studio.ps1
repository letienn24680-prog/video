$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "[AI Video Studio] Created .env from .env.example" -ForegroundColor Yellow
    } else {
        Write-Warning "[AI Video Studio] Missing .env and .env.example!"
    }
}

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $PythonBin = $VenvPython
} else {
    $PythonBin = "python"
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  AI Video Studio - Local Development Server" -ForegroundColor Cyan
Write-Host "  URL:    http://127.0.0.1:8000" -ForegroundColor Green
Write-Host "  Docs:   http://127.0.0.1:8000/docs" -ForegroundColor DarkCyan
Write-Host "  Python: $PythonBin" -ForegroundColor Gray
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

& $PythonBin -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
