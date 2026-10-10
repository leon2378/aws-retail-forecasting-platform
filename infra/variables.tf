variable "region" {
  type    = string
  default = "ap-southeast-2"
}
variable "project" {
  type    = string
  default = "orderflow"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,19}[a-z0-9]$", var.project))
    error_message = "Use 3-21 lowercase letters, numbers and hyphens."
  }
}
variable "table_deletion_protection_enabled" {
  type        = bool
  default     = true
  description = "Turn off only in a separately reviewed decommissioning change after a verified backup."
}
variable "lambda_zip" {
  type    = string
  default = "../build/lambda.zip"
}
variable "schedule_enabled" {
  type        = bool
  default     = false
  description = "Opt in to scheduled outbox repair after validating the deployed flow. Streams publish immediate work independently."
}
variable "schedule_expression" {
  type    = string
  default = "rate(5 minutes)"
}
variable "worker_enabled" {
  type        = bool
  default     = true
  description = "Set false to stop consuming new work without deleting the durable queue."
}
variable "api_enabled" {
  type        = bool
  default     = true
  description = "Set false to stop new API requests with zero route throttles during a reviewed shutdown."
}
variable "outbox_enabled" {
  type        = bool
  default     = true
  description = "Set false to stop DynamoDB stream publication during an incident. Pending records remain durable."
}
variable "enable_delivery" {
  type        = bool
  default     = false
  description = "Opt in to a manual S3-source CodePipeline with validation and a deployment approval stage."
}
variable "source_object_key" {
  type    = string
  default = "source/source.zip"
}
variable "auth_additional_callback_urls" {
  type    = list(string)
  default = []
  validation {
    condition     = length(var.auth_additional_callback_urls) <= 20 && alltrue([for url in var.auth_additional_callback_urls : can(regex("^https://[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{1,5})?/$", url)) || can(regex("^http://(localhost|127\\.0\\.0\\.1|\\[::1\\])(:[0-9]{1,5})?/$", url))])
    error_message = "Use HTTPS root callbacks ending in /, or explicit HTTP loopback roots."
  }
}
variable "auth_additional_logout_urls" {
  type    = list(string)
  default = []
  validation {
    condition     = length(var.auth_additional_logout_urls) <= 20 && alltrue([for url in var.auth_additional_logout_urls : can(regex("^https://[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{1,5})?/$", url)) || can(regex("^http://(localhost|127\\.0\\.0\\.1|\\[::1\\])(:[0-9]{1,5})?/$", url))])
    error_message = "Use HTTPS root logout destinations ending in /, or explicit HTTP loopback roots."
  }
}
variable "monitoring_enabled" {
  type        = bool
  default     = false
  description = "Enable budget emails and SNS operations subscriptions. Alarms and dashboard exist regardless."
}
variable "alert_email" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Private notification recipient; SNS requires confirmation. Never place a real address in tracked files."
  validation {
    condition     = var.alert_email == "" || can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "Provide an email address or leave it empty with monitoring disabled."
  }
  validation {
    condition     = !var.monitoring_enabled || var.alert_email != ""
    error_message = "A private alert_email is required when enabling notifications."
  }
}
variable "monthly_budget_usd" {
  type        = number
  default     = 10
  description = "Account-wide monthly warning excluding credits. This is not a spending cap."
  validation {
    condition     = var.monthly_budget_usd >= 1 && var.monthly_budget_usd <= 1000
    error_message = "Choose a budget warning between 1 and 1000 USD."
  }
}
