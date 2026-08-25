# Start the frontend server.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# The browser is what has to send the backend's API key, so this server has to
# know it. Read it from backend/.env - the one place it is configured - rather
# than making you keep two copies of the same secret in step.
$apiKey = ""
$envFile = Join-Path $PSScriptRoot "..\backend\.env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*GST_API_KEY\s*=\s*(.+?)\s*$') {
            $apiKey = $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
}

# Needs no packages: the frontend is plain HTML, CSS and JavaScript served by
# the Python standard library.
if ($apiKey) {
    python server.py --port 3000 --api http://127.0.0.1:8000 --api-key $apiKey
} else {
    python server.py --port 3000 --api http://127.0.0.1:8000
}
