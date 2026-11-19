provider "azurerm" {
  features {}
  subscription_id     = var.subscription_id
  storage_use_azuread = true
}

provider "azapi" {
  subscription_id = var.subscription_id
}

provider "azuread" {
  tenant_id = var.tenant_id
}

# Workspace-level objects: Unity Catalog securables, warehouse, permissions.
provider "databricks" {
  alias                       = "workspace"
  host                        = local.workspace_url
  azure_workspace_resource_id = azapi_resource.workspace.id
  azure_tenant_id             = var.tenant_id
}

# Account-level objects: groups, service principal, workspace assignment.
provider "databricks" {
  alias           = "account"
  host            = "https://accounts.azuredatabricks.net"
  account_id      = var.databricks_account_id
  azure_tenant_id = var.tenant_id
}
