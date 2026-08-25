# Start the frontend server.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Needs no packages: the frontend is plain HTML, CSS and JavaScript served by
# the Python standard library.
python server.py --port 3000 --api http://127.0.0.1:8000
