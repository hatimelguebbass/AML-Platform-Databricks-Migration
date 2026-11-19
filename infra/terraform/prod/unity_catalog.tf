# The workspace joins the region's metastore when it is created. Terraform owns what the bundle
# cannot: the storage credential, the external locations and the catalog itself; the bundle owns
# the schemas, volumes and their grants (resources/schemas.yml).
resource "databricks_storage_credential" "lake" {
  provider = databricks.workspace
  name     = "${local.name}-lake"
  owner    = databricks_group.admins.display_name
  comment  = "Managed identity of ${azurerm_databricks_access_connector.lake.name} on ${azurerm_storage_account.lake.name}."

  azure_managed_identity {
    access_connector_id = azurerm_databricks_access_connector.lake.id
  }

  isolation_mode = "ISOLATION_MODE_ISOLATED"
  # The operator applying this keeps managing what it hands to the admins group only as a member.
  depends_on = [azurerm_role_assignment.connector_lake, databricks_mws_permission_assignment.admins, databricks_group_member.operators]
}

resource "databricks_external_location" "lake" {
  provider        = databricks.workspace
  for_each        = local.lake_url
  name            = "${local.name}-${each.key}"
  url             = each.value
  credential_name = databricks_storage_credential.lake.name
  owner           = databricks_group.admins.display_name
  isolation_mode  = "ISOLATION_MODE_ISOLATED"
  # Tables the bundle drops stay soft-deleted under the catalog's location for the retention period
  # and would block its deletion; the location is only destroyed with the whole window.
  force_destroy = true
  comment = {
    catalog = "Managed storage of the ${local.catalog} catalog."
    landing = "Deliveries of the on-premises export, behind the landing.deliveries volume (ADR 0003)."
  }[each.key]
}

locals {
  catalog = "aml_prod"
}

resource "databricks_catalog" "aml" {
  provider       = databricks.workspace
  name           = local.catalog
  storage_root   = databricks_external_location.lake["catalog"].url
  owner          = databricks_group.admins.display_name
  isolation_mode = "ISOLATED"
  comment        = "AML lakehouse, prod. Schemas, volumes and their grants are the bundle's."
}

# Isolated securables are visible from the workspaces bound to them only.
resource "databricks_workspace_binding" "catalog" {
  provider       = databricks.workspace
  securable_name = databricks_catalog.aml.name
  securable_type = "catalog"
  workspace_id   = local.workspace_id
}

resource "databricks_workspace_binding" "credential" {
  provider       = databricks.workspace
  securable_name = databricks_storage_credential.lake.name
  securable_type = "storage_credential"
  workspace_id   = local.workspace_id
}

resource "databricks_workspace_binding" "location" {
  provider       = databricks.workspace
  for_each       = databricks_external_location.lake
  securable_name = each.value.name
  securable_type = "external_location"
  workspace_id   = local.workspace_id
}

# Authoritative: any grant made by hand is removed at the next apply.
resource "databricks_grants" "catalog" {
  provider = databricks.workspace
  catalog  = databricks_catalog.aml.name

  # The deploying service principal creates and so owns the schemas; readers get schema grants.
  grant {
    principal  = databricks_group.aml["aml-pipeline"].display_name
    privileges = ["USE_CATALOG", "CREATE_SCHEMA"]
  }
  dynamic "grant" {
    for_each = toset(local.reader_groups)
    content {
      principal  = databricks_group.aml[grant.value].display_name
      privileges = ["USE_CATALOG"]
    }
  }
}

resource "databricks_grants" "landing" {
  provider          = databricks.workspace
  external_location = databricks_external_location.lake["landing"].name

  grant {
    principal  = databricks_group.aml["aml-pipeline"].display_name
    privileges = ["CREATE_EXTERNAL_VOLUME"]
  }
}
