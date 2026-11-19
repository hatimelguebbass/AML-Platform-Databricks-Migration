# ADLS Gen2 for what Unity Catalog stores in prod: the catalog's managed tables and the landing
# volume. Keys are disabled; Databricks reaches it through the access connector's managed identity.
resource "azurerm_storage_account" "lake" {
  name                            = "stamlmigprod${local.suffix}"
  resource_group_name             = azurerm_resource_group.platform.name
  location                        = azurerm_resource_group.platform.location
  account_tier                    = "Standard"
  account_kind                    = "StorageV2"
  account_replication_type        = "LRS"
  is_hns_enabled                  = true
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false
  default_to_oauth_authentication = true
  tags                            = local.tags

  blob_properties {
    delete_retention_policy {
      days = 7
    }
    container_delete_retention_policy {
      days = 7
    }
  }
}

resource "azurerm_storage_container" "lake" {
  for_each           = toset(["catalog", "landing"])
  name               = each.key
  storage_account_id = azurerm_storage_account.lake.id
}

resource "azurerm_databricks_access_connector" "lake" {
  name                = "dbac-${local.name}"
  resource_group_name = azurerm_resource_group.platform.name
  location            = azurerm_resource_group.platform.location
  tags                = local.tags

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_role_assignment" "connector_lake" {
  scope                = azurerm_storage_account.lake.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_databricks_access_connector.lake.identity[0].principal_id
}

locals {
  lake_url = { for name, _ in azurerm_storage_container.lake : name => "abfss://${name}@${azurerm_storage_account.lake.name}.dfs.core.windows.net/" }
}
