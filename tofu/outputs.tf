output "manual_step_console_url" {
  description = "REQUIRED MANUAL STEP - open this URL to enable Kiro user activity reports. Toggle ON 'Collect granular metrics per user' and paste report_s3_uri."
  value       = local.console_settings_url
}

output "bucket_name" {
  description = "S3 bucket for Kiro user activity reports."
  value       = local.bucket_name
}

output "report_s3_uri" {
  description = "Paste this S3 URI in the Kiro console settings under 'S3 location'."
  value       = "s3://${local.bucket_name}/${var.report_prefix}/"
}

output "required_bucket_policy_statement" {
  description = "When use_existing_bucket is true, add this statement to your bucket policy so Kiro can deliver reports. Null for a new bucket (the policy is managed for you)."
  value = var.use_existing_bucket ? jsonencode({
    Sid       = "KiroUserActivityReportWrite"
    Effect    = "Allow"
    Principal = { Service = "q.amazonaws.com" }
    Action    = "s3:PutObject"
    Resource  = "${local.bucket_arn}/${var.report_prefix}/*"
    Condition = {
      StringEquals = { "aws:SourceAccount" = local.account_id }
      ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:codewhisperer:${var.kiro_profile_region}:${local.account_id}:*" }
    }
  }) : null
}

output "optional_s3_lifecycle_policy" {
  description = "When use_existing_bucket is true, this module applies no lifecycle rule. Optionally apply this rule yourself for 90-day retention of the .txt reports. put-bucket-lifecycle-configuration REPLACES the whole configuration, so merge it into any existing rules. Null for a new bucket."
  value = var.use_existing_bucket ? jsonencode({
    Rules = [{
      ID                          = "ExpireRenderedReports"
      Status                      = "Enabled"
      Filter                      = { Prefix = "${var.report_prefix}/reports/" }
      Expiration                  = { Days = 90 }
      NoncurrentVersionExpiration = { NoncurrentDays = 1 }
    }]
  }) : null
}

output "lambda_function_arn" {
  description = "ARN of the Lambda processor function."
  value       = aws_lambda_function.process_reports.arn
}

output "email_notification_lambda_arn" {
  description = "ARN of the scheduled email notification Lambda."
  value       = aws_lambda_function.email_notification.arn
}

output "backfill_lambda_arn" {
  description = "ARN of the one-time install backfill Lambda."
  value       = aws_lambda_function.backfill.arn
}

output "email_notification_schedule" {
  description = "Resolved cron expression for the email notification Lambda."
  value       = local.notification_cron
}

output "sns_topic_arn" {
  description = "ARN of the SNS notification topic."
  value       = aws_sns_topic.notifications.arn
}

output "usage_table_name" {
  description = "DynamoDB table storing daily per-user usage."
  value       = aws_dynamodb_table.usage.name
}

output "lambda_role_arn" {
  description = "ARN of the Lambda execution role."
  value       = aws_iam_role.lambda.arn
}
