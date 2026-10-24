# GCP Deployment (Terraform) — `deployment/gcp/`

> ⚠️ **Illustrative / unapplied templates.** Everything in this folder is a
> deployable-but-**unapplied** reference implementation written and reviewed
> by hand. `terraform init`, `fmt`, and `validate` were run against real
> `hashicorp/google`/`hashicorp/google-beta` provider schemas (v6.50.0) while
> authoring it and all passed cleanly, but it has **not** been `terraform
> plan`'d or `apply`'d against a real GCP project — no real GCP resources
> were created or touched. Treat it as a well-commented, syntax-checked
> starting point, not a production-ready artifact — review resource names,
> SKUs/sizing, costs, and security defaults before using it for anything
> real. See [`docs/GCP_DEPLOYMENT.md`](../../docs/GCP_DEPLOYMENT.md) for the
> full step-by-step guide.

## Files

| File | Purpose |
|---|---|
| `main.tf` | Provider/network/Redis/Cloud Run/Secret Manager/API Gateway resources. |
| `variables.tf` | Input variables with descriptions and sensible defaults. |
| `outputs.tf` | Key resource endpoints (Cloud Run URL, API Gateway hostname, Redis host:port, etc.). |
| `openapi-gateway.yaml` | OpenAPI 2.0 spec consumed by `google_api_gateway_api_config`, templated with the live Cloud Run URL. |
| `terraform.tfvars.example` | Example variable values. **No real secrets** — see inline comments on populating Secret Manager out-of-band. |

## What it provisions

- **A dedicated VPC + subnet + Serverless VPC Access connector** so the
  serverless Cloud Run service can reach Memorystore's private IP. Includes
  the Private Service Access peering (`google_compute_global_address` +
  `google_service_networking_connection`) Memorystore requires.
- **Memorystore for Redis** (`BASIC` tier by default, parameterized to
  `STANDARD_HA`) backing the gateway's semantic cache, reachable only from
  inside the VPC.
- **Cloud Run v2 service** running the gateway's existing Docker image on
  port 8000, with startup/liveness probes on `/health`, parameterized
  CPU/memory/min/max instances, and `GATEWAY_REDIS_URL` / `OPENAI_API_KEY` /
  `VERTEX_ACCESS_TOKEN` injected from Secret Manager secret versions (never
  as plain env vars). `allow_unauthenticated` toggles between public
  (`allUsers` invoker) and IAM-only (API Gateway's managed service agent
  granted invoker) access.
- **Secret Manager** secrets for the Redis URL (computed by Terraform) and
  the OpenAI/Vertex credentials (left empty by default — populate via
  `gcloud secrets versions add` post-apply), with a dedicated Cloud Run
  runtime service account granted `roles/secretmanager.secretAccessor` on
  **only those three secrets** (least privilege, not project-wide).
- **Google Cloud API Gateway** (`google_api_gateway_api` /
  `_api_config` / `_gateway`) as the public front door, routing to the
  Cloud Run service via the OpenAPI spec's `x-google-backend` extension.
  **Apigee** (Google's full enterprise API management product) is the
  heavier alternative — it requires a separately provisioned Apigee
  organization before any proxy can be deployed. See the detailed comment
  block at the top of `main.tf` for why this template uses Cloud API
  Gateway and how you'd swap to Apigee in a true enterprise setting.
- Inline comments on wiring in **Vertex AI** as a first-class provider,
  including the recommended production pattern (workload-identity-based
  ADC + `roles/aiplatform.user`, gated behind
  `enable_vertex_workload_identity`) vs. the repo's dependency-light demo
  adapter (static `VERTEX_ACCESS_TOKEN`).

## Illustrative deployment commands

**Not executed.** Shown only as an example of how you *would* deploy this
once you've reviewed and parameterized it (see
`docs/GCP_DEPLOYMENT.md` for the full walkthrough):

```powershell
# 0. Authenticate and set your project (adjust for your shell/tooling)
gcloud auth application-default login
gcloud config set project my-gcp-project-id

# 1. Copy and edit the example variables
Copy-Item deployment\gcp\terraform.tfvars.example deployment\gcp\terraform.tfvars
# ... edit terraform.tfvars: project_id, container_image, sizing, etc.

# 2. Initialize providers/backend
cd deployment\gcp
terraform init

# 3. Format + validate (recommended before every plan/apply)
terraform fmt
terraform validate

# 4. Preview changes (no resources are created)
terraform plan -out=tfplan

# 5. Apply for real (only after reviewing the plan output)
terraform apply tfplan

# 6. Populate the secret values Terraform left empty
printf '%s' 'sk-...'  | gcloud secrets versions add llmgw-openai-api-key --data-file=-
printf '%s' '<token>' | gcloud secrets versions add llmgw-vertex-access-token --data-file=-

# 7. Verify
$hostname = terraform output -raw api_gateway_default_hostname
curl.exe "https://$hostname/health"
```

See `docs/GCP_DEPLOYMENT.md` for the full guide, including building and
pushing the container image to Artifact Registry and the production-vs-demo
Vertex AI authentication tradeoffs.
