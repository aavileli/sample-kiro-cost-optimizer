data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  account_id     = data.aws_caller_identity.current.account_id
  partition      = data.aws_partition.current.partition
  current_region = data.aws_region.current.region

  create_bucket = !var.use_existing_bucket
  bucket_name   = local.create_bucket ? aws_s3_bucket.reports[0].id : var.bucket_name
  bucket_arn    = "arn:${local.partition}:s3:::${local.bucket_name}"

  # "" -> no boundary.
  permissions_boundary = var.permissions_boundary_arn == "" ? null : var.permissions_boundary_arn

  console_settings_url = "https://${var.kiro_profile_region}.console.aws.amazon.com/amazonq/developer/home?region=${var.kiro_profile_region}#/settings"

  # Translates notification_frequency into an EventBridge cron (8 AM UTC).
  frequency_to_cron = {
    "1"  = "cron(0 8 * * ? *)" # every day
    "7"  = "cron(0 8 ? * 2 *)" # every Monday
    "30" = "cron(0 8 1 * ? *)" # 1st of every month
  }
  notification_cron = local.frequency_to_cron[var.notification_frequency]
}

# Input checks that depend on more than one variable (CloudFormation Rules equivalent).
resource "terraform_data" "input_checks" {
  lifecycle {
    precondition {
      condition     = !var.use_existing_bucket || var.bucket_name != ""
      error_message = "bucket_name is required when use_existing_bucket is true."
    }
    precondition {
      condition     = var.use_existing_bucket || var.kiro_profile_region == local.current_region
      error_message = "When use_existing_bucket is false, deploy in the same Region as your Kiro profile (kiro_profile_region). Kiro only delivers reports to a bucket in the profile's Region."
    }
  }
}

# ==========================================================================
# S3 BUCKET (only when use_existing_bucket = false)
# ==========================================================================
resource "aws_s3_bucket" "reports" {
  count = local.create_bucket ? 1 : 0

  bucket = var.bucket_name != "" ? var.bucket_name : null
  tags   = var.tags

  depends_on = [terraform_data.input_checks]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "reports" {
  count = local.create_bucket ? 1 : 0

  bucket = aws_s3_bucket.reports[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "reports" {
  count = local.create_bucket ? 1 : 0

  bucket                  = aws_s3_bucket.reports[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "reports" {
  count = local.create_bucket ? 1 : 0

  bucket = aws_s3_bucket.reports[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

# 90-day retention on this module's own .txt reports, matched to the DynamoDB
# TTL. The bucket is versioned, so noncurrent_version_expiration (1 day, not 90)
# is what actually deletes the bytes.
resource "aws_s3_bucket_lifecycle_configuration" "reports" {
  count = local.create_bucket ? 1 : 0

  bucket = aws_s3_bucket.reports[0].id

  rule {
    id     = "ExpireRenderedReports"
    status = "Enabled"

    filter {
      prefix = "${var.report_prefix}/reports/"
    }

    expiration {
      days = 90
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.reports]
}

data "aws_iam_policy_document" "bucket" {
  count = local.create_bucket ? 1 : 0

  statement {
    sid       = "KiroUserActivityReportWrite"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.reports[0].arn}/${var.report_prefix}/*"]

    principals {
      type        = "Service"
      identifiers = ["q.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:codewhisperer:${var.kiro_profile_region}:${local.account_id}:*"]
    }
  }
}

resource "aws_s3_bucket_policy" "reports" {
  count = local.create_bucket ? 1 : 0

  bucket = aws_s3_bucket.reports[0].id
  policy = data.aws_iam_policy_document.bucket[0].json

  depends_on = [aws_s3_bucket_public_access_block.reports]
}

# ==========================================================================
# DYNAMODB TABLE (daily per-user usage, accumulated across days)
# ==========================================================================
resource "aws_dynamodb_table" "usage" {
  name         = "${var.name}-usage-report"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "UserId"
  range_key    = "Date"
  tags         = var.tags

  attribute {
    name = "UserId"
    type = "S"
  }

  attribute {
    name = "Date"
    type = "S"
  }

  # Rows auto-expire 90 days after they are written (see the Lambda code).
  ttl {
    attribute_name = "ExpiresAt"
    enabled        = true
  }

  depends_on = [terraform_data.input_checks]
}

# ==========================================================================
# SNS NOTIFICATION
# ==========================================================================
resource "aws_sns_topic" "notifications" {
  name         = "${var.name}-usage-notifications"
  display_name = "Kiro Usage Notifications"
  tags         = var.tags
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.notifications.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

# ==========================================================================
# IAM ROLE (shared by all three Lambda functions)
# ==========================================================================
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name_prefix          = "${var.name}-lambda-"
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume.json
  permissions_boundary = local.permissions_boundary
  tags                 = var.tags
}

resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "s3" {
  name = "${var.name}-s3-read-write"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [local.bucket_arn, "${local.bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${local.bucket_arn}/${var.report_prefix}/reports/*"]
      },
    ]
  })
}

resource "aws_iam_role_policy" "sns" {
  name = "${var.name}-sns-publish"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sns:Publish"]
      Resource = aws_sns_topic.notifications.arn
    }]
  })
}

resource "aws_iam_role_policy" "dynamodb" {
  name = "${var.name}-dynamodb-rw"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:PutItem", "dynamodb:Query"]
      Resource = aws_dynamodb_table.usage.arn
    }]
  })
}

# ==========================================================================
# LAMBDA FUNCTIONS
# The handler code in lambda/ is copied verbatim from the inline ZipFile code in
# cfn/kiro-cost-optimizer.yaml. Keep the two in sync when editing either one.
# ==========================================================================
data "archive_file" "process_reports" {
  type        = "zip"
  output_path = "${path.module}/.build/process_reports.zip"

  source {
    content  = file("${path.module}/lambda/process_reports.py")
    filename = "index.py"
  }
}

data "archive_file" "email_notification" {
  type        = "zip"
  output_path = "${path.module}/.build/email_notification.zip"

  source {
    content  = file("${path.module}/lambda/email_notification.py")
    filename = "index.py"
  }
}

data "archive_file" "backfill" {
  type        = "zip"
  output_path = "${path.module}/.build/backfill.zip"

  source {
    content  = file("${path.module}/lambda/backfill.py")
    filename = "index.py"
  }

  # CloudFormation injects cfnresponse into inline functions; zipped functions
  # need it shipped alongside (see lambda/cfnresponse.py).
  source {
    content  = file("${path.module}/lambda/cfnresponse.py")
    filename = "cfnresponse.py"
  }
}

# ==========================================================================
# CLOUDWATCH LOG GROUPS (7-day retention, one per Lambda)
# Created explicitly so retention is enforced; Lambda would otherwise
# auto-create a log group that never expires.
# ==========================================================================
resource "aws_cloudwatch_log_group" "process_reports" {
  name              = "/aws/lambda/${var.name}-process-kiro-reports"
  retention_in_days = 7
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "email_notification" {
  name              = "/aws/lambda/${var.name}-process-kiro-notification"
  retention_in_days = 7
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "backfill" {
  name              = "/aws/lambda/${var.name}-process-kiro-backfill"
  retention_in_days = 7
  tags              = var.tags
}

resource "aws_lambda_function" "process_reports" {
  function_name    = "${var.name}-process-kiro-reports"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "index.lambda_handler"
  timeout          = 300
  memory_size      = 512
  filename         = data.archive_file.process_reports.output_path
  source_code_hash = data.archive_file.process_reports.output_base64sha256
  tags             = var.tags

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.process_reports.name
  }

  environment {
    variables = {
      BUCKET_NAME          = local.bucket_name
      REPORT_PREFIX        = var.report_prefix
      SNS_TOPIC_ARN        = aws_sns_topic.notifications.arn
      KIRO_REGION          = var.kiro_profile_region
      TABLE_NAME           = aws_dynamodb_table.usage.name
      CONSOLE_SETTINGS_URL = local.console_settings_url
    }
  }
}

resource "aws_lambda_function" "email_notification" {
  function_name    = "${var.name}-process-kiro-notification"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "index.lambda_handler"
  timeout          = 60
  memory_size      = 128
  filename         = data.archive_file.email_notification.output_path
  source_code_hash = data.archive_file.email_notification.output_base64sha256
  tags             = var.tags

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.email_notification.name
  }

  environment {
    variables = {
      BUCKET_NAME   = local.bucket_name
      REPORT_PREFIX = var.report_prefix
      SNS_TOPIC_ARN = aws_sns_topic.notifications.arn
    }
  }
}

resource "aws_lambda_function" "backfill" {
  function_name    = "${var.name}-process-kiro-backfill"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "index.lambda_handler"
  timeout          = 300
  memory_size      = 256
  filename         = data.archive_file.backfill.output_path
  source_code_hash = data.archive_file.backfill.output_base64sha256
  tags             = var.tags

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.backfill.name
  }

  environment {
    variables = {
      BUCKET_NAME   = local.bucket_name
      REPORT_PREFIX = var.report_prefix
      KIRO_REGION   = var.kiro_profile_region
      TABLE_NAME    = aws_dynamodb_table.usage.name
    }
  }
}

# ==========================================================================
# ONE-TIME INSTALL BACKFILL
# Seeds DynamoDB with the current month's daily rows when attaching to an
# EXISTING bucket mid-month. force=false keeps the Lambda's own skip logic
# (skip on the 1st of the month). Invoked once, on create of this resource.
# ==========================================================================
resource "aws_lambda_invocation" "backfill" {
  count = var.use_existing_bucket && var.enable_backfill ? 1 : 0

  function_name = aws_lambda_function.backfill.function_name

  input = jsonencode({
    UseExistingBucket = "true"
    force             = "false"
  })

  depends_on = [
    aws_iam_role_policy.s3,
    aws_iam_role_policy.dynamodb,
    aws_iam_role_policy_attachment.lambda_basic_execution,
  ]
}

# ==========================================================================
# EVENTBRIDGE SCHEDULES
# ==========================================================================
resource "aws_cloudwatch_event_rule" "daily_processing" {
  name                = "${var.name}-process-kiro-reports"
  description         = "Triggers Lambda to process Kiro reports daily at 3 AM UTC"
  schedule_expression = "cron(0 3 * * ? *)"
  state               = "ENABLED"
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "daily_processing" {
  rule      = aws_cloudwatch_event_rule.daily_processing.name
  target_id = "ProcessReportsLambdaTarget"
  arn       = aws_lambda_function.process_reports.arn
}

resource "aws_lambda_permission" "daily_processing" {
  statement_id  = "AllowEventBridgeDailyProcessing"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.process_reports.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily_processing.arn
}

resource "aws_cloudwatch_event_rule" "email_notification" {
  name                = "${var.name}-process-kiro-notification"
  description         = "Triggers the notification Lambda daily, weekly, or monthly based on notification_frequency."
  schedule_expression = local.notification_cron
  state               = "ENABLED"
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "email_notification" {
  rule      = aws_cloudwatch_event_rule.email_notification.name
  target_id = "EmailNotificationLambdaTarget"
  arn       = aws_lambda_function.email_notification.arn
}

resource "aws_lambda_permission" "email_notification" {
  statement_id  = "AllowEventBridgeEmailNotification"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.email_notification.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.email_notification.arn
}
