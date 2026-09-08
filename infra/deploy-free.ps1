<#
.SYNOPSIS
  Deploy the GST automation app to Azure at no cost.

.DESCRIPTION
  The free shape: Container Apps for the API (scaling to zero between
  sessions, inside the monthly free grant), Static Web Apps for the frontend,
  GitHub Container Registry for the images, and Azure Files for the workbooks
  and the SQLite database. The only charge is stored bytes on the share, which
  for a few hundred MB is a few cents a month.

  Run after `az login`. Safe to run repeatedly.

  Two orderings matter:
    * the master workbook is uploaded before the API is created, because the
      API refuses to start without it;
    * the frontend is built after the API exists, because its Content-Security
      -Policy has to name the API's real address.

.EXAMPLE
  ./infra/deploy-free.ps1 -ResourceGroup ira-gst -GithubOwner Glory17805 `
      -MasterWorkbook "D:\IRA\01 Ira Innovations May-26 GST Calculation.xlsx"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $ResourceGroup,
    [Parameter(Mandatory = $true)][string] $GithubOwner,
    [Parameter(Mandatory = $true)][string] $MasterWorkbook,
    [string] $Prefix = 'iragst',
    [string] $Location = 'centralindia',
    [securestring] $GhcrToken,
    [securestring] $AnthropicKey,
    [switch] $SkipInfra
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Step($text) { Write-Host "`n=== $text" -ForegroundColor Cyan }

Step 'Checking prerequisites'
foreach ($tool in 'az', 'docker') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool not found." }
}
if (-not (Test-Path $MasterWorkbook)) { throw "Master workbook not found at $MasterWorkbook" }

$account = az account show 2>$null | ConvertFrom-Json
if (-not $account) { throw 'Not signed in. Run: az login' }
Write-Host "  subscription : $($account.name)"

$plainGhcr = if ($GhcrToken) { [System.Net.NetworkCredential]::new('', $GhcrToken).Password } else { '' }
$plainKey  = if ($AnthropicKey) { [System.Net.NetworkCredential]::new('', $AnthropicKey).Password } else { '' }

# The containerapp commands live in an extension. Installing it up front avoids
# an interactive prompt in the middle of a deployment.
az extension add --name containerapp --upgrade --only-show-errors --output none 2>$null
az extension add --name staticwebapp --upgrade --only-show-errors --output none 2>$null

Step "Resource group $ResourceGroup"
if (-not (az group exists --name $ResourceGroup | ConvertFrom-Json)) {
    az group create --name $ResourceGroup --location $Location --output none
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create $ResourceGroup. This needs Contributor on the group; " +
              "Web Plan Contributor is not enough."
    }
}
Write-Host '  ready'

# --- Images, before the infrastructure that pulls them ---------------------

Step 'Building and pushing images to ghcr.io'
$owner = $GithubOwner.ToLower()
$imageBase = "ghcr.io/$owner"

if ($plainGhcr) {
    $plainGhcr | docker login ghcr.io --username $GithubOwner --password-stdin
    if ($LASTEXITCODE -ne 0) { throw 'docker login to ghcr.io failed.' }
} else {
    Write-Host '  no token supplied - assuming you are already logged in to ghcr.io'
}

# Only the API is a container on this tier - the frontend is static files on
# Static Web Apps, built further down once the API's address is known.
#
# linux/amd64 explicitly: Container Apps runs amd64, and an ARM image fails as
# a container that exits immediately with no useful log.
docker build --platform linux/amd64 -t "$imageBase/gst-api:latest" "$root/backend"
if ($LASTEXITCODE -ne 0) { throw 'Build of gst-api failed.' }
docker push "$imageBase/gst-api:latest"
if ($LASTEXITCODE -ne 0) {
    throw 'Push of gst-api failed. If this is the first push, the package may ' +
          'need to be made public in your GitHub packages settings, or pass -GhcrToken.'
}

# --- Storage first, so the workbook is in place before the API starts ------

if (-not $SkipInfra) {
    Step 'Deploying infrastructure'
    az deployment group create `
        --resource-group $ResourceGroup `
        --template-file "$PSScriptRoot/main-free.bicep" `
        --parameters prefix=$Prefix location=$Location githubOwner=$GithubOwner `
                     anthropicApiKey=$plainKey ghcrToken=$plainGhcr `
        --output none
    if ($LASTEXITCODE -ne 0) { throw 'Infrastructure deployment failed.' }
    Write-Host '  done'
}

Step 'Reading deployment outputs'
$outputs = az deployment group show --resource-group $ResourceGroup --name main-free `
    --query properties.outputs --output json | ConvertFrom-Json
$apiUrl = $outputs.apiUrl.value
$webUrl = $outputs.webUrl.value
$webName = $outputs.webName.value
$storageAccount = $outputs.storageAccount.value
$shareName = $outputs.shareName.value
Write-Host "  api : $apiUrl"
Write-Host "  web : $webUrl"

Step 'Uploading the master workbook'
$storageKey = az storage account keys list --account-name $storageAccount `
    --resource-group $ResourceGroup --query '[0].value' --output tsv
az storage directory create --account-name $storageAccount --account-key $storageKey `
    --share-name $shareName --name master --output none 2>$null
az storage file upload --account-name $storageAccount --account-key $storageKey `
    --share-name $shareName --path master/workbook.xlsx --source $MasterWorkbook --output none
Write-Host '  uploaded'

# The API may have started before the workbook arrived and crash-looped; a
# restart now that it is there costs seconds and saves a confusing first look.
az containerapp revision restart --resource-group $ResourceGroup --name "$Prefix-api" `
    --revision (az containerapp revision list -g $ResourceGroup -n "$Prefix-api" `
                --query '[0].name' -o tsv) --output none 2>$null

# --- Frontend, once the API address is known -------------------------------

Step 'Building the frontend against the real API address'
python "$root/frontend/build_static.py" --api $apiUrl --out "$root/frontend/dist"
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }

Step 'Uploading the frontend'
$swaToken = az staticwebapp secrets list --name $webName --resource-group $ResourceGroup `
    --query 'properties.apiKey' --output tsv
npx --yes @azure/static-web-apps-cli deploy "$root/frontend/dist" `
    --deployment-token $swaToken --env production
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'SWA upload failed. Install Node, or upload frontend/dist through the portal.'
}

Step 'Waiting for the API to report ready'
$ready = $false
foreach ($attempt in 1..30) {
    Start-Sleep -Seconds 15
    try {
        $probe = Invoke-RestMethod -Uri "$apiUrl/api/ready" -TimeoutSec 25 -ErrorAction Stop
        if ($probe.status -eq 'ready') {
            $ready = $true
            Write-Host "  ready after $($attempt * 15)s"
            foreach ($name in $probe.checks.PSObject.Properties.Name) {
                $c = $probe.checks.$name
                $flag = if ($c.degraded) { ' DEGRADED' } else { '' }
                Write-Host ("    {0,-16} {1}{2}  {3}" -f $name, $(if ($c.ok) {'ok'} else {'FAIL'}), $flag, $c.detail)
            }
            break
        }
    } catch {
        Write-Host "  attempt $attempt - waking up (it scales to zero when idle)"
    }
}

if (-not $ready) {
    Write-Warning 'The API did not report ready. Logs:'
    Write-Host "  az containerapp logs show -g $ResourceGroup -n $Prefix-api --follow"
    exit 1
}

Write-Host "`nDeployed, at no cost beyond stored bytes." -ForegroundColor Green
Write-Host "  Open $webUrl and create the first account - it becomes the administrator."
Write-Host "  The API sleeps when idle, so the first request after a quiet spell takes a few seconds."
