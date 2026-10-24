# Azure Deployment (Bicep) — `deployment/azure/`

> ⚠️ **Illustrative / unapplied templates.** Everything in this folder is a
> deployable-but-**unapplied** reference implementation written and reviewed
> by hand. It has **not** been run through `bicep build` or
> `az deployment group create` against a real Azure subscription as part of
> this exercise. Treat it as a well-commented starting point, not a
> production-ready artifact — review naming, SKUs, costs, and security
> defaults before using it for anything real. See
> [`docs/AZURE_DEPLOYMENT.md`](../../docs/AZURE_DEPLOYMENT.md) for the full
> step-by-step guide.

## Files

| File | Purpose |
|---|---|
| `main.bicep` | The deployment template (Container Apps, Redis, APIM, Key Vault, optional Azure OpenAI). |
| `main.parameters.json` | Example parameters file. Secrets are left blank — set them via Key Vault after deployment, not in source control. |

## What it provisions

- **Azure Container Apps environment + Container App** running the gateway
  image on port 8000, with liveness/readiness probes on `/health`, a
  user-assigned managed identity, and `GATEWAY_REDIS_URL` / `OPENAI_API_KEY`
  injected from Key Vault secrets (never as plain env vars).
- **Azure Cache for Redis** (Basic tier by default, parameterized) backing
  the gateway's semantic cache.
- **Azure API Management** (Consumption tier by default, parameterized) as
  the public-facing API gateway/front door, with a sample `/gateway` API
  routed to the Container App's ingress FQDN (`GET /health`,
  `POST /v1/chat/completions`).
- **Azure Key Vault** (RBAC-authorized) holding the Redis connection string
  and the OpenAI API key, with the Container App's managed identity granted
  `Key Vault Secrets User`.
- **Optional Azure OpenAI** (Cognitive Services `OpenAI` kind + one model
  deployment), gated behind the `deployAzureOpenAI` parameter (`false` by
  default). When enabled, its endpoint and key are also stored in the same
  Key Vault and surfaced as `AZURE_OPENAI_*` environment variables on the
  Container App — see the inline comments in `main.bicep` for how a
  corresponding `providers/azure_openai_provider.py` adapter would consume
  them.

## Illustrative deployment command

**Not executed.** Shown only as an example of how you *would* deploy this
once you've reviewed and parameterized it:

```powershell
# 1. Create (or choose) a resource group
az group create --name rg-llm-gateway-demo --location eastus

# 2. Validate the template compiles (recommended before every deployment)
az bicep build --file deployment\azure\main.bicep

# 3. Preview changes with what-if (no resources are created)
az deployment group what-if `
  --resource-group rg-llm-gateway-demo `
  --template-file deployment\azure\main.bicep `
  --parameters deployment\azure\main.parameters.json

# 4. Deploy for real (only after reviewing the what-if output)
az deployment group create `
  --resource-group rg-llm-gateway-demo `
  --template-file deployment\azure\main.bicep `
  --parameters deployment\azure\main.parameters.json
```

See `docs/AZURE_DEPLOYMENT.md` for the full walkthrough, including building
and pushing the container image, seeding Key Vault secrets, and verifying the
deployment.
