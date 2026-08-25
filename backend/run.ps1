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

# Deliberately one worker.
#
# Posting a row is read-the-register, pick-the-next-free-line, write, save.
# Two workers would interleave those steps against the same .xlsx and the
# second save would drop the first one's row, with no error raised anywhere.
# The process takes an exclusive lock on backend/data/ at startup, so a second
# one refuses to start rather than corrupting the first - but do not reach for
# --workers here expecting throughput. Scaling this safely means moving the
# workbook writes behind a single queue, not adding processes.
python -m uvicorn app.main:app --port 8000 --workers 1
