<#
.SYNOPSIS
  Build, push and deploy the GST automation app to Azure.

.DESCRIPTION
  Run after `az login`. Safe to run repeatedly: the Bicep template reconciles
  rather than duplicates, and re-running after a code change rebuilds and
  redeploys the images.

  The order matters in one place. The API refuses to start without the master
  workbook, so the workbook is uploaded to the file share BEFORE the web apps
  are pointed at their images. Deploying first and uploading afterwards leaves
  the container crash-looping on a missing path, which looks like a broken
  image rather than a missing file.

.EXAMPLE
  ./infra/deploy.ps1 -ResourceGroup ira-gst -MasterWorkbook "D:\IRA\01 Ira Innovations May-26 GST Calculation.xlsx"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $ResourceGroup,
    [Parameter(Mandatory = $true)][string] $MasterWorkbook,
    [string] $Prefix = 'iragst',
    [string] $Location = 'centralindia',
    [securestring] $DbPassword,
    [securestring] $AnthropicKey,
    [switch] $SkipInfra
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Step($text) { Write-Host "`n=== $text" -ForegroundColor Cyan }

# --- Preconditions ---------------------------------------------------------

Step 'Checking prerequisites'
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw 'Azure CLI not found. Install it, then run az login.'
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker not found. It is needed to build the images.'
}
if (-not (Test-Path $MasterWorkbook)) {
    throw "Master workbook not found at $MasterWorkbook"
}

$account = az account show 2>$null | ConvertFrom-Json
if (-not $account) { throw 'Not signed in. Run: az login' }
Write-Host "  subscription : $($account.name)"
Write-Host "  tenant       : $($account.tenantId)"

if (-not $DbPassword) {
    # Generated rather than prompted: this password is only ever used by the
    # app, through an app setting, so a human never needs to type or keep it.
    $bytes = [byte[]]::new(24)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $plainDbPassword = ([Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', '') + 'aZ9!'
    Write-Host '  db password  : generated'
} else {
    $plainDbPassword = [System.Net.NetworkCredential]::new('', $DbPassword).Password
    Write-Host '  db password  : supplied'
}

$plainAnthropicKey = if ($AnthropicKey) {
    [System.Net.NetworkCredential]::new('', $AnthropicKey).Password
} else { '' }

# --- Resource group --------------------------------------------------------

Step "Resource group $ResourceGroup"
az group create --name $ResourceGroup --location $Location --output none
Write-Host '  ready'

# --- Infrastructure --------------------------------------------------------

if (-not $SkipInfra) {
    Step 'Deploying infrastructure (this takes several minutes - Postgres is the slow part)'
    az deployment group create `
        --resource-group $ResourceGroup `
        --template-file "$PSScriptRoot/main.bicep" `
        --parameters prefix=$Prefix location=$Location `
                     dbAdminPassword=$plainDbPassword `
                     anthropicApiKey=$plainAnthropicKey `
        --output none
    if ($LASTEXITCODE -ne 0) { throw 'Infrastructure deployment failed.' }
    Write-Host '  done'
}

Step 'Reading deployment outputs'
$outputs = az deployment group show --resource-group $ResourceGroup --name main `
    --query properties.outputs --output json | ConvertFrom-Json
$registry = $outputs.registryLoginServer.value
$registryName = $outputs.registryName.value
$storageAccount = $outputs.storageAccount.value
$shareName = $outputs.shareName.value
$apiUrl = $outputs.apiUrl.value
$webUrl = $outputs.webUrl.value
Write-Host "  registry : $registry"
Write-Host "  api      : $apiUrl"
Write-Host "  web      : $webUrl"

# --- Master workbook, before anything tries to start -----------------------

Step 'Uploading the master workbook'
$storageKey = az storage account keys list --account-name $storageAccount `
    --resource-group $ResourceGroup --query '[0].value' --output tsv
az storage directory create --account-name $storageAccount --account-key $storageKey `
    --share-name $shareName --name master --output none 2>$null
az storage file upload --account-name $storageAccount --account-key $storageKey `
    --share-name $shareName --path master/workbook.xlsx --source $MasterWorkbook --output none
Write-Host "  uploaded to //$storageAccount/$shareName/master/workbook.xlsx"

# --- Images ----------------------------------------------------------------

Step 'Building and pushing images'
az acr login --name $registryName --output none

# Built for linux/amd64 explicitly: App Service runs amd64, and a machine with
# an ARM Docker default would otherwise push an image that cannot start there
# - and the failure appears as a container that exits immediately with no
# useful log.
docker build --platform linux/amd64 -t "$registry/gst-api:latest" "$root/backend"
if ($LASTEXITCODE -ne 0) { throw 'Backend image build failed.' }
docker build --platform linux/amd64 -t "$registry/gst-web:latest" "$root/frontend"
if ($LASTEXITCODE -ne 0) { throw 'Frontend image build failed.' }

docker push "$registry/gst-api:latest"
if ($LASTEXITCODE -ne 0) { throw 'Backend image push failed.' }
docker push "$registry/gst-web:latest"
if ($LASTEXITCODE -ne 0) { throw 'Frontend image push failed.' }

# --- Restart onto the new images -------------------------------------------

Step 'Restarting the apps onto the new images'
az webapp restart --resource-group $ResourceGroup --name "$Prefix-api" --output none
az webapp restart --resource-group $ResourceGroup --name "$Prefix-web" --output none

Step 'Waiting for the API to report ready'
$ready = $false
foreach ($attempt in 1..40) {
    Start-Sleep -Seconds 15
    try {
        $probe = Invoke-RestMethod -Uri "$apiUrl/api/ready" -TimeoutSec 20 -ErrorAction Stop
        if ($probe.status -eq 'ready') {
            $ready = $true
            Write-Host "  ready after $($attempt * 15)s"
            foreach ($name in $probe.checks.PSObject.Properties.Name) {
                $check = $probe.checks.$name
                $flag = if ($check.degraded) { ' DEGRADED' } else { '' }
                Write-Host ("    {0,-16} {1}{2}  {3}" -f $name, $(if ($check.ok) { 'ok' } else { 'FAIL' }), $flag, $check.detail)
            }
            break
        }
    } catch {
        Write-Host "  attempt $attempt - not up yet"
    }
}

if (-not $ready) {
    Write-Warning 'The API did not report ready. Check the logs:'
    Write-Host "  az webapp log tail -g $ResourceGroup -n $Prefix-api"
    exit 1
}

Write-Host "`nDeployed." -ForegroundColor Green
Write-Host "  Open $webUrl and create the first account - it becomes the administrator."
Write-Host "  To migrate an existing store.db, see infra/README.md."
