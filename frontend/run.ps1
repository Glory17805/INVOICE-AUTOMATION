# Start the frontend server.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# No packages, and no credentials. People sign in with their own accounts, so
# this server only needs to know where the backend is - it never holds a secret
# and never hands one to the browser.
python server.py --port 3000 --api http://127.0.0.1:8000
