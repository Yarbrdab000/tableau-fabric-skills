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
  ./deploy.ps1 -ResourceGroup RY-fabric-demo `
               -TableauServer https://10ay.online.tableau.com `
               -TableauSite my-site `
               -ConnectedAppClientId <id> -ConnectedAppSecretId <id> `
               -ConnectedAppSecretValue <value> `
               -ServiceAccountUsername svc-mcp@company.com `
               -SidecarApiKey (New-Guid).Guid

.EXAMPLE
  # Per-user RLS: passthrough mode behind Easy Auth (Entra).
  ./deploy.ps1 -ResourceGroup RY-fabric-demo `
               -TableauServer https://10ay.online.tableau.com -TableauSite my-site `
               -ConnectedAppClientId <id> -ConnectedAppSecretId <id> -ConnectedAppSecretValue <value> `
               -ServiceAccountUsername svc-mcp@company.com `
               -IdentityMode passthrough -EnableEasyAuth `
               -EntraTenantId <tenant-guid> -EntraClientId <app-client-id>
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$ResourceGroup,
  [string]$Location = "eastus",
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
  [int]$MinReplicas = 0,
  [int]$MaxReplicas = 2
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($AllowApiKey -and [string]::IsNullOrWhiteSpace($SidecarApiKey)) {
  $SidecarApiKey = (New-Guid).Guid
  Write-Host "No -SidecarApiKey provided; generated one: $SidecarApiKey" -ForegroundColor Yellow
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
  --query properties.outputs -o json | ConvertFrom-Json

Write-Host ""
Write-Host "Deployment complete." -ForegroundColor Green
Write-Host "Identity mode: $($result.identityModeOut.value)  |  Easy Auth: $($result.easyAuthEnabled.value)"
Write-Host "MCP endpoint (register this in Copilot Studio):" -ForegroundColor Yellow
Write-Host "  $($result.mcpEndpoint.value)"
if ($AllowApiKey) {
  Write-Host "Send header  x-api-key: $SidecarApiKey" -ForegroundColor Yellow
}
Write-Host "Health check:"
Write-Host "  $($result.healthUrl.value)"
