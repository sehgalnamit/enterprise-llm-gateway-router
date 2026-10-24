# Deploying the LLM Gateway to GCP

This guide walks through deploying `enterprise-llm-gateway-router` to Google
Cloud using the Terraform configuration in
[`deployment/gcp/`](../deployment/gcp/). It provisions:

- **Cloud Run v2** — runs the gateway's existing Docker image
- **Memorystore for Redis** — backs the semantic cache, reached via a
  Serverless VPC Access connector
- **Google Cloud API Gateway** — public API gateway/front door
- **Secret Manager** — holds the Redis connection URL and provider API
  keys/tokens, least-privilege IAM to a dedicated Cloud Run runtime service
  account
- *(optional, off by default)* **Vertex AI** workload-identity IAM binding,
  as the recommended production alternative to the repo's demo
  `VERTEX_ACCESS_TOKEN` approach

> ## ⚠️ Honesty note
>
> The Terraform templates (`main.tf`, `variables.tf`, `outputs.tf`,
> `openapi-gateway.yaml`), the example tfvars, and this guide are **reference
> material produced and reviewed by hand**. `terraform fmt` and
> `terraform validate` (against real `hashicorp/google`/`hashicorp/google-beta`
> provider schemas, v6.50.0) were run while authoring this template and both
> passed cleanly — so the HCL is syntactically correct and internally
> consistent with that provider version. However, they were **not**
> `terraform plan`'d or `terraform apply`'d against a real GCP project as
> part of this exercise (no `gcloud`/project credentials were used, and no
> real GCP resources were touched), so this is still an **unapplied**
> template: resource-level correctness (quotas, region availability, IAM
> propagation timing, actual Cloud Run startup behavior, etc.) has not been
> exercised end-to-end.
>
> Before using any of this in a real environment:
> - Run `terraform plan` yourself against a disposable/sandbox project
>   (after `terraform init`/`fmt`/`validate`, which you can also re-run
>   yourself to confirm against your own provider version), and read the
>   plan output carefully
>   before ever running `terraform apply`. Fix anything that doesn't match
>   your installed provider version's resource schema.
> - Review **cost** implications of every default (Memorystore tier/size,
>   Cloud Run CPU/memory/min-instances, VPC connector instance count,
>   API Gateway request volume).
> - Review **security** defaults: `allow_unauthenticated` defaults to `true`
>   for demo simplicity (the Cloud Run service is publicly invokable).
>   Secret Manager access is already least-privilege (per-secret
>   `secretAccessor` bindings, not project-wide), but for production you
>   should additionally consider setting `allow_unauthenticated = false`,
>   restricting Cloud Run ingress further (e.g. `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`
>   behind an external HTTPS load balancer instead of API Gateway, if your
>   org standardizes on that), and VPC Service Controls around the project
>   if it will hold real provider credentials.
> - Review **correctness** against the current state of your project,
>   region/quota availability (especially Memorystore `STANDARD_HA`'s
>   5 GB minimum and Vertex AI model/region availability), and your
>   organization's naming/labeling conventions.

---

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| GCP project | With billing enabled and permission to create the resource types listed above. |
| [gcloud CLI](https://cloud.google.com/sdk/docs/install) | Run `gcloud auth login` and `gcloud auth application-default login` first. |
| [Terraform CLI](https://developer.hashicorp.com/terraform/install) | 1.5+ recommended (matches `required_version` in `main.tf`). |
| [Docker](https://docs.docker.com/get-docker/) | To build and push the gateway image — the repo already has a working `Dockerfile` at the repo root. |
| An Artifact Registry Docker repository | The template expects an image reference like `us-central1-docker.pkg.dev/<project>/<repo>/enterprise-llm-gateway-router:latest`; create the repo in step 2 if you don't have one. |

Verify your tools:

```powershell
gcloud version
terraform version
docker --version
```

---

## 2. Enable required APIs and build & push the container image

Terraform's `google_project_service` resources (gated by `var.enable_apis`,
default `true`) will enable the required APIs for you on `apply`. If you'd
rather enable them explicitly up front (or your project disallows Terraform
managing API enablement), do it manually first:

```powershell
gcloud config set project my-gcp-project-id

gcloud services enable `
  run.googleapis.com `
  redis.googleapis.com `
  vpcaccess.googleapis.com `
  servicenetworking.googleapis.com `
  secretmanager.googleapis.com `
  apigateway.googleapis.com `
  servicecontrol.googleapis.com `
  servicemanagement.googleapis.com `
  compute.googleapis.com `
  iam.googleapis.com `
  aiplatform.googleapis.com
```

Create an Artifact Registry Docker repository (one-time) and build & push
the image, from the repo root where the `Dockerfile` lives:

```powershell
gcloud artifacts repositories create llm-gateway `
  --repository-format=docker `
  --location=us-central1 `
  --description="Enterprise LLM Gateway images"

gcloud auth configure-docker us-central1-docker.pkg.dev

docker build -t us-central1-docker.pkg.dev/my-gcp-project-id/llm-gateway/enterprise-llm-gateway-router:latest .
docker push us-central1-docker.pkg.dev/my-gcp-project-id/llm-gateway/enterprise-llm-gateway-router:latest
```

Take note of the full image reference — you'll set it as `container_image`
in step 3.

---

## 3. Review and parameterize the template

```powershell
Copy-Item deployment\gcp\terraform.tfvars.example deployment\gcp\terraform.tfvars
```

Edit `deployment\gcp\terraform.tfvars` and update at minimum:

- `project_id` → your GCP project ID (required — there is no default)
- `container_image` → the image reference from step 2
- `region`, `name_prefix` → your preferred region/naming (keep `name_prefix`
  short; the VPC connector name has a 25-character API limit)
- `redis_tier`, `redis_memory_size_gb`, `cpu`, `memory`,
  `min_instance_count` / `max_instance_count` → your SKU/cost tradeoffs
- `allow_unauthenticated` → `true` for a quick demo, `false` if you want
  Cloud Run locked to IAM auth (API Gateway's managed service agent is
  automatically granted invoker in that mode — see `main.tf`)
- Leave `openai_api_key` and `vertex_access_token` **blank** here and set
  them after `apply` (step 5) rather than committing them to a tfvars file.

**Do not commit real secret values** into `terraform.tfvars`. Either leave
the variables blank and populate Secret Manager afterwards, or pass an
override on the command line / from a pipeline secret store instead of
checking it into source control:

```powershell
terraform plan -out=tfplan -var="openai_api_key=$env:OPENAI_API_KEY"
```

---

## 4. Initialize, validate, plan, then apply

```powershell
cd deployment\gcp

# Download/verify provider plugins
terraform init

# Format + compile-check the configuration
terraform fmt
terraform validate

# Preview what would be created (no resources are modified)
terraform plan -out=tfplan

# Apply for real (only after reviewing the plan output above)
terraform apply tfplan
```

> The first `apply` will create a Cloud Run revision whose
> `OPENAI_API_KEY` / `VERTEX_ACCESS_TOKEN` secrets may have **zero
> versions** yet (if you left them blank in step 3) — the revision may fail
> to become ready, or the container will start but those providers will be
> disabled (the repo's adapters raise `ProviderUnavailableError` and the
> router fails over to the mock/local personas when a key is missing, so
> `/health` and the mock-provider demo routes still work either way). Move
> on to step 5 to populate the real secret values, which creates a new
> secret version that the **next** Cloud Run revision will pick up
> automatically (Cloud Run v2 references secrets by the `latest` alias).

---

## 5. Populate Secret Manager secret values

```powershell
printf '%s' 'sk-...' | gcloud secrets versions add llmgw-openai-api-key --data-file=-
printf '%s' '<your-vertex-bearer-token>' | gcloud secrets versions add llmgw-vertex-access-token --data-file=-
```

(Replace `llmgw-` with whatever `name_prefix` you used.) The Redis URL
secret (`llmgw-redis-url`) is populated automatically by Terraform — you
don't need to touch it.

After adding a new secret version, force Cloud Run to pick it up by
deploying a new revision (re-running `terraform apply` with no other
changes is enough, since Cloud Run references `version = "latest"` and a
no-op `apply` won't create a new revision on its own — the simplest way to
force one is `gcloud run services update <service> --region <region>` with
no other flags, or re-running the deployment pipeline that built the
image).

---

## 6. Verify the deployment

```powershell
cd deployment\gcp
$hostname = terraform output -raw api_gateway_default_hostname
curl.exe "https://$hostname/health"
# -> {"status":"ok"}

curl.exe -X POST "https://$hostname/v1/chat" `
  -H "Content-Type: application/json" `
  -d '{"session_id":"demo","message":"hello","model":"mock-claude"}'
```

If you set `allow_unauthenticated = false`, calling the **Cloud Run URL
directly** (`terraform output cloud_run_service_url`) will return `403` for
unauthenticated callers — that's expected; only API Gateway's managed
service agent (and anyone you explicitly grant `roles/run.invoker`) can call
it directly. Calls through the API Gateway hostname above are unaffected.

---

## 7. Wiring the gateway to Vertex AI as a provider

The repo's `providers/vertex_provider.py` (`VertexAIProvider`) is a
dependency-light **demo** adapter: it reads `VERTEX_PROJECT_ID`,
`VERTEX_LOCATION`, and a **static** `VERTEX_ACCESS_TOKEN` bearer token
directly from the environment, calling Vertex AI's REST `generateContent`
endpoint via plain `httpx` (no `google-cloud-aiplatform` SDK dependency).
That's what this Terraform template wires up by default via the
`vertex_access_token` Secret Manager secret from step 5.

**This is fine for a demo, but a static long-lived bearer token is not the
recommended production pattern** — it has to be manually minted (e.g. via
`gcloud auth print-access-token`), expires hourly, and is a credential that
can leak. The recommended production approach is:

1. Set `enable_vertex_workload_identity = true` in `terraform.tfvars`. This
   grants the Cloud Run runtime service account `roles/aiplatform.user` on
   the project (see `main.tf` ->
   `google_project_iam_member.cloud_run_sa_vertex_user`).
2. Update `VertexAIProvider` to fetch credentials via **Application Default
   Credentials (ADC)** instead of a static token — Cloud Run automatically
   exposes the attached service account's identity through the metadata
   server, so `google.auth.default()` (from the `google-auth` package)
   "just works" with no key files or manual token refresh:

   ```python
   import google.auth
   import google.auth.transport.requests

   credentials, _ = google.auth.default(
       scopes=["https://www.googleapis.com/auth/cloud-platform"]
   )
   credentials.refresh(google.auth.transport.requests.Request())
   access_token = credentials.token  # short-lived, auto-refreshable
   ```

3. Remove the `VERTEX_ACCESS_TOKEN` env var / secret wiring once the code
   no longer needs it (or leave it as an optional override for local dev
   against a different project than the one the service account belongs
   to).

This keeps Vertex AI access scoped to exactly `roles/aiplatform.user` on
exactly this project, with automatically-rotated credentials, instead of a
hand-minted token living in Secret Manager.

---

## 8. Tearing it down

```powershell
cd deployment\gcp
terraform plan -destroy -out=tfplan.destroy
terraform apply tfplan.destroy
```

Review the destroy plan carefully — `google_cloud_run_v2_service.gateway`
has `deletion_protection = false` in this template for demo convenience, so
`destroy` will remove it without an extra confirmation step. Memorystore and
the VPC resources also have no deletion protection by default.

---

## See also

- [`deployment/gcp/README.md`](../deployment/gcp/README.md) — resource
  summary and illustrative CLI commands.
- [`docs/AZURE_DEPLOYMENT.md`](AZURE_DEPLOYMENT.md) — the equivalent Azure
  (Bicep) deployment guide, for comparison.
