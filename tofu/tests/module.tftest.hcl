# Offline tests using mocked providers (no AWS credentials needed).
# Run with: tofu init -backend=false && tofu test

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:t" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::mock-bucket" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:dynamodb:us-east-1:123456789012:table/mock" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:us-east-1:123456789012:function:mock" }
  }
  mock_resource "aws_cloudwatch_event_rule" {
    defaults = { arn = "arn:aws:events:us-east-1:123456789012:rule/mock" }
  }
}

mock_provider "archive" {}

variables {
  notification_email       = "admin@example.com"
  permissions_boundary_arn = "arn:aws:iam::123456789012:policy/SecOps-Boundary"
}

run "applies_permissions_boundary" {
  command = plan

  assert {
    condition     = aws_iam_role.lambda.permissions_boundary == "arn:aws:iam::123456789012:policy/SecOps-Boundary"
    error_message = "Lambda role must use the supplied permissions boundary."
  }
}

run "empty_boundary_disables_it" {
  command = plan

  variables {
    permissions_boundary_arn = ""
  }

  assert {
    condition     = aws_iam_role.lambda.permissions_boundary == null
    error_message = "An empty permissions_boundary_arn must create the role without a boundary."
  }
}

run "rejects_malformed_boundary" {
  command = plan

  variables {
    permissions_boundary_arn = "not-an-arn"
  }

  expect_failures = [var.permissions_boundary_arn]
}

run "existing_bucket_requires_name" {
  command = plan

  variables {
    use_existing_bucket = true
  }

  expect_failures = [terraform_data.input_checks]
}

run "new_bucket_in_wrong_region_rejected" {
  command = plan

  variables {
    kiro_profile_region = "eu-central-1"
  }

  expect_failures = [terraform_data.input_checks]
}

run "existing_bucket_creates_no_bucket_and_backfills" {
  command = plan

  variables {
    use_existing_bucket = true
    bucket_name         = "my-existing-bucket"
    kiro_profile_region = "eu-central-1"
  }

  assert {
    condition     = length(aws_s3_bucket.reports) == 0 && length(aws_lambda_invocation.backfill) == 1
    error_message = "Existing bucket: no bucket created, backfill invoked."
  }
}

run "log_groups_expire_after_7_days" {
  command = plan

  assert {
    condition = alltrue([
      aws_cloudwatch_log_group.process_reports.retention_in_days == 7,
      aws_cloudwatch_log_group.email_notification.retention_in_days == 7,
      aws_cloudwatch_log_group.backfill.retention_in_days == 7,
    ])
    error_message = "All Lambda log groups must retain logs for 7 days."
  }
}
