# =============================================================================
# Outputs for the GCP deployment template
# =============================================================================

output "cloud_run_service_url" {
  description = <<-EOT
    Direct URL of the Cloud Run service (bypasses API Gateway). Useful for
    debugging/`curl`-ing /health directly, but prefer the API Gateway
    hostname below for real traffic if allow_unauthenticated = false, since
    this URL requires IAM auth in that mode.
  EOT
  value       = google_cloud_run_v2_service.gateway.uri
}

output "cloud_run_service_name" {
  description = "Name of the Cloud Run v2 service."
  value       = google_cloud_run_v2_service.gateway.name
}

output "cloud_run_service_account_email" {
  description = "Email of the Cloud Run runtime service account (holds secretAccessor on the three Secret Manager secrets below, and optionally roles/aiplatform.user)."
  value       = google_service_account.cloud_run_sa.email
}

output "api_gateway_default_hostname" {
  description = "Default public hostname of the API Gateway front door -- this is the URL clients should call (e.g. https://<hostname>/health)."
  value       = google_api_gateway_gateway.gateway.default_hostname
}

output "redis_host" {
  description = "Private IP address of the Memorystore Redis instance (reachable only from within the VPC / via the Serverless VPC Access connector)."
  value       = google_redis_instance.cache.host
}

output "redis_port" {
  description = "Port of the Memorystore Redis instance."
  value       = google_redis_instance.cache.port
}

output "redis_connection_summary" {
  description = "Convenience \"host:port\" summary of the Redis endpoint."
  value       = "${google_redis_instance.cache.host}:${google_redis_instance.cache.port}"
}

output "secret_redis_url_id" {
  description = "Secret Manager secret ID holding the full redis:// connection URL injected into Cloud Run as GATEWAY_REDIS_URL."
  value       = google_secret_manager_secret.redis_url.secret_id
}

output "secret_openai_api_key_id" {
  description = "Secret Manager secret ID for the OpenAI API key. Populate its value with `gcloud secrets versions add` (see README.md) if not supplied via variables."
  value       = google_secret_manager_secret.openai_api_key.secret_id
}

output "secret_vertex_access_token_id" {
  description = "Secret Manager secret ID for the Vertex AI static access token (demo auth path -- see enable_vertex_workload_identity for the recommended production alternative)."
  value       = google_secret_manager_secret.vertex_access_token.secret_id
}

output "vpc_access_connector_id" {
  description = "Resource ID of the Serverless VPC Access connector Cloud Run uses to reach Memorystore."
  value       = google_vpc_access_connector.connector.id
}
