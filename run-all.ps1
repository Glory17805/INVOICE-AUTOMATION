# Start both servers, each in its own window, and open the app.
#
# They are independent processes: either can be restarted without the other,
# and closing one window stops only that service.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Start-Process powershell -ArgumentList "-NoExit", "-File", "$PSScriptRoot\backend\run.ps1"
Start-Sleep -Seconds 3
Start-Process powershell -ArgumentList "-NoExit", "-File", "$PSScriptRoot\frontend\run.ps1"
Start-Sleep -Seconds 2

Write-Host ""
Write-Host "Backend   http://127.0.0.1:8000" -ForegroundColor Cyan
Write-Host "Frontend  http://127.0.0.1:3000" -ForegroundColor Cyan
Write-Host ""
Write-Host "Close either window to stop that service." -ForegroundColor DarkGray
Start-Process "http://127.0.0.1:3000"
