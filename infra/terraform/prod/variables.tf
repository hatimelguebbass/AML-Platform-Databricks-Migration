variable "subscription_id" {
  description = "Subscription of the prod window."
  type        = string
}

variable "tenant_id" {
  description = "Entra ID tenant of the subscription."
  type        = string
}

variable "location" {
  description = "Region of every resource; it must offer serverless compute."
  type        = string
  default     = "francecentral"
}


variable "databricks_account_id" {
  description = "Azure Databricks account of the tenant (account console, top right)."
  type        = string
}

variable "operators" {
  description = "Account users who administer the platform (data-platform-admins)."
  type        = list(string)
}

variable "github_repository" {
  description = "Repository whose prod environment deploys, as GitHub's OIDC subject names it: owner@owner_id/name@repository_id (immutable identifiers, so a renamed or recreated repository cannot inherit the trust)."
  type        = string
  default     = "Revoyaldev@339949978/aml-platform-databricks-migration@1458213653"
}

variable "alert_emails" {
  description = "Mailboxes of the budget alerts."
  type        = list(string)
}

variable "budget_amount" {
  description = "Monthly budget of the subscription, in its billing currency."
  type        = number
  default     = 40
}

variable "budget_start" {
  description = "First day of the budget's first month (RFC 3339); null takes the month of the plan."
  type        = string
  default     = null
}
