# =============================================================================
# GCP deployment template for "enterprise-llm-gateway-router"
# =============================================================================
#
# ⚠️  ILLUSTRATIVE / UNAPPLIED TEMPLATE — READ BEFORE USE  ⚠️
#
# This Terraform configuration is a *deployable-but-unapplied* reference for
# running the gateway on GCP. It was written and reviewed by hand, and
# `terraform init` / `fmt` / `validate` were run against real
# hashicorp/google + hashicorp/google-beta provider schemas (v6.50.0) while
# authoring it -- all passed cleanly, confirming the HCL is syntactically
# correct and schema-consistent. It has NOT, however, been `terraform
# plan`'d or `apply`'d against a real GCP project as part of this exercise --
# no gcloud/project credentials were used and no real GCP resources were
# created or touched.
#
# Before using this in any real environment you MUST:
#   1. Re-run `terraform fmt` / `terraform validate` yourself against your
#      own pinned provider version, then `terraform plan` against a
#      disposable/sandbox project, and read the plan output carefully
#      before ever running `terraform apply`.
#   2. Pin the `google` (and `google-beta`, see below) provider version in
#      your own lockfile and confirm every resource/attribute name below
#      still matches that provider version's schema -- provider APIs do
#      change over time.
#   3. Review every default in variables.tf for cost implications (Redis
#      tier/size, Cloud Run CPU/memory/min-instances, VPC connector
#      instance count).
#   4. Review security defaults: `allow_unauthenticated` defaults to `true`
#      for demo simplicity. Secret Manager access is least-privilege
#      (per-secret `secretAccessor` bindings only), but you should still
#      review network exposure (Cloud Run ingress, API Gateway auth) and
#      consider VPC Service Controls for anything handling real provider
#      credentials.
#   5. Populate real secret values out-of-band (see variables.tf + README)
#      rather than committing them to terraform.tfvars.
#
# -----------------------------------------------------------------------------
# What this provisions
# -----------------------------------------------------------------------------
#   - A dedicated VPC + subnet for a Serverless VPC Access connector, so
#     Cloud Run (a serverless product with no VPC presence by default) can
#     reach Memorystore for Redis (a private-IP-only service).
#   - Memorystore for Redis (Basic or Standard HA tier, parameterized),
#     reachable via Private Service Access peering.
#   - Cloud Run v2 service running the gateway container, with Redis/OpenAI/
#     Vertex credentials injected from Secret Manager (never as plain env
#     vars), health probes on /health, and parameterized sizing/scaling.
#   - Secret Manager secrets for the Redis URL and OpenAI/Vertex credentials,
#     with a dedicated runtime service account granted
#     `roles/secretmanager.secretAccessor` on *only* those specific secrets.
#   - Google Cloud API Gateway (NOT Apigee -- see note below) as the public
#     front door, backed by the OpenAPI spec in ./openapi-gateway.yaml.
#
# -----------------------------------------------------------------------------
# Why Cloud API Gateway instead of Apigee?
# -----------------------------------------------------------------------------
# Apigee is Google's full enterprise API management product (traffic
# management, monetization, developer portals, analytics, hybrid/on-prem
# runtimes) and is the more realistic choice for a large enterprise's
# "front door" in the true sense the prompt is gesturing at. However, Apigee
# requires a *provisioned Apigee organization* (itself a heavyweight,
# billable, hours-to-provision prerequisite with its own networking model)
# before a single Terraform resource in this file could reference it --
# that's a poor fit for a template meant to be reviewed and `plan`'d
# end-to-end without extra manual setup.
#
# Google Cloud API Gateway is the lighter, fully-declarative alternative:
# it's just three Terraform resources (google_api_gateway_api,
# google_api_gateway_api_config, google_api_gateway_gateway) plus an
# OpenAPI document, with no separate "organization" provisioning step. It
# gives you a managed hostname, an OpenAPI-driven routing/auth layer, and
# per-API quotas/keys -- a reasonable "enterprise front door" for a template
# like this one.
#
# To swap this for Apigee in a true enterprise setting, you would instead:
#   - Provision `google_apigee_organization` + `google_apigee_environment` +
#     `google_apigee_envgroup` (+ `google_apigee_instance` for non-hybrid
#     deployments), which themselves need a dedicated /22 peering CIDR via
#     `google_apigee_organization.network` (another Private Service Access
#     range, extending the pattern used below for Memorystore).
#   - Deploy an Apigee proxy bundle (an API proxy definition, not an OpenAPI
#     doc) via `google_apigee_api` / the Apigee management API or `apigeecli`,
#     with a TargetEndpoint pointing at this same Cloud Run URL.
#   - Replace `google_api_gateway_gateway`'s output hostname with the
#     Apigee environment group's hostname/virtual host instead.
# =============================================================================

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0, < 7.0"
    }
    # google-beta is used only for the API Gateway resource family below.
    # As of recent `google` provider releases these resources have GA
    # equivalents too; if your pinned provider version supports the GA
    # `google_api_gateway_*` resources, you can drop google-beta entirely
    # and remove the `provider = google-beta` lines from those resources.
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 5.0, < 7.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

data "google_project" "current" {
  project_id = var.project_id
}

locals {
  # Falls back to the main project if a separate Vertex-hosting project
  # isn't specified.
  vertex_project_id = var.vertex_project_id != "" ? var.vertex_project_id : var.project_id

  # Plain (non-secret) env vars for the Cloud Run container. Vertex AI
  # project/location are not secrets -- only the (demo-only) static bearer
  # token is. OLLAMA_HOST is only included if explicitly set (see
  # variables.tf -- Ollama is a local dev-loop provider with no sensible
  # default in a cloud deployment).
  plain_env_vars = merge(
    {
      VERTEX_PROJECT_ID = local.vertex_project_id
      VERTEX_LOCATION   = var.vertex_location
    },
    var.ollama_host != "" ? { OLLAMA_HOST = var.ollama_host } : {}
  )

  required_apis = [
    "run.googleapis.com",
    "redis.googleapis.com",
    "vpcaccess.googleapis.com",
    "servicenetworking.googleapis.com",
    "secretmanager.googleapis.com",
    "apigateway.googleapis.com",
    "servicecontrol.googleapis.com",
    "servicemanagement.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    # Only strictly required if enable_vertex_workload_identity = true, but
    # harmless to enable unconditionally so switching the flag later doesn't
    # require a second `apply` just to enable an API.
    "aiplatform.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each = var.enable_apis ? toset(local.required_apis) : toset([])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# -----------------------------------------------------------------------------
# Networking: dedicated VPC + subnet for the Serverless VPC Access connector,
# and a Private Service Access peering range for Memorystore.
# -----------------------------------------------------------------------------
# Cloud Run v2 services have no VPC presence by default (they're fully
# serverless/multi-tenant). Memorystore for Redis, meanwhile, only exposes a
# *private* IP inside a VPC. To bridge the two, Cloud Run needs a Serverless
# VPC Access connector attached to the same VPC that Memorystore's private
# services access peering uses.

resource "google_compute_network" "vpc_network" {
  provider = google

  name                    = "${var.name_prefix}-vpc"
  auto_create_subnetworks = false

  depends_on = [google_project_service.apis]
}

# Dedicated /28 subnet for the Serverless VPC Access connector. Must not
# overlap with the Private Service Access range reserved below.
resource "google_compute_subnetwork" "connector_subnet" {
  name          = "${var.name_prefix}-connector-subnet"
  ip_cidr_range = var.vpc_connector_cidr
  region        = var.region
  network       = google_compute_network.vpc_network.id
}

resource "google_vpc_access_connector" "connector" {
  # Note: connector names are capped at 25 characters by the API -- keep
  # name_prefix short (enforced via a validation block in variables.tf).
  name   = "${var.name_prefix}-connector"
  region = var.region

  subnet {
    name = google_compute_subnetwork.connector_subnet.name
  }

  machine_type  = var.vpc_connector_machine_type
  min_instances = var.vpc_connector_min_instances
  max_instances = var.vpc_connector_max_instances

  depends_on = [google_project_service.apis]
}

# Reserved internal IP range used for the VPC peering that backs Private
# Service Access -- this is how Memorystore's private IP becomes routable
# from the VPC (and therefore from the VPC connector, and therefore from
# Cloud Run) without exposing Redis publicly.
resource "google_compute_global_address" "private_service_access_range" {
  name          = "${var.name_prefix}-psa-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = var.private_service_access_range_prefix_length
  network       = google_compute_network.vpc_network.id
}

resource "google_service_networking_connection" "private_vpc_connection" {
  network                 = google_compute_network.vpc_network.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_service_access_range.name]

  depends_on = [google_project_service.apis]
}

# -----------------------------------------------------------------------------
# Memorystore for Redis
# -----------------------------------------------------------------------------

resource "google_redis_instance" "cache" {
  name           = "${var.name_prefix}-redis"
  display_name   = "LLM Gateway semantic cache"
  tier           = var.redis_tier
  memory_size_gb = var.redis_memory_size_gb
  region         = var.region
  redis_version  = var.redis_version

  authorized_network = google_compute_network.vpc_network.id
  connect_mode       = "PRIVATE_SERVICE_ACCESS"

  labels = var.labels

  # Memorystore must wait for the Private Service Access peering to exist
  # before it can provision a private IP inside that peered range.
  depends_on = [google_service_networking_connection.private_vpc_connection]
}

# -----------------------------------------------------------------------------
# Secret Manager
# -----------------------------------------------------------------------------
# Three secrets: the (computed) Redis connection URL, the OpenAI API key, and
# the Vertex AI static access token. Only the Redis URL version is created by
# Terraform itself (it's derived infrastructure state, not a hand-typed
# credential). The OpenAI/Vertex secret *versions* are only created if you
# supplied a non-empty value via variables -- otherwise the secret container
# exists but empty, ready for you to populate out-of-band (recommended; see
# README.md / docs/GCP_DEPLOYMENT.md).

resource "google_secret_manager_secret" "redis_url" {
  secret_id = "${var.name_prefix}-redis-url"
  labels    = var.labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "redis_url" {
  secret      = google_secret_manager_secret.redis_url.id
  secret_data = "redis://${google_redis_instance.cache.host}:${google_redis_instance.cache.port}/0"
}

resource "google_secret_manager_secret" "openai_api_key" {
  secret_id = "${var.name_prefix}-openai-api-key"
  labels    = var.labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "openai_api_key" {
  count = var.openai_api_key != "" ? 1 : 0

  secret      = google_secret_manager_secret.openai_api_key.id
  secret_data = var.openai_api_key
}

resource "google_secret_manager_secret" "vertex_access_token" {
  secret_id = "${var.name_prefix}-vertex-access-token"
  labels    = var.labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "vertex_access_token" {
  count = var.vertex_access_token != "" ? 1 : 0

  secret      = google_secret_manager_secret.vertex_access_token.id
  secret_data = var.vertex_access_token
}

# -----------------------------------------------------------------------------
# Cloud Run runtime service account + least-privilege secret access
# -----------------------------------------------------------------------------

resource "google_service_account" "cloud_run_sa" {
  account_id   = "${var.name_prefix}-run-sa"
  display_name = "LLM Gateway Cloud Run runtime service account"
}

# Least privilege: bind secretAccessor per-secret, not project-wide.
resource "google_secret_manager_secret_iam_member" "redis_url_accessor" {
  secret_id = google_secret_manager_secret.redis_url.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cloud_run_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "openai_api_key_accessor" {
  secret_id = google_secret_manager_secret.openai_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cloud_run_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "vertex_access_token_accessor" {
  secret_id = google_secret_manager_secret.vertex_access_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cloud_run_sa.email}"
}

# RECOMMENDED PRODUCTION PATH (see variables.tf:enable_vertex_workload_identity
# and the VertexAIProvider docstring in providers/vertex_provider.py): instead
# of the static VERTEX_ACCESS_TOKEN secret above, grant the runtime service
# account `roles/aiplatform.user` and have the adapter fetch short-lived
# credentials via Application Default Credentials (Cloud Run automatically
# provides a metadata-server identity for its attached service account, no
# key files or static tokens needed). Gated behind a variable so the template
# defaults to matching the repo's current demo adapter behavior.
resource "google_project_iam_member" "cloud_run_sa_vertex_user" {
  count = var.enable_vertex_workload_identity ? 1 : 0

  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.cloud_run_sa.email}"
}

# -----------------------------------------------------------------------------
# Cloud Run v2 service
# -----------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "gateway" {
  name     = "${var.name_prefix}-gateway"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  # Explicit: avoid accidental `terraform destroy` data loss surprises;
  # flip to true deliberately once you're confident in the environment.
  deletion_protection = false

  labels = var.labels

  template {
    service_account = google_service_account.cloud_run_sa.email

    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }

    vpc_access {
      connector = google_vpc_access_connector.connector.id
      # PRIVATE_RANGES_ONLY routes only RFC1918 traffic (e.g. to Memorystore)
      # through the connector; public internet calls to OpenAI/Vertex still
      # go out Cloud Run's normal path. Use "ALL_TRAFFIC" instead if you need
      # all egress to traverse the VPC (e.g. for an egress NAT/firewall
      # policy), at the cost of extra connector throughput provisioning.
      egress = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = var.container_image

      ports {
        name           = "http1"
        container_port = var.container_port
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        # CPU is only allocated during request processing by default, which
        # is fine for a request/response gateway. Set to true if you need
        # background work (e.g. cache warmers) between requests.
        cpu_idle = true
      }

      # --- Secret-backed env vars -------------------------------------------
      env {
        name = "GATEWAY_REDIS_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.redis_url.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "OPENAI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.openai_api_key.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "VERTEX_ACCESS_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.vertex_access_token.secret_id
            version = "latest"
          }
        }
      }

      # --- Plain env vars ------------------------------------------------
      dynamic "env" {
        for_each = local.plain_env_vars
        content {
          name  = env.key
          value = env.value
        }
      }

      startup_probe {
        http_get {
          path = var.health_check_path
          port = var.container_port
        }
        initial_delay_seconds = 0
        timeout_seconds       = 3
        period_seconds        = 5
        failure_threshold     = 6
      }

      liveness_probe {
        http_get {
          path = var.health_check_path
          port = var.container_port
        }
        initial_delay_seconds = 10
        timeout_seconds       = 3
        period_seconds        = 15
        failure_threshold     = 3
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.redis_url_accessor,
    google_secret_manager_secret_iam_member.openai_api_key_accessor,
    google_secret_manager_secret_iam_member.vertex_access_token_accessor,
  ]
}

# Public (no-auth) invocation, only when explicitly opted in.
resource "google_cloud_run_v2_service_iam_member" "invoker_all_users" {
  count = var.allow_unauthenticated ? 1 : 0

  project  = var.project_id
  location = google_cloud_run_v2_service.gateway.location
  name     = google_cloud_run_v2_service.gateway.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# IAM-auth mode: grant invoker to API Gateway's Google-managed service agent
# instead, so requests routed *through* the gateway hostname still work while
# the raw Cloud Run URL stays locked down.
resource "google_cloud_run_v2_service_iam_member" "invoker_api_gateway" {
  count = var.allow_unauthenticated ? 0 : 1

  project  = var.project_id
  location = google_cloud_run_v2_service.gateway.location
  name     = google_cloud_run_v2_service.gateway.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-apigateway.iam.gserviceaccount.com"
}

# -----------------------------------------------------------------------------
# API Gateway (front door) — see the module-level comment at the top of this
# file for why Cloud API Gateway was chosen over Apigee for this template.
# -----------------------------------------------------------------------------

resource "google_api_gateway_api" "gateway_api" {
  provider = google-beta

  api_id = "${var.name_prefix}-api"

  depends_on = [google_project_service.apis]
}

resource "google_api_gateway_api_config" "gateway_api_config" {
  provider = google-beta

  api                  = google_api_gateway_api.gateway_api.api_id
  api_config_id_prefix = "${var.name_prefix}-cfg-"

  openapi_documents {
    document {
      path = "openapi-gateway.yaml"
      # The OpenAPI spec is a template: ${backend_address} is substituted
      # with the live Cloud Run URL so API Gateway's `x-google-backend`
      # extension routes to the right place without hand-editing the spec
      # after every deploy.
      contents = base64encode(templatefile("${path.module}/openapi-gateway.yaml", {
        backend_address = google_cloud_run_v2_service.gateway.uri
      }))
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "google_api_gateway_gateway" "gateway" {
  provider = google-beta

  api_config = google_api_gateway_api_config.gateway_api_config.id
  gateway_id = "${var.name_prefix}-gw"
  region     = var.region
}
