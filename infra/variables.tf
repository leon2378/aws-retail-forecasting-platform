variable "region" {
  type    = string
  default = "ap-southeast-2"

}
variable "project" {

  type    = string
  default = "retail-forecast"
  validation {

    condition     = can(regex("^[a-z][a-z0-9-]{1,19}[a-z0-9]$", var.project))
    error_message = "Use 3-21 lowercase letters, numbers and hyphens, starting with a letter and ending with a letter or number."

  }

}
variable "lambda_zip" {
  type    = string
  default = "../build/lambda.zip"

}
variable "schedule_enabled" {
  type    = bool
  default = false

}
variable "schedule_expression" {
  type    = string
  default = "rate(1 day)"

}
variable "initial_cutoff" {

  type    = number
  default = 196
  validation {

    condition     = var.initial_cutoff >= 196 && floor(var.initial_cutoff) == var.initial_cutoff
    error_message = "Initial cutoff must be an integer of at least 196."

  }

}
variable "candidate_model" {

  type    = string
  default = "seasonal"
  validation {

    condition     = contains(["seasonal", "xgboost"], var.candidate_model)
    error_message = "Candidate must be seasonal or xgboost."

  }

}
variable "sagemaker_instance_type" {
  type        = string
  default     = "ml.m5.xlarge"
  description = "Type used when CodeBuild generates the ML pipeline; verify all three job quotas in the target region."
  validation {
    condition     = can(regex("^ml\\.[a-z0-9]+\\.[a-z0-9]+$", var.sagemaker_instance_type))
    error_message = "Use a SageMaker instance type such as ml.m5.xlarge."
  }
}

variable "pipeline_definition_file" {

  type        = string
  default     = ""
  description = "Generated pipeline JSON. Leave blank for initial foundation deployment."

}
variable "enable_delivery" {
  type    = bool
  default = false

}
variable "source_object_key" {
  type    = string
  default = "source/source.zip"

}
variable "enable_clearml" {
  type    = bool
  default = false

}
variable "clearml_vpc_id" {
  type    = string
  default = ""

}
variable "clearml_subnet_id" {
  type    = string
  default = ""

}
variable "clearml_ami_id" {

  type        = string
  default     = ""
  description = "Reviewed ClearML Server AMI in Sydney; bring your own or build from documented upstream Compose."

}
variable "clearml_instance_type" {
  type    = string
  default = "t3.xlarge"

}

variable "auth_additional_callback_urls" {
  type        = list(string)
  default     = []
  description = "Optional additional sign-in redirect roots, including explicit loopback previews. Registration only: the application's default callback remains the CloudFront root."
  validation {
    condition = length(var.auth_additional_callback_urls) <= 20 && alltrue([
      for url in var.auth_additional_callback_urls :
      can(regex("^https://[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{1,5})?/$", url)) ||
      can(regex("^http://(localhost|127\\.0\\.0\\.1|\\[::1\\])(:[0-9]{1,5})?/$", url))
    ])
    error_message = "Use at most 20 HTTPS root URLs ending in /, or explicit localhost, 127.0.0.1 or [::1] HTTP roots for testing. Queries, fragments and remote HTTP callbacks are not allowed."
  }
}

variable "auth_additional_logout_urls" {
  type        = list(string)
  default     = []
  description = "Optional additional sign-out redirect roots. Registration only: the application's default sign-out destination remains the CloudFront root."
  validation {
    condition = length(var.auth_additional_logout_urls) <= 20 && alltrue([
      for url in var.auth_additional_logout_urls :
      can(regex("^https://[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{1,5})?/$", url)) ||
      can(regex("^http://(localhost|127\\.0\\.0\\.1|\\[::1\\])(:[0-9]{1,5})?/$", url))
    ])
    error_message = "Use at most 20 HTTPS root URLs ending in /, or explicit localhost, 127.0.0.1 or [::1] HTTP roots for testing. Queries, fragments and remote HTTP sign-out redirects are not allowed."
  }
}

variable "monitoring_enabled" {
  type        = bool
  default     = false
  description = "Opt in to the account budget, email topic and job failure rules after reviewing deployment. Does not start or stop jobs."
}

variable "alert_email" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Private recipient for budget and operations emails; keep in ignored local configuration. SNS email requires confirmation after deployment."
  validation {
    condition     = var.alert_email == "" || (length(var.alert_email) <= 254 && can(regex("^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\\.[A-Za-z]{2,63}$", var.alert_email)))
    error_message = "Enter a valid email address without whitespace, or leave it blank while monitoring is disabled."
  }
  validation {
    condition     = !var.monitoring_enabled || var.alert_email != ""
    error_message = "Set a private alert_email before enabling monitoring."
  }
}

variable "monthly_budget_usd" {
  type        = number
  default     = 10
  description = "Account-wide monthly USD spend warning excluding credits, refunds and discounts. This is an alert threshold, not a spending cap."
  validation {
    condition     = var.monthly_budget_usd >= 1 && var.monthly_budget_usd <= 1000 && floor(var.monthly_budget_usd * 100) == var.monthly_budget_usd * 100
    error_message = "Choose a monthly budget from 1 to 1000 USD with at most two decimal places."
  }
}
