// Tableau-Fabric-AI-Bridge — Play 1 landing zone.
//
// One-click deployment of the OFFICIAL Tableau MCP server image to Azure Container Apps,
// fronted by our auth sidecar that adds the Microsoft glue (x-api-key for Copilot Studio,
// optional Entra -> Tableau identity passthrough for per-user RLS).
//
//   client ──> [Easy Auth?] ──> sidecar (public, :9000) ──> tableau-mcp (localhost :8000) ──> Tableau
//
// Both containers run in ONE Container App (shared localhost). Only the sidecar port is
// exposed via ingress; the official server is never reachable from the internet, so the
// sidecar is the complete auth boundary (DANGEROUSLY_DISABLE_OAUTH=true is safe).
//
// Deploy from the portal via the "Deploy to Azure" button (see ../../Play1_README.md), or:
//   az deployment group create -g <rg> -f main.bicep -p @main.parameters.json

// ---------------------------------------------------------------------------------------
// Core
// ---------------------------------------------------------------------------------------

@description('Azure region for all resources. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Name for the Container App (also used as the public subdomain).')
param containerAppName string = 'tableau-mcp'

@description('OFFICIAL Tableau MCP image. Readable tag default; overridable. For maximum reproducibility, pin by digest (see the hardening opt-in below / deploy-azure.md).')
// Hardening opt-in — pin by digest so a retag can never change what you deploy:
//   ghcr.io/tableau/tableau-mcp:2.7.4@sha256:10a043fea52c6152ab1d86222540aa1bc2ba021411dc772bc3f48a3c36b54de1
// Version-coupled: the UPSTREAM_MCP_URL path (/tableau-mcp) and the ENABLE_MCP_SITE_SETTINGS=false
// default below track this 2.7.x tag — re-verify both if you bump the image.
param tableauMcpImage string = 'ghcr.io/tableau/tableau-mcp:2.7.4'

@description('Our auth sidecar image (built + published by .github/workflows/build-sidecar-image.yml).')
param sidecarImage string = 'ghcr.io/yarbrdab000/tableau-fabric-ai-bridge-sidecar:latest'

// ---------------------------------------------------------------------------------------
// Tableau site + Connected App (Direct Trust)
// ---------------------------------------------------------------------------------------

@description('Tableau server/pod URL, e.g. https://10ay.online.tableau.com')
param tableauServer string

@description('Tableau site content URL (the slug in the site URL). Empty = Default site.')
param tableauSite string = ''

@description('Connected App client ID (Tableau > Settings > Connected Apps).')
param connectedAppClientId string

@description('Connected App secret ID (the non-sensitive identifier shown next to the client ID).')
#disable-next-line secure-secrets-in-params
param connectedAppSecretId string

@description('Connected App secret VALUE (sensitive).')
@secure()
param connectedAppSecretValue string

@description('Tableau username the SERVICE ACCOUNT acts as. Required by the official server at startup; also the identity used in service_account mode. A Site Admin bypasses RLS.')
param serviceAccountUsername string

@description('Official server INCLUDE_TOOLS — comma-separated tool or group names to expose. Default exposes the full NL-analytics set (data queries, content search, workbooks, views, Pulse). Empty = all ~20 tools.')
param includeTools string = 'datasource,content-exploration,workbook,view,pulse'

@description('Official server MAX_RESULT_LIMITS — per-tool row caps (e.g. "query-datasource:100") to prevent payload blowups. Empty = server defaults.')
param maxResultLimits string = 'query-datasource:100'

// ---------------------------------------------------------------------------------------
// Caller auth + identity mode
// ---------------------------------------------------------------------------------------

@description('Allow callers to authenticate to the sidecar with a shared x-api-key. Recommended for Copilot Studio custom connectors.')
param allowApiKey bool = true

@description('Shared API key callers present as the "x-api-key" header (or "Authorization: Bearer <key>"). Required when allowApiKey is true. Invent a long random string.')
@secure()
param sidecarApiKey string = ''

@description('Identity mode. service_account = all queries run as the service account (no per-user RLS, works anywhere). passthrough = map the caller Entra UPN -> Tableau user for per-user RLS (requires Easy Auth or APIM in front).')
@allowed([
  'service_account'
  'passthrough'
])
param identityMode string = 'service_account'

@description('UPN -> Tableau username mapping (passthrough only). direct = UPN is the username; transform = swap domain; explicit = use a JSON map provided to the sidecar out-of-band.')
@allowed([
  'direct'
  'transform'
  'explicit'
])
param upnMappingMode string = 'direct'

@description('Source UPN domain for upnMappingMode=transform (e.g. contoso.com).')
param upnDomainFrom string = ''

@description('Target Tableau domain for upnMappingMode=transform.')
param upnDomainTo string = ''

@description('Entra tenant ID. Used for the Easy Auth issuer and recorded for the sidecar identity cache key.')
param entraTenantId string = ''

// ---------------------------------------------------------------------------------------
// Optional Entra "Easy Auth" front door
// ---------------------------------------------------------------------------------------

@description('Enable Container Apps Easy Auth (Microsoft Entra) in front of the sidecar. Required for passthrough unless an external gateway (APIM) supplies the identity. You must pre-create an Entra app registration and pass its client ID.')
param enableEasyAuth bool = false

@description('Entra app registration (client) ID for Easy Auth. Required when enableEasyAuth is true.')
param entraClientId string = ''

// ---------------------------------------------------------------------------------------
// Secrets store + scaling
// ---------------------------------------------------------------------------------------

@description('Store secrets in Azure Key Vault (pulled via a user-assigned managed identity) instead of plain Container App secrets. Production-leaning; adds a role-assignment propagation step. Leave false for the simplest reliable one-click.')
param useKeyVault bool = false

@description('Minimum replicas. 0 enables scale-to-zero (near-zero idle cost).')
@minValue(0)
@maxValue(5)
param minReplicas int = 0

@description('Maximum replicas.')
@minValue(1)
@maxValue(10)
param maxReplicas int = 2

// ---------------------------------------------------------------------------------------
// Validation (fail fast in the portal)
// ---------------------------------------------------------------------------------------

var isPassthrough = identityMode == 'passthrough'

#disable-next-line no-hardcoded-env-urls
var easyAuthIssuer = 'https://login.microsoftonline.com/${entraTenantId}/v2.0'

// NOTE: conditional-required rules (sidecarApiKey when allowApiKey; entraClientId/tenant when
// enableEasyAuth; transform domains in passthrough) are enforced at container startup — the
// sidecar (config.py) and the official server fail fast with a clear message if misconfigured.

var sidecarPort = 9000
var mcpPort = 8000
var logName = '${containerAppName}-logs'
var envName = '${containerAppName}-env'

// ---------------------------------------------------------------------------------------
// Secret names + values
// ---------------------------------------------------------------------------------------

var secretSpecs = concat(
  allowApiKey ? [
    {
      name: 'sidecar-api-key'
      value: sidecarApiKey
    }
  ] : [],
  [
    {
      name: 'connected-app-secret-value'
      value: connectedAppSecretValue
    }
  ]
)

// ---------------------------------------------------------------------------------------
// Observability
// ---------------------------------------------------------------------------------------

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource managedEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

// ---------------------------------------------------------------------------------------
// Optional Key Vault + user-assigned managed identity (useKeyVault = true)
// ---------------------------------------------------------------------------------------

var kvName = take('${replace(containerAppName, '-', '')}kv${uniqueString(resourceGroup().id, containerAppName)}', 24)
// "Key Vault Secrets User" built-in role.
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (useKeyVault) {
  name: '${containerAppName}-mi'
  location: location
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = if (useKeyVault) {
  name: kvName
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

resource kvSecrets 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = [for s in secretSpecs: if (useKeyVault) {
  parent: kv
  name: s.name
  properties: {
    value: s.value
  }
}]

resource kvRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (useKeyVault) {
  name: guid(kv.id, uami.id, kvSecretsUserRoleId)
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
    principalId: uami!.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------------------
// Environment variables
// ---------------------------------------------------------------------------------------

// Official Tableau MCP server. Internal only. AUTH=direct-trust + Connected App is required
// at startup; in passthrough mode the per-request identity arrives via X-Tableau-Auth.
var mcpEnv = concat(
  [
    { name: 'TRANSPORT', value: 'http' }
    { name: 'PORT', value: string(mcpPort) }
    { name: 'DANGEROUSLY_DISABLE_OAUTH', value: 'true' }
    // Version-sensitive (tableau-mcp 2.7.x): a startup site-settings probe needs the scope
    // tableau:mcp_site_settings:read, which a direct-trust Connected App typically does not
    // grant -> the initialize handshake 500s. Disabling it only skips that one read; the
    // curated tool set still registers. Re-evaluate if a future image changes this default.
    { name: 'ENABLE_MCP_SITE_SETTINGS', value: 'false' }
    { name: 'ENABLE_PASSTHROUGH_AUTH', value: isPassthrough ? 'true' : 'false' }
    { name: 'SERVER', value: tableauServer }
    { name: 'SITE_NAME', value: tableauSite }
    { name: 'AUTH', value: 'direct-trust' }
    { name: 'JWT_SUB_CLAIM', value: serviceAccountUsername }
    { name: 'CONNECTED_APP_CLIENT_ID', value: connectedAppClientId }
    { name: 'CONNECTED_APP_SECRET_ID', value: connectedAppSecretId }
    { name: 'CONNECTED_APP_SECRET_VALUE', secretRef: 'connected-app-secret-value' }
    { name: 'DEFAULT_LOG_LEVEL', value: 'info' }
  ],
  empty(includeTools) ? [] : [
    { name: 'INCLUDE_TOOLS', value: includeTools }
  ],
  empty(maxResultLimits) ? [] : [
    { name: 'MAX_RESULT_LIMITS', value: maxResultLimits }
  ]
)

// Sidecar. Public ingress. Owns caller auth + (passthrough) Entra->Tableau identity mapping.
var sidecarEnv = concat(
  [
    { name: 'PORT', value: string(sidecarPort) }
    // Version-coupled to the pinned tableauMcpImage tag: tableau-mcp 2.x serves Streamable HTTP
    // at /tableau-mcp; older tags used /mcp. A wrong path returns an Express 404 ("Cannot POST").
    // Re-verify this path if you bump the image tag.
    { name: 'UPSTREAM_MCP_URL', value: 'http://localhost:${mcpPort}/tableau-mcp' }
    { name: 'ALLOW_API_KEY', value: allowApiKey ? 'true' : 'false' }
    { name: 'TRUST_EASY_AUTH', value: (enableEasyAuth || isPassthrough) ? 'true' : 'false' }
    { name: 'IDENTITY_MODE', value: identityMode }
    { name: 'ON_UNRESOLVED_IDENTITY', value: 'deny' }
    { name: 'TABLEAU_SERVER', value: tableauServer }
    { name: 'TABLEAU_SITE', value: tableauSite }
    { name: 'ENTRA_TENANT_ID', value: entraTenantId }
  ],
  allowApiKey ? [
    { name: 'SIDECAR_API_KEY', secretRef: 'sidecar-api-key' }
  ] : [],
  // The sidecar only needs the Connected App (to mint per-user JWTs) in passthrough mode.
  isPassthrough ? [
    { name: 'TABLEAU_CONNECTED_APP_CLIENT_ID', value: connectedAppClientId }
    { name: 'TABLEAU_CONNECTED_APP_SECRET_ID', value: connectedAppSecretId }
    { name: 'TABLEAU_CONNECTED_APP_SECRET_VALUE', secretRef: 'connected-app-secret-value' }
    { name: 'UPN_MAPPING_MODE', value: upnMappingMode }
    { name: 'UPN_DOMAIN_FROM', value: upnDomainFrom }
    { name: 'UPN_DOMAIN_TO', value: upnDomainTo }
  ] : []
)

// ---------------------------------------------------------------------------------------
// Container App
// ---------------------------------------------------------------------------------------

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  identity: useKeyVault ? {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  } : {
    type: 'None'
  }
  properties: {
    managedEnvironmentId: managedEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: sidecarPort
        transport: 'auto'
        allowInsecure: false
      }
      secrets: [for (s, i) in secretSpecs: useKeyVault ? {
        name: s.name
        keyVaultUrl: kvSecrets[i]!.properties.secretUri
        identity: uami!.id
      } : {
        name: s.name
        #disable-next-line use-secure-value-for-secure-inputs
        value: s.value
      }]
    }
    template: {
      containers: [
        {
          // OFFICIAL Tableau MCP server — internal only.
          name: 'tableau-mcp'
          image: tableauMcpImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: mcpEnv
          probes: [
            {
              type: 'Liveness'
              tcpSocket: {
                port: mcpPort
              }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
            {
              // Gates replica readiness on the MCP server actually listening, so ingress
              // never routes a cold-start request to the sidecar before upstream is up.
              type: 'Readiness'
              tcpSocket: {
                port: mcpPort
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              failureThreshold: 30
            }
          ]
        }
        {
          // Our auth sidecar — the public ingress target.
          name: 'sidecar'
          image: sidecarImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: sidecarEnv
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: sidecarPort
              }
              initialDelaySeconds: 5
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: sidecarPort
              }
              initialDelaySeconds: 3
              periodSeconds: 10
            }
            {
              type: 'Startup'
              httpGet: {
                path: '/healthz'
                port: sidecarPort
              }
              initialDelaySeconds: 3
              periodSeconds: 5
              failureThreshold: 30
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-scale'
            http: {
              metadata: {
                concurrentRequests: '20'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: useKeyVault ? [
    kvRole
  ] : []
}

// ---------------------------------------------------------------------------------------
// Optional Easy Auth (Microsoft Entra) front door
// ---------------------------------------------------------------------------------------

resource auth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = if (enableEasyAuth) {
  parent: app
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      // When api-key is also allowed, let anonymous through so the sidecar can enforce the
      // key; Easy Auth still sets X-MS-CLIENT-PRINCIPAL when a valid Entra token is present.
      // When api-key is off, reject unauthenticated callers at the platform edge.
      unauthenticatedClientAction: allowApiKey ? 'AllowAnonymous' : 'Return401'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          openIdIssuer: easyAuthIssuer
          clientId: entraClientId
        }
        validation: {
          allowedAudiences: [
            entraClientId
            'api://${entraClientId}'
          ]
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------------------

@description('The public base URL of the deployed landing zone (the sidecar).')
output mcpBaseUrl string = 'https://${app.properties.configuration.ingress.fqdn}'

@description('The MCP endpoint to register in Copilot Studio (Streamable HTTP).')
output mcpEndpoint string = 'https://${app.properties.configuration.ingress.fqdn}/mcp'

@description('Health check URL.')
output healthUrl string = 'https://${app.properties.configuration.ingress.fqdn}/healthz'

@description('Resolved identity mode.')
output identityModeOut string = identityMode

@description('Whether Easy Auth (Entra front door) is enabled.')
output easyAuthEnabled bool = enableEasyAuth
