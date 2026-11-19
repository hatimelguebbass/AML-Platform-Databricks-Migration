# People and pipelines get access through account groups only (ADR 0005). The groups are the same
# as in dev; data-platform-admins owns the catalog and administers the workspace.
locals {
  # aml-migration is the on-premises migration tooling run by the operators: it delivers the exports
  # and reads the published layers for the parity checks (ADR 0008). It goes with the source.
  reader_groups = ["aml-compliance", "aml-analysts", "aml-reporting", "aml-migration"]
  aml_groups    = concat(local.reader_groups, ["aml-pipeline"])
  workspace_id  = tonumber(azapi_resource.workspace.output.properties.workspaceId)
}

resource "databricks_group" "aml" {
  provider     = databricks.account
  for_each     = toset(local.aml_groups)
  display_name = each.key
}

resource "databricks_group" "admins" {
  provider     = databricks.account
  display_name = "data-platform-admins"
}

resource "databricks_mws_permission_assignment" "admins" {
  provider     = databricks.account
  workspace_id = local.workspace_id
  principal_id = databricks_group.admins.id
  permissions  = ["ADMIN"]
}

resource "databricks_mws_permission_assignment" "aml" {
  provider     = databricks.account
  for_each     = databricks_group.aml
  workspace_id = local.workspace_id
  principal_id = each.value.id
  permissions  = ["USER"]
}

# Azure Databricks refuses browser sign-in to personal Microsoft accounts (guests of the tenant):
# operators are users of the tenant itself. force adopts a user the account already has.
resource "databricks_user" "operators" {
  provider  = databricks.account
  for_each  = toset(var.operators)
  user_name = each.key
  force     = true
}

resource "databricks_group_member" "operators" {
  provider  = databricks.account
  for_each  = databricks_user.operators
  group_id  = databricks_group.admins.id
  member_id = each.value.id
}

resource "databricks_group_member" "migration_operators" {
  provider  = databricks.account
  for_each  = databricks_user.operators
  group_id  = databricks_group.aml["aml-migration"].id
  member_id = each.value.id
}

# CI deploys as this service principal: GitHub's OIDC token for the repository's prod environment
# is exchanged for an Entra token, so no secret exists anywhere (ADR 0007).
resource "azuread_application" "deployer" {
  display_name = "${local.name}-deployer"
}

resource "azuread_service_principal" "deployer" {
  client_id = azuread_application.deployer.client_id
}

resource "azuread_application_federated_identity_credential" "github_prod" {
  application_id = azuread_application.deployer.id
  display_name   = "github-aml-platform-databricks-migration-prod"
  description    = "Deployments from the prod environment of ${var.github_repository}."
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:${var.github_repository}:environment:prod"
}

resource "databricks_service_principal" "deployer" {
  provider       = databricks.account
  application_id = azuread_service_principal.deployer.client_id
  display_name   = azuread_application.deployer.display_name
  # At account level the provider only deactivates by default; the window leaves nothing behind.
  disable_as_user_deletion = false
}

# The pipeline group: it reads the masked columns in clear and builds every layer.
resource "databricks_group_member" "deployer" {
  provider  = databricks.account
  group_id  = databricks_group.aml["aml-pipeline"].id
  member_id = databricks_service_principal.deployer.id
}

# The workspace's users group carries no entitlement in a serverless workspace: each group gets
# the workspace and SQL access it needs, and nothing reaches people outside the groups.
resource "databricks_entitlements" "aml" {
  provider              = databricks.workspace
  for_each              = databricks_group.aml
  group_id              = each.value.id
  workspace_access      = true
  databricks_sql_access = true

  depends_on = [databricks_mws_permission_assignment.aml]
}
