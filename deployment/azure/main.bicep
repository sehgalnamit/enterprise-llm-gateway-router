// =============================================================================
// Azure deployment template for the "enterprise-llm-gateway-router" project
// =============================================================================
//
// ⚠️  ILLUSTRATIVE / UNAPPLIED TEMPLATE — READ BEFORE USE  ⚠️
// This Bicep template is provided as a *starting point* for deploying the
// gateway to Azure. It has NOT been deployed or validated against a live
// Azure subscription as part of this exercise (no `az deployment group
// create` or `bicep build` was executed here — only manual review).
//
// Before using this in any real environment you MUST:
//   1. Run `az bicep build --file main.bicep` (or `bicep build`) to confirm
//      it compiles against the Bicep CLI version you have installed.
//   2. Review every default value (SKUs, capacities, names) for cost and
//      capacity-planning implications.
//   3. Review security defaults — this template favors simplicity
//      (public network access, RBAC-only Key Vault, no private endpoints/
//      VNet integration) over the hardened posture you'd want in
//      production. Add Private Endpoints, VNet integration for the
//      Container Apps environment, IP allow-lists on APIM/Key Vault/Redis,
//      and diagnostic settings as appropriate for your environment.
//   4. Parameterize or externalize all secrets — do NOT commit real
//      secret values into `main.parameters.json`. Use `az keyvault`,
//      a pipeline secret store, or `--parameters @secure.params.json`
//      (excluded from source control) instead.
//   5. Test in a disposable/sandbox resource group first.
//
// =============================================================================

targetScope = 'resourceGroup'

// -----------------------------------------------------------------------------
// Parameters
// -----------------------------------------------------------------------------

@description('Short prefix used to build resource names (lowercase alphanumeric, <= 10 chars recommended).')
@minLength(2)
@maxLength(10)
param namePrefix string = 'llmgw'

@description('Azure region for all resources. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Full container image reference for the gateway, e.g. "myregistry.azurecr.io/llm-gateway:latest". Build & push this image first (see docs/AZURE_DEPLOYMENT.md).')
param containerImage string = 'ghcr.io/example/enterprise-llm-gateway-router:latest'

@description('Optional: login server of an existing Azure Container Registry (e.g. "myregistry.azurecr.io") the Container App should pull from using its managed identity. Leave empty to pull from a public/anonymous registry.')
param containerRegistryServer string = ''

@description('Optional: name (not FQDN) of an existing Azure Container Registry in this resource group to grant the Container App managed identity "AcrPull" on. Leave empty to skip the role assignment (e.g. if the registry lives in another RG/subscription and you wire up access separately).')
param containerRegistryName string = ''

@description('TCP port the gateway container listens on (matches the Dockerfile EXPOSE/uvicorn --port).')
param containerTargetPort int = 8000

@description('HTTP path used for the Container App liveness/readiness probes (matches the FastAPI /health route).')
param healthProbePath string = '/health'

@description('Minimum number of Container App replicas.')
@minValue(0)
param minReplicas int = 1

@description('Maximum number of Container App replicas.')
@minValue(1)
param maxReplicas int = 3

@description('CPU cores allocated per Container App replica (fractional values allowed, e.g. 0.5, 1.0).')
param containerCpu string = '0.5'

@description('Memory allocated per Container App replica, must pair with a valid CPU/memory combination (e.g. 1Gi with 0.5 CPU).')
param containerMemory string = '1Gi'

@description('SKU name for Azure Cache for Redis. "Basic" is cheapest/no-SLA; use "Standard" or "Premium" for production.')
@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param redisSkuName string = 'Basic'

@description('SKU family for Azure Cache for Redis. "C" for Basic/Standard, "P" for Premium.')
@allowed([
  'C'
  'P'
])
param redisSkuFamily string = 'C'

@description('SKU capacity/size for Azure Cache for Redis. For family "C": 0=250MB,1=1GB,2=2.5GB,3=6GB,4=13GB,5=26GB,6=53GB. For family "P": 1-5.')
param redisCapacity int = 0

@description('API Management SKU name fronting the gateway. "Consumption" is serverless/pay-per-call (no VNet integration); "Developer" is a cheap always-on tier for non-prod testing.')
@allowed([
  'Consumption'
  'Developer'
  'Basic'
  'Standard'
  'Premium'
])
param apimSkuName string = 'Consumption'

@description('API Management SKU capacity (instance count). Must be 0 for Consumption.')
param apimSkuCapacity int = 0

@description('Publisher email required by API Management (used for notifications, not a real mailbox requirement for this template).')
param apimPublisherEmail string = 'admin@example.com'

@description('Publisher/organization name required by API Management.')
param apimPublisherName string = 'Example Org'

@secure()
@description('Optional OpenAI API key to seed into Key Vault as a secret. Leave blank to configure later via `az keyvault secret set` instead of committing it to a parameters file.')
param openAiApiKey string = ''

@description('Whether to provision an Azure OpenAI (Cognitive Services) resource as an additional first-class provider. Off by default — Azure OpenAI capacity/regions are restricted and this avoids surprising quota consumption.')
param deployAzureOpenAI bool = false

@description('SKU name for the Azure OpenAI Cognitive Services account (only used when deployAzureOpenAI is true).')
param azureOpenAiSkuName string = 'S0'

@description('Name of the model deployment to create under the Azure OpenAI account (only used when deployAzureOpenAI is true).')
param azureOpenAiDeploymentName string = 'gpt-4o-mini'

@description('Underlying model name to deploy (only used when deployAzureOpenAI is true). Check regional model availability before deploying.')
param azureOpenAiModelName string = 'gpt-4o-mini'

@description('Model version to deploy (only used when deployAzureOpenAI is true). Check current supported versions for your region.')
param azureOpenAiModelVersion string = '2024-07-18'

@description('Tokens-per-minute capacity (in thousands) for the Azure OpenAI deployment (only used when deployAzureOpenAI is true).')
param azureOpenAiCapacity int = 10

@description('Tags applied to all resources.')
param tags object = {
  project: 'enterprise-llm-gateway-router'
  environment: 'illustrative'
}

// -----------------------------------------------------------------------------
// Variables — deterministic, (mostly) globally-unique resource names
// -----------------------------------------------------------------------------

var uniqueSuffix = uniqueString(resourceGroup().id)
var logAnalyticsName = '${namePrefix}-law-${uniqueSuffix}'
var containerAppEnvName = '${namePrefix}-cae-${uniqueSuffix}'
var containerAppName = '${namePrefix}-app'
var managedIdentityName = '${namePrefix}-identity'
var keyVaultName = take('${namePrefix}kv${uniqueSuffix}', 24)
var redisName = '${namePrefix}-redis-${uniqueSuffix}'
var apimName = '${namePrefix}-apim-${uniqueSuffix}'
var azureOpenAiName = take('${namePrefix}-aoai-${uniqueSuffix}', 64)

var redisSecretName = 'redis-connection-string'
var openAiSecretName = 'openai-api-key'
var azureOpenAiKeySecretName = 'azure-openai-api-key'

// Built-in RBAC role definition IDs (stable across all Azure tenants).
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

// -----------------------------------------------------------------------------
// User-assigned managed identity for the Container App
// -----------------------------------------------------------------------------
// Created up front (rather than relying on the Container App's
// system-assigned identity) so the Key Vault RBAC role assignment below does
// not have a circular dependency on the Container App resource itself.

resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: managedIdentityName
  location: location
  tags: tags
}

// -----------------------------------------------------------------------------
// Key Vault — stores Redis connection string, OpenAI key(s)
// -----------------------------------------------------------------------------

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true // RBAC instead of legacy access policies
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled' // review: restrict with firewall rules / Private Endpoint for production
  }
}

// Grant the Container App's managed identity read access to secrets via RBAC.
resource kvSecretsUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, managedIdentity.id, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
  }
}

// -----------------------------------------------------------------------------
// Azure Cache for Redis — backs the gateway's semantic cache
// -----------------------------------------------------------------------------

resource redisCache 'Microsoft.Cache/redis@2023-08-01' = {
  name: redisName
  location: location
  tags: tags
  properties: {
    sku: {
      name: redisSkuName
      family: redisSkuFamily
      capacity: redisCapacity
    }
    enableNonSslPort: false // TLS only; app must connect via rediss:// on port 6380
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled' // review: switch to 'Disabled' + Private Endpoint for production
  }
}

// Build a rediss:// (TLS) connection string and store it as a Key Vault secret
// so the Container App never receives the raw Redis access key as a plain
// environment variable.
resource redisConnectionStringSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: redisSecretName
  properties: {
    value: 'rediss://:${redisCache.listKeys().primaryKey}@${redisCache.properties.hostName}:6380/0'
  }
  dependsOn: [
    kvSecretsUserRoleAssignment
  ]
}

// Seeded from the openAiApiKey parameter; pass '' and set it later via
// `az keyvault secret set` if you don't want it captured in deployment history.
resource openAiApiKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: openAiSecretName
  properties: {
    value: empty(openAiApiKey) ? 'REPLACE_ME' : openAiApiKey
  }
  dependsOn: [
    kvSecretsUserRoleAssignment
  ]
}

// -----------------------------------------------------------------------------
// Optional: Azure OpenAI as a first-class provider
// -----------------------------------------------------------------------------
// Set `deployAzureOpenAI = true` to provision a Cognitive Services "OpenAI"
// account plus one model deployment. The resulting endpoint + key are stored
// in the same Key Vault so the gateway can be wired up exactly like the
// OpenAI/Vertex/Ollama providers already in `providers/`. A new
// `providers/azure_openai_provider.py` adapter would read:
//   - AZURE_OPENAI_ENDPOINT   (-> azureOpenAiEndpoint output, below)
//   - AZURE_OPENAI_API_KEY    (-> Key Vault secret "azure-openai-api-key")
//   - AZURE_OPENAI_DEPLOYMENT (-> azureOpenAiDeploymentName param)
// and call `POST {endpoint}openai/deployments/{deployment}/chat/completions
// ?api-version=2024-06-01` with header `api-key: <key>` instead of
// `Authorization: Bearer <key>` (see docs/AZURE_DEPLOYMENT.md for details).

resource azureOpenAi 'Microsoft.CognitiveServices/accounts@2024-10-01' = if (deployAzureOpenAI) {
  name: azureOpenAiName
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: {
    name: azureOpenAiSkuName
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: azureOpenAiName // required so the account gets a *.openai.azure.com endpoint
    publicNetworkAccess: 'Enabled' // review: restrict for production
  }
}

resource azureOpenAiDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = if (deployAzureOpenAI) {
  parent: azureOpenAi
  name: azureOpenAiDeploymentName
  sku: {
    name: 'Standard'
    capacity: azureOpenAiCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: azureOpenAiModelName
      version: azureOpenAiModelVersion
    }
  }
}

resource azureOpenAiKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (deployAzureOpenAI) {
  parent: keyVault
  name: azureOpenAiKeySecretName
  properties: {
    value: deployAzureOpenAI ? azureOpenAi.listKeys().key1 : 'not-deployed'
  }
  dependsOn: [
    kvSecretsUserRoleAssignment
  ]
}

// -----------------------------------------------------------------------------
// Log Analytics workspace + Container Apps managed environment
// -----------------------------------------------------------------------------

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource containerAppEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerAppEnvName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// -----------------------------------------------------------------------------
// Container App running the gateway image
// -----------------------------------------------------------------------------

var baseSecrets = [
  {
    name: redisSecretName
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/${redisSecretName}'
    identity: managedIdentity.id
  }
  {
    name: openAiSecretName
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/${openAiSecretName}'
    identity: managedIdentity.id
  }
]

var azureOpenAiSecrets = deployAzureOpenAI ? [
  {
    name: azureOpenAiKeySecretName
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/${azureOpenAiKeySecretName}'
    identity: managedIdentity.id
  }
] : []

var baseEnvVars = [
  {
    name: 'GATEWAY_REDIS_URL'
    secretRef: redisSecretName
  }
  {
    name: 'OPENAI_API_KEY'
    secretRef: openAiSecretName
  }
  {
    // Local/offline model fallback used by the mock "local-qwen" persona;
    // point this at a real Ollama endpoint if you run one (e.g. on a VM or
    // another Container App) rather than inside this environment.
    name: 'OLLAMA_HOST'
    value: 'http://localhost:11434'
  }
]

var azureOpenAiEnvVars = deployAzureOpenAI ? [
  {
    name: 'AZURE_OPENAI_ENDPOINT'
    value: azureOpenAi.properties.endpoint
  }
  {
    name: 'AZURE_OPENAI_DEPLOYMENT'
    value: azureOpenAiDeploymentName
  }
  {
    name: 'AZURE_OPENAI_API_KEY'
    secretRef: azureOpenAiKeySecretName
  }
] : []

var registries = empty(containerRegistryServer) ? [] : [
  {
    server: containerRegistryServer
    identity: managedIdentity.id
  }
]

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: containerTargetPort
        transport: 'auto'
        allowInsecure: false
      }
      registries: registries
      secrets: concat(baseSecrets, azureOpenAiSecrets)
    }
    template: {
      containers: [
        {
          name: 'gateway'
          image: containerImage
          resources: {
            cpu: json(containerCpu)
            memory: containerMemory
          }
          env: concat(baseEnvVars, azureOpenAiEnvVars)
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: healthProbePath
                port: containerTargetPort
              }
              initialDelaySeconds: 5
              periodSeconds: 15
            }
            {
              type: 'Readiness'
              httpGet: {
                path: healthProbePath
                port: containerTargetPort
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
  dependsOn: [
    kvSecretsUserRoleAssignment
  ]
}

// Optional: grant the Container App identity pull access on an existing ACR
// in this resource group. Skipped automatically when containerRegistryName
// is left blank (e.g. using a public registry, or wiring up ACR access
// separately / in another resource group or subscription).
resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(containerRegistryName)) {
  name: guid(resourceGroup().id, managedIdentity.id, acrPullRoleId, containerRegistryName)
  scope: resourceId('Microsoft.ContainerRegistry/registries', containerRegistryName)
  properties: {
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

// -----------------------------------------------------------------------------
// API Management — front door / API gateway in front of the Container App
// -----------------------------------------------------------------------------

resource apim 'Microsoft.ApiManagement/service@2022-08-01' = {
  name: apimName
  location: location
  tags: tags
  sku: {
    name: apimSkuName
    capacity: apimSkuCapacity
  }
  properties: {
    publisherEmail: apimPublisherEmail
    publisherName: apimPublisherName
  }
}

// Sample API definition: forwards requests under /gateway/* to the Container
// App's ingress FQDN. Extend with additional operations/policies (auth,
// rate-limiting, request/response transformation) as needed — this is
// intentionally minimal.
resource apimApi 'Microsoft.ApiManagement/service/apis@2022-08-01' = {
  parent: apim
  name: 'llm-gateway'
  properties: {
    displayName: 'LLM Gateway'
    path: 'gateway'
    protocols: [
      'https'
    ]
    serviceUrl: 'https://${containerApp.properties.configuration.ingress.fqdn}'
    subscriptionRequired: false // review: enable subscription keys / OAuth validation for production
  }
}

resource apimHealthOperation 'Microsoft.ApiManagement/service/apis/operations@2022-08-01' = {
  parent: apimApi
  name: 'get-health'
  properties: {
    displayName: 'Health check'
    method: 'GET'
    urlTemplate: '/health'
    responses: []
  }
}

resource apimChatCompletionsOperation 'Microsoft.ApiManagement/service/apis/operations@2022-08-01' = {
  parent: apimApi
  name: 'post-chat-completions'
  properties: {
    displayName: 'Chat completions'
    method: 'POST'
    urlTemplate: '/v1/chat/completions'
    responses: []
  }
}

// Strips the APIM "/gateway" path prefix so the backend Container App sees
// the original "/health" and "/v1/chat/completions" paths it expects.
resource apimApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2022-08-01' = {
  parent: apimApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: '''
<policies>
  <inbound>
    <base />
    <rewrite-uri template="{context.Request.OriginalUrl.Path.Substring(context.Api.Path.Length)}" copy-unmatched-params="true" />
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
'''
  }
}

// -----------------------------------------------------------------------------
// Outputs
// -----------------------------------------------------------------------------

@description('Container App ingress FQDN (direct, bypassing APIM).')
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn

@description('Direct Container App health check URL (bypasses APIM — useful for debugging).')
output containerAppHealthUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}${healthProbePath}'

@description('Public APIM gateway base URL that fronts the Container App.')
output apimGatewayUrl string = apim.properties.gatewayUrl

@description('Full APIM URL for the sample "/gateway" API (e.g. append "/health").')
output apimApiBaseUrl string = '${apim.properties.gatewayUrl}/gateway'

@description('Azure Cache for Redis hostname (TLS port 6380; see Key Vault secret for full connection string).')
output redisHostName string = redisCache.properties.hostName

@description('Key Vault URI for managing/rotating secrets post-deployment.')
output keyVaultUri string = keyVault.properties.vaultUri

@description('Azure OpenAI endpoint, if deployed; empty string otherwise.')
output azureOpenAiEndpoint string = deployAzureOpenAI ? azureOpenAi.properties.endpoint : ''

@description('Name of the user-assigned managed identity used by the Container App (for diagnostics/RBAC review).')
output containerAppIdentityName string = managedIdentity.name
