<#
.SYNOPSIS
  Deploy the Play 1 landing zone (official Tableau MCP image + auth sidecar) to Azure
  Container Apps.

.DESCRIPTION
  CLI alternative to the "Deploy to Azure" button. Requires the Azure CLI (`az login`
  first). The OFFICIAL Tableau MCP image is pulled from GHCR; the sidecar image is built
  + published by .github/workflows/build-sidecar-image.yml.

.EXAMPLE
  # Simplest: service-account mode, api-key auth (Copilot Studio custom connector).
  ./deploy.ps1 -ResourceGroup rg-tableau-mcp `
               -TableauServer https://10ay.online.tableau.com `
               -TableauSite my-site `
               -ConnectedAppClientId <id> -ConnectedAppSecretId <id> `
               -ConnectedAppSecretValue <value> `
               -ServiceAccountUsername svc-mcp@company.com `
               -SidecarApiKey (New-Guid).Guid

.EXAMPLE
  # Per-user RLS: passthrough mode behind Easy Auth (Entra).
  ./deploy.ps1 -ResourceGroup rg-tableau-mcp `
               -TableauServer https://10ay.online.tableau.com -TableauSite my-site `
               -ConnectedAppClientId <id> -ConnectedAppSecretId <id> -ConnectedAppSecretValue <value> `
               -ServiceAccountUsername svc-mcp@company.com `
               -IdentityMode passthrough -EnableEasyAuth `
               -EntraTenantId <tenant-guid> -EntraClientId <app-client-id>
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$ResourceGroup,
  # Default "" => derived from the resource group's region (falls back to eastus only when creating a new RG).
  [string]$Location = "",
  [string]$ContainerAppName = "tableau-mcp",
  # Readable tag default. Hardening opt-in: pin by digest so a retag can't change the deploy:
  #   ghcr.io/tableau/tableau-mcp:2.7.4@sha256:10a043fea52c6152ab1d86222540aa1bc2ba021411dc772bc3f48a3c36b54de1
  # Version-coupled: the upstream path (/tableau-mcp) + ENABLE_MCP_SITE_SETTINGS default track this tag.
  [string]$TableauMcpImage = "ghcr.io/tableau/tableau-mcp:2.7.4",
  [string]$SidecarImage = "ghcr.io/yarbrdab000/tableau-fabric-ai-bridge-sidecar:latest",
  [Parameter(Mandatory = $true)][string]$TableauServer,
  [string]$TableauSite = "",
  [Parameter(Mandatory = $true)][string]$ConnectedAppClientId,
  [Parameter(Mandatory = $true)][string]$ConnectedAppSecretId,
  [Parameter(Mandatory = $true)][string]$ConnectedAppSecretValue,
  [Parameter(Mandatory = $true)][string]$ServiceAccountUsername,
  [bool]$AllowApiKey = $true,
  [string]$SidecarApiKey = "",
  [ValidateSet("service_account", "passthrough")][string]$IdentityMode = "service_account",
  [ValidateSet("direct", "transform", "explicit")][string]$UpnMappingMode = "direct",
  [string]$UpnDomainFrom = "",
  [string]$UpnDomainTo = "",
  [string]$EntraTenantId = "",
  [switch]$EnableEasyAuth,
  [string]$EntraClientId = "",
  [switch]$UseKeyVault,
  # Tool curation forwarded to the official server (defaults match main.bicep). Add 'pulse' to expose
  # Pulse tools (also requires the Pulse insight scope family on the Connected App -- see resources/identity-modes.md).
  [string]$IncludeTools = "datasource,content-exploration,workbook,view,pulse",
  [string]$MaxResultLimits = "query-datasource:100",
  [int]$MinReplicas = 0,
  [int]$MaxReplicas = 2
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# Resolve the deploy region from the resource group so resources never land cross-region.
# An existing RG's region wins (the deployment targets that RG); fall back to -Location (or eastus)
# only when the RG has to be created.
$rgExists = (az group exists --name $ResourceGroup) -eq "true"
if ($rgExists) {
  $rgLocation = (az group show --name $ResourceGroup --query location -o tsv).Trim()
  if ([string]::IsNullOrWhiteSpace($Location)) {
    $Location = $rgLocation
    Write-Host "Using resource group '$ResourceGroup' region: $Location" -ForegroundColor Cyan
  }
  elseif ($Location -ne $rgLocation) {
    Write-Host "WARNING: -Location '$Location' differs from resource group region '$rgLocation'; using '$rgLocation' to avoid cross-region resources." -ForegroundColor Yellow
    $Location = $rgLocation
  }
}
else {
  if ([string]::IsNullOrWhiteSpace($Location)) { $Location = "eastus" }
  Write-Host "Resource group '$ResourceGroup' not found; creating it in '$Location'." -ForegroundColor Cyan
  az group create --name $ResourceGroup --location $Location | Out-Null
}

if ($AllowApiKey -and [string]::IsNullOrWhiteSpace($SidecarApiKey)) {
  $SidecarApiKey = (New-Guid).Guid
  Write-Host "No -SidecarApiKey provided; generated a random key (not printed). Retrieve it after deploy from the 'sidecar-api-key' Container App secret (command shown below)." -ForegroundColor Yellow
}

Write-Host "Deploying Play 1 landing zone to resource group '$ResourceGroup'..." -ForegroundColor Cyan

$result = az deployment group create `
  --resource-group $ResourceGroup `
  --template-file "$here/main.bicep" `
  --parameters `
    location=$Location `
    containerAppName=$ContainerAppName `
    tableauMcpImage=$TableauMcpImage `
    sidecarImage=$SidecarImage `
    tableauServer=$TableauServer `
    tableauSite=$TableauSite `
    connectedAppClientId=$ConnectedAppClientId `
    connectedAppSecretId=$ConnectedAppSecretId `
    connectedAppSecretValue=$ConnectedAppSecretValue `
    serviceAccountUsername=$ServiceAccountUsername `
    allowApiKey=$AllowApiKey `
    sidecarApiKey=$SidecarApiKey `
    identityMode=$IdentityMode `
    upnMappingMode=$UpnMappingMode `
    upnDomainFrom=$UpnDomainFrom `
    upnDomainTo=$UpnDomainTo `
    entraTenantId=$EntraTenantId `
    enableEasyAuth=$($EnableEasyAuth.IsPresent) `
    entraClientId=$EntraClientId `
    useKeyVault=$($UseKeyVault.IsPresent) `
    minReplicas=$MinReplicas `
    maxReplicas=$MaxReplicas `
    includeTools=$IncludeTools `
    maxResultLimits=$MaxResultLimits `
  --query properties.outputs -o json | ConvertFrom-Json

Write-Host ""
Write-Host "Deployment complete." -ForegroundColor Green
Write-Host "Identity mode: $($result.identityModeOut.value)  |  Easy Auth: $($result.easyAuthEnabled.value)"
Write-Host "MCP endpoint (register this in Copilot Studio):" -ForegroundColor Yellow
Write-Host "  $($result.mcpEndpoint.value)"
if ($AllowApiKey) {
  Write-Host "Caller auth: send the shared key as header  x-api-key: <sidecarApiKey>" -ForegroundColor Yellow
  Write-Host "  The key is stored as the 'sidecar-api-key' Container App secret and is NOT printed here."
  Write-Host "  Retrieve it without echoing it into a transcript, e.g.:"
  Write-Host "    az containerapp secret show -n $ContainerAppName -g $ResourceGroup --secret-name sidecar-api-key --query value -o tsv"
  Write-Host "  (If you deployed with -UseKeyVault, read it from your Key Vault instead.)"
}
Write-Host "Health check:"
Write-Host "  $($result.healthUrl.value)"

# Tool-curation visibility: make the enabled set + Pulse gating obvious instead of a mystery.
Write-Host ""
Write-Host "Curated tools (INCLUDE_TOOLS = '$IncludeTools'):" -ForegroundColor Cyan
Write-Host "  Default set is the full NL-analytics suite: data queries (list-datasources, get-datasource-metadata, query-datasource), content search, workbooks, views, and Pulse insights."
Write-Host "  Row caps (MAX_RESULT_LIMITS = '$MaxResultLimits')."
Write-Host "  Pulse, workbooks, and views are ON by default. Content/workbooks need only tableau:content:read; views also need tableau:views:download; Pulse needs the 5 insight scopes (tableau:insight_definitions_metrics:read, tableau:insight_metrics:read, tableau:metric_subscriptions:read, tableau:insights:read, tableau:insight_brief:create). Tools whose scopes are not granted return 401 at call time, but the server stays healthy. Trim -IncludeTools to slim the set."

# Emit a ready-to-import Copilot Studio connector with host pre-filled (removes the manual host edit).
$fqdn = (($result.mcpEndpoint.value) -replace '^https://', '') -replace '/mcp/?$', ''
$swaggerSrc = Join-Path $here '..\copilot-studio\mcp-connector.swagger.yaml'
if (Test-Path $swaggerSrc) {
  $swaggerOut = Join-Path (Get-Location) 'mcp-connector.generated.swagger.yaml'
  $swagger = Get-Content -Raw $swaggerSrc
  $swagger = [regex]::Replace($swagger, '(?m)^host:.*$', "host: $fqdn")
  [System.IO.File]::WriteAllText($swaggerOut, $swagger, (New-Object System.Text.UTF8Encoding($false)))
  Write-Host ""
  Write-Host "Copilot Studio connector written with host pre-filled (no manual edit needed):" -ForegroundColor Cyan
  Write-Host "  $swaggerOut"
}
