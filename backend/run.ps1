# Start the backend API server.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created backend/.env - add your ANTHROPIC_API_KEY to enable the Claude reader." -ForegroundColor Yellow
}

python -m pip install -q -r requirements.txt
Write-Host "Backend API  http://127.0.0.1:8000" -ForegroundColor Cyan
Write-Host "Docs         http://127.0.0.1:8000/docs" -ForegroundColor DarkCyan
python -m uvicorn app.main:app --port 8000
