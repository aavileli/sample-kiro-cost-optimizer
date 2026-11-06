variable "name" {
  type        = string
  description = "Name prefix for all resources. Equivalent to the CloudFormation stack name."
  default     = "kiro-cost-optimizer"
}

variable "use_existing_bucket" {
  type        = bool
  default     = false
  description = <<-EOT
    Set to true if you already have an S3 bucket with Kiro user activity reports
    enabled. When true, bucket_name is required and this module does NOT modify
    your bucket policy or lifecycle configuration (see the
    required_bucket_policy_statement and optional_s3_lifecycle_policy outputs).
    When false, a new bucket is created with the correct policy and lifecycle rule.
  EOT
}

variable "bucket_name" {
  type        = string
  default     = ""
  description = <<-EOT
    S3 bucket name (no "/"). Required when use_existing_bucket is true. When
    use_existing_bucket is false, this is the name of the new bucket; if empty,
    a unique name is generated.
  EOT
}

variable "report_prefix" {
  type        = string
  default     = "kiro-user-activity"
  description = "S3 prefix (no leading or trailing \"/\") where Kiro writes the CSV reports. Kiro requires a prefix."

  validation {
    condition     = can(regex("^[a-zA-Z0-9!_.*'()-]+( [a-zA-Z0-9!_.*'()-]+)*$", var.report_prefix))
    error_message = "report_prefix must contain only valid S3 key characters, with no leading/trailing spaces or consecutive spaces."
  }
}

variable "kiro_profile_region" {
  type        = string
  default     = "us-east-1"
  description = <<-EOT
    AWS Region where your Kiro profile was installed (us-east-1 or eu-central-1).
    When use_existing_bucket is false, deploy this module in the same Region.
  EOT

  validation {
    condition     = contains(["us-east-1", "eu-central-1"], var.kiro_profile_region)
    error_message = "kiro_profile_region must be us-east-1 or eu-central-1."
  }
}

variable "notification_email" {
  type        = string
  description = "Email address that receives the usage report and alert notifications."

  validation {
    condition     = can(regex("^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$", var.notification_email))
    error_message = "notification_email must be a valid email address."
  }
}

variable "notification_frequency" {
  type        = string
  default     = "7"
  description = "How often the email notification Lambda runs: \"1\" = daily, \"7\" = weekly (Mondays), \"30\" = monthly (1st). All runs fire at 8 AM UTC."

  validation {
    condition     = contains(["1", "7", "30"], var.notification_frequency)
    error_message = "notification_frequency must be \"1\", \"7\" or \"30\"."
  }
}

variable "permissions_boundary_arn" {
  type        = string
  description = "ARN of the IAM managed policy used as the permissions boundary on the Lambda execution role. Required; set to an empty string to create the role without a boundary."
  nullable    = false

  validation {
    condition     = can(regex("^$|^arn:aws[a-z-]*:iam::[0-9]{12}:policy/[A-Za-z0-9+=,.@_/-]+$", var.permissions_boundary_arn))
    error_message = "permissions_boundary_arn must be empty or a valid IAM managed policy ARN (arn:aws:iam::<account-id>:policy/<name>)."
  }
}

variable "enable_backfill" {
  type        = bool
  default     = true
  description = <<-EOT
    Run the one-time backfill after the first apply when use_existing_bucket is
    true. It seeds DynamoDB with the current month's data so the first report is
    correct. Equivalent to the CloudFormation BackfillCustomResource. Has no
    effect when use_existing_bucket is false.
  EOT
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Tags applied to all taggable resources."
}
