# The free subscription's spending limit stops everything at the end of the credit; this alert
# warns well before, on actual and forecast cost.
resource "azurerm_consumption_budget_subscription" "window" {
  name            = "budget-${local.name}"
  subscription_id = "/subscriptions/${var.subscription_id}"
  amount          = var.budget_amount
  time_grain      = "Monthly"

  time_period {
    start_date = coalesce(var.budget_start, formatdate("YYYY-MM-01'T00:00:00Z'", plantimestamp()))
  }

  # Azure refuses a start date in the past, and the month of the plan moves on after the first apply.
  lifecycle {
    ignore_changes = [time_period]
  }

  dynamic "notification" {
    for_each = { actual-50 = ["Actual", 50], actual-80 = ["Actual", 80], forecast-100 = ["Forecasted", 100] }
    content {
      operator       = "GreaterThan"
      threshold      = notification.value[1]
      threshold_type = notification.value[0]
      contact_emails = var.alert_emails
    }
  }
}
