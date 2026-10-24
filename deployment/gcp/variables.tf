# =============================================================================
# GCP deployment variables for "enterprise-llm-gateway-router"
# =============================================================================
# See main.tf (top of file) and deployment/gcp/README.md for the big-picture
# honesty disclaimer. In short: this is an illustrative, deployable-but-
# UNAPPLIED template. Review every default below before running `terraform
# plan`/`apply` against a real project.
# =============================================================================

variable "project_id" {
  description = <<-EOT
    GCP project ID to deploy into. Required -- there is intentionally no
    default, so you must pass this explicitly (terraform.tfvars, -var, or an
    environment-specific .tfvars file) rather than accidentally deploying
    into whatever project your gcloud/terraform default happens to be.
  EOT
  type        = string
}

variable "region" {
  description = "GCP region for regional resources (Cloud Run, Memorystore, VPC connector, API Gateway)."
  type        = string
  default     = "us-central1"
}

variable "name_prefix" {
  description = <<-EOT
    Short prefix used to build resource names (lowercase alphanumeric and
    hyphens only). Keep this short: the Serverless VPC Access connector name
    has a 25-character limit, and this prefix is combined with suffixes like
    "-connector".
  EOT
  type        = string
  default     = "llmgw"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,8}$", var.name_prefix))
    error_message = "name_prefix must be 2-9 lowercase alphanumeric/hyphen characters, starting with a letter."
  }
}

variable "labels" {
  description = "Common labels applied to taggable resources."
  type        = map(string)
  default = {
    app        = "enterprise-llm-gateway-router"
    managed-by = "terraform"
  }
}

# -----------------------------------------------------------------------------
# Cloud Run
# -----------------------------------------------------------------------------

variable "container_image" {
  description = <<-EOT
    Fully-qualified container image for the gateway, e.g.
    "us-central1-docker.pkg.dev/<project>/<repo>/enterprise-llm-gateway-router:latest".
    Build and push this using the repo's existing Dockerfile (see
    docs/GCP_DEPLOYMENT.md step 2) before applying.
  EOT
  type        = string
  default     = "us-central1-docker.pkg.dev/PROJECT_ID/llm-gateway/enterprise-llm-gateway-router:latest"
}

variable "container_port" {
  description = "TCP port the gateway container listens on (matches the Dockerfile EXPOSE / uvicorn --port 8000)."
  type        = number
  default     = 8000
}

variable "health_check_path" {
  description = "HTTP path used for Cloud Run startup/liveness probes (matches the FastAPI /health route)."
  type        = string
  default     = "/health"
}

variable "cpu" {
  description = "vCPUs allocated per Cloud Run v2 instance (string, per Cloud Run's resource.limits.cpu format, e.g. \"1\", \"2\")."
  type        = string
  default     = "1"
}

variable "memory" {
  description = "Memory allocated per Cloud Run v2 instance (e.g. \"512Mi\", \"1Gi\")."
  type        = string
  default     = "512Mi"
}

variable "min_instance_count" {
  description = "Minimum number of Cloud Run instances. 0 allows scale-to-zero (adds cold-start latency); 1+ keeps a warm instance for lower tail latency."
  type        = number
  default     = 0
}

variable "max_instance_count" {
  description = "Maximum number of Cloud Run instances (caps cost/blast radius under load)."
  type        = number
  default     = 3
}

variable "allow_unauthenticated" {
  description = <<-EOT
    If true, grants `roles/run.invoker` to `allUsers` so the Cloud Run
    service (and therefore anything proxying to it, including the API
    Gateway) is reachable without IAM-based auth -- simplest for a demo, but
    means anyone with the URL can call the gateway directly, bypassing the
    API Gateway's managed-service quota/auth layer.

    If false, the Cloud Run service requires IAM auth. The API Gateway's
    Google-managed service agent is instead granted `roles/run.invoker`, so
    traffic *through the gateway hostname* still works, but the raw Cloud Run
    URL is locked down to principals you explicitly grant invoker to. This is
    the recommended setting for anything beyond a throwaway demo.
  EOT
  type        = bool
  default     = true
}

# -----------------------------------------------------------------------------
# Memorystore (Redis) + networking
# -----------------------------------------------------------------------------

variable "redis_tier" {
  description = "Memorystore for Redis service tier: \"BASIC\" (no HA/SLA, cheapest) or \"STANDARD_HA\" (replica + automatic failover, for production)."
  type        = string
  default     = "BASIC"

  validation {
    condition     = contains(["BASIC", "STANDARD_HA"], var.redis_tier)
    error_message = "redis_tier must be \"BASIC\" or \"STANDARD_HA\"."
  }
}

variable "redis_memory_size_gb" {
  description = "Memorystore Redis instance size in GB. Minimum is 1 for BASIC, 5 for STANDARD_HA (GCP requirement -- raise this if redis_tier = STANDARD_HA)."
  type        = number
  default     = 1
}

variable "redis_version" {
  description = "Memorystore Redis version."
  type        = string
  default     = "REDIS_7_0"
}

variable "vpc_connector_cidr" {
  description = <<-EOT
    /28 CIDR range dedicated to the Serverless VPC Access connector's subnet.
    Must not overlap with any other subnet in the VPC (here we create a
    purpose-built VPC, so the only constraint is avoiding the Private
    Service Access range below).
  EOT
  type        = string
  default     = "10.8.0.0/28"
}

variable "private_service_access_range_prefix_length" {
  description = "Prefix length of the reserved internal IP range used for the Private Service Access peering that Memorystore sits behind (see main.tf comments)."
  type        = number
  default     = 16
}

variable "vpc_connector_machine_type" {
  description = "Machine type for the Serverless VPC Access connector instances."
  type        = string
  default     = "e2-micro"
}

variable "vpc_connector_min_instances" {
  description = "Minimum number of VPC connector instances (GCP requires >= 2)."
  type        = number
  default     = 2
}

variable "vpc_connector_max_instances" {
  description = "Maximum number of VPC connector instances."
  type        = number
  default     = 3
}

# -----------------------------------------------------------------------------
# Secrets / provider credentials
# -----------------------------------------------------------------------------
# NOTE ON SECRET HYGIENE: these variables are marked `sensitive` and default
# to empty strings deliberately. If left empty, Terraform creates the Secret
# Manager *secret container* but no version, and you populate the real value
# out-of-band after apply (see docs/GCP_DEPLOYMENT.md step 6), e.g.:
#   printf '%s' 'sk-...' | gcloud secrets versions add llmgw-openai-api-key --data-file=-
# This avoids ever writing real credentials into a .tfvars file, plan output,
# or Terraform state as a *planned* value (Terraform state will still contain
# whatever value *is* eventually stored if you do pass one in via -var, so
# out-of-band population remains the recommended path for real secrets).

variable "openai_api_key" {
  description = "Optional: OpenAI API key to seed into Secret Manager at apply time. Leave empty (default) and set it post-apply via `gcloud secrets versions add` instead."
  type        = string
  default     = ""
  sensitive   = true
}

variable "vertex_access_token" {
  description = <<-EOT
    Optional: static Vertex AI bearer token to seed into Secret Manager,
    matching the repo's dependency-light demo VertexAIProvider (which reads
    VERTEX_ACCESS_TOKEN directly). Leave empty and set post-apply, OR better
    yet, see `enable_vertex_workload_identity` below for the recommended
    production alternative that avoids static tokens entirely.
  EOT
  type        = string
  default     = ""
  sensitive   = true
}

variable "vertex_project_id" {
  description = "GCP project ID hosting the Vertex AI models the gateway should call. Defaults to var.project_id when left empty."
  type        = string
  default     = ""
}

variable "vertex_location" {
  description = "Vertex AI region, e.g. \"us-central1\" (matches VertexAIProvider's VERTEX_LOCATION env var)."
  type        = string
  default     = "us-central1"
}

variable "enable_vertex_workload_identity" {
  description = <<-EOT
    RECOMMENDED FOR PRODUCTION. When true, grants the Cloud Run runtime
    service account `roles/aiplatform.user` on the project so the gateway
    can authenticate to Vertex AI using Application Default Credentials
    (ADC) via Cloud Run's built-in workload identity, instead of the static
    VERTEX_ACCESS_TOKEN the repo's demo adapter currently reads. This is
    more secure (no long-lived bearer token to leak/rotate) but requires a
    code change: swap VertexAIProvider's manual `Authorization: Bearer
    <VERTEX_ACCESS_TOKEN>` header for `google-auth`'s ADC-based token
    fetch (e.g. `google.auth.default()` + `google.auth.transport.requests`).
    See docs/GCP_DEPLOYMENT.md for details. Defaults to false so the
    template matches the repo's current demo implementation out of the box.
  EOT
  type        = bool
  default     = false
}

variable "ollama_host" {
  description = <<-EOT
    Optional OLLAMA_HOST override. Left empty by default: Ollama is a local
    dev-loop provider in this repo (OllamaProvider probes it and falls back
    to the mock persona if unreachable), and there is normally no Ollama
    server reachable from a Cloud Run instance. Only set this if you've
    deployed Ollama somewhere reachable from the VPC connector.
  EOT
  type        = string
  default     = ""
}

# -----------------------------------------------------------------------------
# API Gateway
# -----------------------------------------------------------------------------

variable "enable_apis" {
  description = "Whether Terraform should enable the required GCP service APIs. Set to false if your project already has them enabled and you don't want Terraform managing that (e.g. shared/locked-down projects)."
  type        = bool
  default     = true
}
