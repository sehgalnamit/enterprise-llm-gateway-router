# Deploying the LLM Gateway to Azure

This guide walks through deploying `enterprise-llm-gateway-router` to Azure
using the Bicep template in [`deployment/azure/main.bicep`](../deployment/azure/main.bicep).
It provisions:

- **Azure Container Apps** — runs the gateway's existing Docker image
- **Azure Cache for Redis** — backs the semantic cache
- **Azure API Management (APIM)** — public API gateway/front door
- **Azure Key Vault** — holds the Redis connection string and provider API keys
- **Azure OpenAI** *(optional, off by default)* — an additional first-class
  LLM provider

> ## ⚠️ Honesty note
>
> The Bicep template, parameters file, and this guide are **reference
> material produced and reviewed by hand**. They were **not** deployed,
> `bicep build`-validated, or otherwise tested against a real Azure
> subscription as part of this exercise — the Azure CLI/Bicep CLI were not
> available in the environment used to write them, and no `az deployment
> group create` (or `what-if`) was run.
>
> Before using any of this in a real environment:
> - Run `bicep build` / `az deployment group validate` yourself and fix
>   anything that doesn't match your installed CLI/API versions.
> - Review **cost** implications of every SKU and capacity default (Redis
>   tier, APIM tier, Azure OpenAI capacity, Container Apps scale limits).
> - Review **security** defaults: this template uses public network access
>   on Key Vault, Redis, and APIM for simplicity. Production deployments
>   should add VNet integration for the Container Apps environment, Private
>   Endpoints for Key Vault/Redis, IP allow-lists or VNet-restricted APIM,
>   and tighter RBAC scoping.
> - Review **correctness** against the current state of your subscription,
>   region availability (especially for Azure OpenAI model/region quotas),
>   and your organization's naming/tagging conventions.

---

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| Azure subscription | With permission to create resource groups and the resource types listed above. |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (`az`) | Version 2.60+ recommended. Run `az login` first. |
| [Bicep CLI](https://learn.microsoft.com/azure/azure-resource-manager/bicep/install) | `az bicep install` (ships as an `az` extension) or standalone `bicep` binary. |
| [Docker](https://docs.docker.com/get-docker/) | To build and push the gateway image — the repo already has a working `Dockerfile` at the repo root. |
| An Azure Container Registry (ACR) or another registry reachable from Azure | The template can grant the Container App's managed identity `AcrPull` on an ACR in the same resource group, or you can point at any registry the Container App can reach (public or otherwise). |

Verify your tools:

```powershell
az version
az bicep version
docker --version
```

---

## 2. Build and push the container image

From the repo root (where the `Dockerfile` already lives):

```powershell
# Create a resource group and an Azure Container Registry (if you don't have one)
az group create --name rg-llm-gateway-demo --location eastus
az acr create --resource-group rg-llm-gateway-demo --name myregistry --sku Basic

# Build and push using ACR Tasks (no local Docker build required), or build locally:
az acr build --registry myregistry --image enterprise-llm-gateway-router:latest .

# --- OR, building locally and pushing ---
docker build -t myregistry.azurecr.io/enterprise-llm-gateway-router:latest .
az acr login --name myregistry
docker push myregistry.azurecr.io/enterprise-llm-gateway-router:latest
```

Take note of the full image reference
(`myregistry.azurecr.io/enterprise-llm-gateway-router:latest`) — you'll pass
it as the `containerImage` parameter below.

---

## 3. Review and parameterize the template

Open [`deployment/azure/main.parameters.json`](../deployment/azure/main.parameters.json)
and update at minimum:

- `containerImage` → the image reference from step 2
- `containerRegistryServer` / `containerRegistryName` → your ACR, or leave
  blank (`""`) if pulling from a public/anonymous registry
- `location`, `namePrefix` → your preferred region/naming
- `redisSkuName`, `apimSkuName` → your SKU/cost tradeoffs
- Leave `openAiApiKey` blank here and set it after deployment (step 5) rather
  than committing it to a parameters file.

**Do not commit real secret values** into `main.parameters.json`. For
anything sensitive, either leave the parameter blank and set the Key Vault
secret afterwards, or pass an override on the command line / from a
pipeline secret store:

```powershell
az deployment group create `
  --resource-group rg-llm-gateway-demo `
  --template-file deployment\azure\main.bicep `
  --parameters deployment\azure\main.parameters.json `
  --parameters openAiApiKey=$env:OPENAI_API_KEY
```

---

## 4. Validate, preview, then deploy

```powershell
# Compile-check the template
az bicep build --file deployment\azure\main.bicep

# Preview what would be created/changed (no resources are modified)
az deployment group what-if `
  --resource-group rg-llm-gateway-demo `
  --template-file deployment\azure\main.bicep `
  --parameters deployment\azure\main.parameters.json

# Deploy — only after reviewing the what-if output
az deployment group create `
  --resource-group rg-llm-gateway-demo `
  --name llm-gateway-deployment `
  --template-file deployment\azure\main.bicep `
  --parameters deployment\azure\main.parameters.json
```

Capture the outputs (also retrievable later with `az deployment group show`):

```powershell
az deployment group show `
  --resource-group rg-llm-gateway-demo `
  --name llm-gateway-deployment `
  --query properties.outputs
```

Key outputs: `containerAppFqdn`, `apimGatewayUrl`, `redisHostName`,
`keyVaultUri`, `azureOpenAiEndpoint` (empty unless `deployAzureOpenAI=true`).

---

## 5. Configure secrets in Key Vault

The template seeds the Redis connection string automatically (built from the
provisioned cache's keys) and seeds `openai-api-key` with either the
parameter value or a placeholder. Set/rotate secrets post-deployment with:

```powershell
$kvName = "<keyVaultUri host, without https:// or trailing slash>"

az keyvault secret set --vault-name $kvName --name openai-api-key --value "$env:OPENAI_API_KEY"

# Rotate the Redis connection string yourself only if you regenerate Redis keys;
# otherwise the value set at deploy time remains valid.
```

After updating a secret, new Container App revisions will pick up the latest
value; existing running revisions may need a restart/new revision to re-read
it (Container Apps resolves Key Vault references at revision creation time).

---

## 6. Verify the deployment

```powershell
# Direct Container App health check (bypasses APIM)
curl.exe "https://<containerAppFqdn>/health"

# Through the APIM front door (the intended external entry point)
curl.exe "https://<apimGatewayUrl host>/gateway/health"
```

Both should return the gateway's `/health` JSON payload. If the APIM route
fails but the direct Container App URL works, check the APIM API policy
(`deployment/azure/main.bicep`'s `apimApiPolicy` resource) and the API's
`serviceUrl`.

---

## 7. Point the gateway at Azure OpenAI as a provider

By default `deployAzureOpenAI` is `false` (Azure OpenAI quota/region
availability varies, and it's easy to accidentally provision capacity you
don't need). To enable it:

```powershell
az deployment group create `
  --resource-group rg-llm-gateway-demo `
  --template-file deployment\azure\main.bicep `
  --parameters deployment\azure\main.parameters.json `
  --parameters deployAzureOpenAI=true azureOpenAiModelName=gpt-4o-mini azureOpenAiModelVersion=2024-07-18
```

This provisions a `Microsoft.CognitiveServices` account (`kind: OpenAI`) plus
one model deployment, stores its key in Key Vault as
`azure-openai-api-key`, and injects three environment variables into the
Container App:

| Env var | Source |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | `https://<account>.openai.azure.com/` |
| `AZURE_OPENAI_DEPLOYMENT` | the deployment name (`azureOpenAiDeploymentName` param) |
| `AZURE_OPENAI_API_KEY` | Key Vault secret `azure-openai-api-key` |

### Wiring it into the gateway code

The existing providers (`providers/openai_provider.py`,
`providers/vertex_provider.py`, `providers/ollama_provider.py`) each read
their config from environment variables and implement `generate_raw()` /
`normalize()`. A new `providers/azure_openai_provider.py` adapter would
follow the same pattern, but the **request shape differs from public
OpenAI** in two important ways:

1. **URL path.** Azure OpenAI uses a deployment-scoped path and an explicit
   `api-version` query parameter instead of `/v1/chat/completions`:

   ```text
   # Public OpenAI (providers/openai_provider.py today)
   POST https://api.openai.com/v1/chat/completions

   # Azure OpenAI
   POST {AZURE_OPENAI_ENDPOINT}openai/deployments/{AZURE_OPENAI_DEPLOYMENT}/chat/completions?api-version=2024-06-01
   ```

2. **Auth header.** Public OpenAI uses a bearer token; Azure OpenAI uses a
   dedicated header carrying the deployment key directly (no `Bearer`
   prefix):

   ```text
   # Public OpenAI
   Authorization: Bearer <OPENAI_API_KEY>

   # Azure OpenAI
   api-key: <AZURE_OPENAI_API_KEY>
   ```

   Also note the request body no longer needs a `model` field (the
   deployment name in the URL determines the model), though Azure OpenAI
   tolerates it being present.

A minimal sketch (illustrative, not wired into the router/provider registry
in this change):

```python
# providers/azure_openai_provider.py (sketch only)
import os
import httpx

endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
api_key = os.environ["AZURE_OPENAI_API_KEY"]
api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-06-01")

url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
headers = {"api-key": api_key}
```

Once such an adapter exists, register it alongside the other providers and
route to it the same way the gateway already routes between OpenAI, Vertex,
and Ollama personas.

---

## 8. Cleaning up

```powershell
az group delete --name rg-llm-gateway-demo --yes --no-wait
```

This removes everything provisioned by the template (Container Apps
environment, Redis, APIM, Key Vault, and — if enabled — the Azure OpenAI
account). Key Vault and Cognitive Services resources are soft-deleted by
default; purge them explicitly if you need the names back immediately:

```powershell
az keyvault purge --name <keyVaultName> --location eastus
az cognitiveservices account purge --name <azureOpenAiName> --resource-group rg-llm-gateway-demo --location eastus
```

---

## Related files

- [`deployment/azure/main.bicep`](../deployment/azure/main.bicep) — the template itself (heavily commented)
- [`deployment/azure/main.parameters.json`](../deployment/azure/main.parameters.json) — example parameters
- [`deployment/azure/README.md`](../deployment/azure/README.md) — short resource summary + illustrative deploy command
