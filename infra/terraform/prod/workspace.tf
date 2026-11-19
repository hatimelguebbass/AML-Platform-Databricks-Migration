# A serverless workspace: compute runs in Databricks' network and is billed per use, so the
# subscription needs no VM quota and nothing runs between jobs (ADR 0007). azurerm has no
# computeMode yet, hence the ARM resource through azapi.
resource "azapi_resource" "workspace" {
  type      = "Microsoft.Databricks/workspaces@2026-01-01"
  name      = "dbw-${local.name}"
  parent_id = azurerm_resource_group.platform.id
  location  = var.location
  tags      = local.tags

  body = {
    sku = {
      # The trial tier is refused for serverless workspaces (InvalidSkuForServerlessWorkspace).
      name = "premium"
    }
    properties = {
      computeMode = "Serverless"
    }
  }

  response_export_values = ["properties.workspaceUrl", "properties.workspaceId"]
}
