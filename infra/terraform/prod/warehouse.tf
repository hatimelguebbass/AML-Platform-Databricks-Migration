# Runs the dbt tasks of the daily job and, after the cutover, the reports. Serverless: it starts in
# seconds, so it stops after one idle minute instead of billing an idle quarter hour.
resource "databricks_sql_endpoint" "aml" {
  provider                  = databricks.workspace
  name                      = "aml-prod"
  cluster_size              = "2X-Small"
  min_num_clusters          = 1
  max_num_clusters          = 1
  auto_stop_mins            = 1
  enable_serverless_compute = true
  warehouse_type            = "PRO"

  tags {
    dynamic "custom_tags" {
      for_each = local.tags
      content {
        key   = custom_tags.key
        value = custom_tags.value
      }
    }
  }

  depends_on = [databricks_mws_permission_assignment.admins]
}

resource "databricks_permissions" "warehouse" {
  provider        = databricks.workspace
  sql_endpoint_id = databricks_sql_endpoint.aml.id

  access_control {
    group_name       = databricks_group.admins.display_name
    permission_level = "CAN_MANAGE"
  }
  dynamic "access_control" {
    for_each = databricks_group.aml
    content {
      group_name       = access_control.value.display_name
      permission_level = "CAN_USE"
    }
  }

  # A group is known to the workspace only once assigned to it; granting before fails.
  depends_on = [databricks_mws_permission_assignment.aml]
}
