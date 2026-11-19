variable "subscription_id" {
  description = "Subscription of the prod window."
  type        = string
}

variable "location" {
  description = "Region of the state account; the same as the platform's."
  type        = string
  default     = "francecentral"
}
