output "workspace_url" {
  description = "DATABRICKS_HOST of the prod target."
  value       = local.workspace_url
}

output "deployer_client_id" {
  description = "ARM_CLIENT_ID of the prod GitHub environment."
  value       = azuread_service_principal.deployer.client_id
}

output "landing_location" {
  description = "Storage location of the landing.deliveries volume (bundle variable landing_location)."
  value       = databricks_external_location.lake["landing"].url
}
