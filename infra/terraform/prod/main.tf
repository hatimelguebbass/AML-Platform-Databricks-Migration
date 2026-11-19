locals {
  name = "amlmig-prod"
  # Storage account names are global: a suffix derived from the subscription keeps reruns stable.
  suffix = substr(sha1(var.subscription_id), 0, 6)
  tags = {
    project     = "aml-platform-databricks-migration"
    environment = "prod"
    managed_by  = "terraform"
  }
  workspace_url = "https://${azapi_resource.workspace.output.properties.workspaceUrl}"
}

resource "azurerm_resource_group" "platform" {
  name     = "rg-${local.name}"
  location = var.location
  tags     = local.tags
}
