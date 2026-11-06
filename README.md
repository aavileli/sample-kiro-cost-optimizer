# Kiro Cost Optimizer

Automated cost optimization for **Kiro Enterprise**. Ingests daily per-user activity reports (IDE + CLI + WEB), accumulates month-to-date credit usage per user, and emails tier upgrade/downgrade recommendations on a configurable daily, weekly, or monthly schedule.

# Authors
- Nikhil Arora
- Rob Yang

## Quick Start

### Prerequisites

- AWS CLI configured with appropriate credentials
- Kiro Enterprise account with user activity reports enabled
- Valid email address for notifications

### Deploy

```bash
aws cloudformation deploy \
  --region us-east-1 \
  --template-file cfn/kiro-cost-optimizer.yaml \
  --stack-name kiro-cost-optimizer \
  --s3-bucket <bucket-for-template-upload> \
  --parameter-overrides \
      UseExistingBucket=false \
      ReportPrefix=kiro-user-activity \
      KiroProfileRegion=us-east-1 \
      NotificationEmail=your-email@example.com \
      NotificationFrequency=1 \
      PermissionsBoundaryArn=arn:aws:iam::<account-id>:policy/SecOps-Boundary \
  --capabilities CAPABILITY_IAM
```

The template is larger than CloudFormation's 51,200-byte direct-upload limit, so `--s3-bucket` (any bucket you can write to in the same Region) is required. Lambda logs go to CloudWatch log groups created by the stack with 7-day retention.

`NotificationFrequency`: `1` = daily, `7` = weekly (Mondays), `30` = monthly (1st).

`KiroProfileRegion` must be the Region where your Kiro profile was installed (`us-east-1` or `eu-central-1`). With `UseExistingBucket=false`, deploy the stack in that same Region (`--region`), because the new bucket is created in the stack's Region and Kiro only delivers reports to a bucket in the profile's Region. A mismatched deploy fails with a `Parameter validation failed` error before any resources are created, leaving an empty stack in `ROLLBACK_COMPLETE`; delete it (`aws cloudformation delete-stack`) and redeploy in the right Region. With `UseExistingBucket=true`, the stack can run in any Region, but the existing bucket must be in the Kiro profile's Region.

`PermissionsBoundaryArn` is required and is set on the Lambda execution role. Pass `PermissionsBoundaryArn=""` to create the role without a boundary, or another policy ARN to use a different one. The role's permissions are the intersection of its policies and the boundary, so the boundary must allow the S3, SNS, DynamoDB and CloudWatch Logs actions the Lambdas use.

### Deploy with OpenTofu

The [`tofu/`](tofu/) directory is an OpenTofu module that deploys the same resources as the CloudFormation template.

```hcl
module "kiro_cost_optimizer" {
  source = "./tofu"

  notification_email     = "your-email@example.com"
  notification_frequency = "1"
  kiro_profile_region    = "us-east-1"
  permissions_boundary_arn = "arn:aws:iam::<account-id>:policy/SecOps-Boundary"
}
```

Variables mirror the CloudFormation parameters in snake_case (`use_existing_bucket`, `bucket_name`, `report_prefix`, `kiro_profile_region`, `notification_email`, `notification_frequency`, `permissions_boundary_arn`), and the outputs match the stack outputs. Requires OpenTofu 1.6+ and the AWS provider 6.0+. Run `tofu test` inside `tofu/` for the offline module tests.

The Lambda code in `tofu/lambda/` is a verbatim copy of the inline code in the CloudFormation template. Update both when changing handler logic. Instead of the CloudFormation custom resource, the module runs the backfill with `aws_lambda_invocation` (only when `use_existing_bucket = true`; disable with `enable_backfill = false`). Unlike the custom resource, a backfill error fails the apply.

### Enable Reports in Kiro Console

1. Get the S3 URI from stack outputs:
   ```bash
   aws cloudformation describe-stacks \
     --stack-name kiro-cost-optimizer \
     --query 'Stacks[0].Outputs[?OutputKey==`ReportS3Uri`].OutputValue' \
     --output text
   ```
2. Go to **[Kiro Console → Settings](https://us-east-1.console.aws.amazon.com/amazonq/developer/home?region=us-east-1#/settings)**.
3. Under **Kiro user activity reports**, click **Edit**, toggle **Collect granular metrics per user** ON, paste the S3 URI, and **Save**.

### Confirm Email Subscription

Click the SNS subscription confirmation link sent to your inbox. Reports begin arriving the next day at 2:00 AM UTC; the first optimization email follows on your chosen schedule.

## Overview

Kiro Enterprise delivers daily usage reports to S3 at 2:00 AM UTC, split by client type (`KIRO_IDE_*.csv`,`KIRO_WEB_*.csv`, `KIRO_CLI_*.csv`). At 3:00 AM UTC the `process-kiro-reports` Lambda:

- Merges all CSVs for the target date, summing `Credits_Used` and `Overage_Credits_Used` per user across client types and unioning the client types (`Client_Type`, e.g. `KIRO_WEB`, `KIRO_CLI`, `KIRO_IDE`) and models each user touched. Models are read from every per-model column (any header ending in `_messages`, e.g. `auto_messages`, `claude_opus_5_messages`); the model name is the header prefix and a model counts only when its message count is above zero. If no report exists for the date, the most recent prior report is carried forward (labelled accordingly).
- Persists one consolidated row per user per day to DynamoDB (idempotent) and accumulates per-user month-to-date totals across a per-month user roster, so users active earlier in the month still appear on days they had no usage. The roster resets on the 1st.
- Archives the rendered report to S3 (`<ReportPrefix>/reports/YYYY/MM/`) and alerts the admin via SNS if no report exists on or before the target date, or if processing fails.

A separate, independently scheduled `process-kiro-notification` Lambda reads the latest archived report from S3 and emails it on the `NotificationFrequency` schedule. A one-time `process-kiro-backfill` Lambda seeds the current month's prior days when installing against an existing bucket mid-month (see [Mid-month installs](#mid-month-installs-backfill)).

## Architecture

![Architecture Diagram](images/architecture.jpg)

## Report File Naming Convention

Kiro delivers reports with separate CSVs per client type:

```
{ReportPrefix}/AWSLogs/{AccountId}/KiroLogs/user_report/{Region}/YYYY/MM/DD/HH/KIRO_{ClientType}_{AccountId}_user_report_YYYYMMDDHHNN.csv
```

The Lambda collects all `.csv` files for a date and sums credits per user across client types before writing a single consolidated row to DynamoDB. Each row also stores the set of client types and models the user used that day (as DynamoDB string sets `ClientTypes` and `Models`), which are unioned across the month for the report. Processed reports are archived under `{ReportPrefix}/reports/YYYY/MM/usage-report-YYYY-MM-DD-HHMMSS.txt`.

## Tier Limits & Pricing Logic

| Tier | Monthly Credits | Base Price | Overage Rate |
|------|----------------|------------|--------------|
| Pro | 1,000 | $20/mo | $0.04/credit |
| Pro+ | 2,000 | $40/mo | $0.04/credit |
| Pro Max | 5,000 | $100/mo | $0.04/credit |
| Power | 10,000 | $200/mo | $0.04/credit |

Reference: [Kiro Enterprise Billing](https://kiro.dev/docs/cli/enterprise/billing/) (subject to change)

The `Subscription_Tier` value in the Kiro CSVs is mapped to these tiers (case-insensitive): `PRO`, `PRO+` / `PRO_PLUS` / `PROPLUS`, `PRO MAX` / `PRO_MAX` / `PROMAX`, and `POWER`. Kiro currently writes `PRO_MAX` and `PRO_PLUS` with underscores. A value outside this list is treated as Pro for the recommendation maths, and its Usage % shows `-`, so a `-` in that column for a user on a higher tier means the name needs adding to `TIER_MAP`.

The report produces a unified "Cost Optimization Recommendations" section with both upgrade and downgrade suggestions.

- Upgrades (always active): the Lambda computes each user's total monthly cost on their current tier (`base + overage × $0.04`) from MTD usage and recommends the cheapest higher tier whose total cost is at or below what they already pay. The recommendation either saves money or, at break-even, costs the same while granting more credits. It can skip tiers (e.g. a heavy Pro user straight to Pro Max).

- Downgrades (last 5 days of the month only): during the end-of-month window, the Lambda checks whether a user would be cheaper on a lower tier (accounting for overage there) and recommends the cheapest one. Downgrades say "next month" because, per [billing rules](https://kiro.dev/docs/cli/enterprise/billing/), they take effect the following month. The window is limited to the last 5 days because earlier in the month low usage is misleading.

## Deployment Options

### Using an existing bucket

Use the same [Deploy](#deploy) command, overriding these parameters:

```bash
      UseExistingBucket=true \
      BucketName=my-existing-kiro-reports-bucket \
      ReportPrefix=<the prefix Kiro already writes to> \
```

`ReportPrefix` must match the prefix configured in the Kiro console, because the Lambdas look for CSVs under `{ReportPrefix}/AWSLogs/<account-id>/KiroLogs/user_report/<region>/`. Deploy into the same AWS account as the Kiro profile. If the bucket is encrypted with SSE-KMS, the Lambda role also needs KMS permissions, which this stack does not grant. If the bucket is in another account, its bucket policy must allow the Lambda role. The stack writes only to `{ReportPrefix}/reports/` in the bucket.

After deployment, check the `RequiredBucketPolicyStatement` stack output for the policy statement to add to your bucket manually. The stack applies no lifecycle rule to an existing bucket; the `OptionalS3LifecyclePolicy` output supplies a ready-to-apply rule that expires this stack's reports after 90 days, scoped to `{ReportPrefix}/reports/` so your Kiro CSVs are untouched. Note that `put-bucket-lifecycle-configuration` replaces a bucket's entire lifecycle configuration — merge the rule into your existing rules rather than applying it standalone.

### Mid-month installs (backfill)

When installing against an existing bucket that already has reports and it's not the 1st, the `process-kiro-backfill` Lambda runs once during stack creation and seeds DynamoDB with every report from the 1st of the current month up to yesterday, so MTD totals are correct on the next 3:00 AM run.

- Runs once per stack create; idempotent `put_item`s.
- Skips when `UseExistingBucket=false` or when run on the 1st.
- Best-effort: errors are logged but never block or roll back the stack.

To (re)seed specific days manually, invoke the processing Lambda per date with `{"date": "YYYY-MM-DD"}` (see [Testing](#testing)).

To rerun the whole backfill (for example after changing the Lambda code) and regenerate the month-to-date report, invoke the backfill Lambda directly. A manual run is forced by default, bypassing the skip rules; pass `{"force": false}` to keep them. Then invoke the notification Lambda to email the new report.

```bash
aws lambda invoke --function-name kiro-cost-optimizer-process-kiro-backfill \
  --cli-binary-format raw-in-base64-out --payload '{}' backfill-out.json && cat backfill-out.json
aws lambda invoke --function-name kiro-cost-optimizer-process-kiro-notification \
  --cli-binary-format raw-in-base64-out notify-out.json && cat notify-out.json
```

With OpenTofu the backfill runs as `aws_lambda_invocation` when the resource is created, only if `use_existing_bucket = true` (set `enable_backfill = false` to skip it). It is not rerun on later applies unless the resource is replaced.

## Customization

All Lambda source code is embedded inline in `cfn/kiro-cost-optimizer.yaml` (under each function's `Code` → `ZipFile`). Edit the inline code and redeploy to change behavior. The OpenTofu module ships the same code as files in `tofu/lambda/`; copy any change there too (`process_reports.py`, `email_notification.py`, `backfill.py`). `tofu/lambda/cfnresponse.py` is a shim and is not part of the template.

Tier limits — edit the `TIER_LIMITS` dictionary in the inline Lambda code:

```python
TIER_LIMITS = {'Pro': 1000, 'Pro+': 2000, 'Pro Max': 5000, 'Power': 10000}
```

Tier names as they appear in the CSV are mapped by `TIER_MAP`, which exists in both the processing and backfill Lambdas. Update both.

CloudWatch Logs — each Lambda writes to its own log group (`/aws/lambda/<stack-name>-process-kiro-reports`, `-process-kiro-notification`, `-process-kiro-backfill`) with 7-day retention (`RetentionInDays` in the template, `retention_in_days` in the module). The log groups are deleted with the stack.

Permissions boundary — see `PermissionsBoundaryArn` (CloudFormation) / `permissions_boundary_arn` (OpenTofu).

Processing schedule — hardcoded to 3:00 AM UTC (`cron(0 3 * * ? *)`) in the `DailyProcessingRule` EventBridge rule, since Kiro delivers at a fixed 2:00 AM UTC. Edit its `ScheduleExpression` to change. The notification schedule is configurable at deploy time via `NotificationFrequency`.

## Testing

```bash
# Manual invocation
aws lambda invoke \
  --function-name kiro-cost-optimizer-process-kiro-reports \
  --payload '{}' response.json && cat response.json

# Process a specific day (backfill a date)
aws lambda invoke \
  --function-name kiro-cost-optimizer-process-kiro-reports \
  --cli-binary-format raw-in-base64-out \
  --payload '{"date": "YYYY-MM-DD"}' response.json && cat response.json

# Email the latest archived report now (instead of waiting for the schedule)
aws lambda invoke \
  --function-name kiro-cost-optimizer-process-kiro-notification \
  --cli-binary-format raw-in-base64-out notify-out.json && cat notify-out.json

# Logs (retained for 7 days)
aws logs tail /aws/lambda/kiro-cost-optimizer-process-kiro-reports --follow
```

Add `--profile <name> --region <region>` to the commands if your CLI needs them. The notification Lambda sends the newest `.txt` under `{ReportPrefix}/reports/`; it does not generate a new report. Run the processing or backfill Lambda first if you want a fresh one.

## Sample Output

The email is split into two sections: users with a recommendation first, then everyone else. Within each, users are grouped by tier (Power → Pro Max → Pro+ → Pro) and sorted by MTD credits descending, so the heaviest users stay on top.

The Usage % column is MTD credits divided by the user's tier allowance (e.g. 900 of Pro's 1,000 = `90.0%`). It can exceed 100% when overage is enabled, and shows `-` if the tier in the report isn't recognized. The header line shows how far through the month the data is (e.g. `day 15 of 30, 50.0% elapsed`), so you can compare the two: a user at 70% on day 15 is on pace to exceed their allowance. Both are display-only and do not affect recommendations. They are useful when overage is disabled, because the recommendations are cost-based and only suggest upgrades for users who are paying overage.

The Clients column lists the client types the user was active on (`KIRO_` prefix stripped: `WEB`, `CLI`, `IDE`); the Models column lists the models they used (including `auto`, Kiro's automatic model picker). Both are comma-separated within a single cell. Models sits second-to-last (just before Recommendation) since its length varies the most, keeping the numeric columns aligned on the left.

> Note: the email is plain text with fixed-width columns, so it renders best in a monospace font. Some mail clients (e.g. Outlook with a default proportional font) may misalign the columns; the archived S3 report is the same content and renders correctly in any monospace viewer.

```
Subject: Kiro Usage Report (2026-06-05) - 4 user(s)

Kiro Usage Report - 2026-06-05
Month-to-date accumulation (2026-06; day 5 of 30, 16.7% elapsed)
Data as of: 2026-06-05 (UTC)
Total users: 4
Total estimated savings: $80.00/mo

=== Cost Optimization Recommendations (2 user(s)) ===
User              | Tier  | Clients  | MTD Credits | Usage % | MTD Overage | Models                       | Recommendation
------------------+-------+----------+-------------+---------+-------------+------------------------------+-------------------------------------
bob@example.com   | PRO+  | CLI, WEB |     7000.00 |  350.0% |     5000.00 | auto, claude_sonnet_5        | Upgrade to Pro Max (saves $60.00/mo)
alice@example.com | PRO   | IDE      |     2100.00 |  210.0% |     1100.00 | auto, claude_opus_5          | Upgrade to Pro+ (saves $20.00/mo)

=== All Other Users (2 user(s)) ===
User              | Tier  | Clients  | MTD Credits | Usage % | MTD Overage | Models                       | Recommendation
------------------+-------+----------+-------------+---------+-------------+------------------------------+-------------------------------------
carol@example.com | POWER | IDE, WEB |     8200.00 |   82.0% |        0.00 | auto, claude_opus_5, gpt_5.6 | -
dave@example.com  | PRO   | CLI      |      450.00 |   45.0% |        0.00 | auto                         | -
```

Variants:
- **End-of-month (last 5 days):** downgrade recommendations also appear, e.g. `Downgrade to Pro next month (saves $148.00/mo)`.
- **Carried-forward (no report for the date):** the subject and header note the source date, e.g. `Data as of: 2026-06-05 (UTC) (carried forward - no new report for 2026-06-07)`.

> SNS email is plain text, so tables use fixed-width columns (best in a monospace font). The full report is also archived to S3. A 500-user report is ~50 KB, well within the SNS 256 KB limit.

## Cost Estimation

| Service | Monthly Cost |
|---------|-------------|
| S3 | ~$0.03 |
| Lambda | ~$0.01 |
| DynamoDB | ~$0.00 (on-demand, 90-day TTL) |
| SNS | ~$0.00 |
| EventBridge | Free |
| **Total** | **~$0.04** |

*Estimated based on US East (N. Virginia) pricing with daily report and no free tier.*

## Troubleshooting

**No reports found** — the Lambda carries forward the most recent prior report; it only alerts when no report exists on or before the target date. Common causes: feature not enabled, S3 bucket policy doesn't allow `q.amazonaws.com` to write, incorrect `ReportPrefix`, or first day after enabling (wait 24h).

**Lambda execution errors** — check CloudWatch Logs; verify S3 read permissions and that bucket name/prefix match parameters.

**Email not received** — Confirm the SNS subscription, check spam, review CloudWatch Logs for publish errors. The email is only sent on the `NotificationFrequency` schedule (for `30`, the 1st of the month at 8 AM UTC), so invoke the notification Lambda to get one sooner.

**Usage % shows `-` or recommendations look wrong** — the `Subscription_Tier` value in the CSV isn't in `TIER_MAP`, so the user is treated as Pro. Add the exact value to `TIER_MAP` in the processing and backfill Lambdas, redeploy, then rerun the backfill to regenerate the report (see [Mid-month installs](#mid-month-installs-backfill)).

**Logs missing** — CloudWatch Logs are kept for 7 days, so older invocations are gone by design.

**`Templates with a size greater than 51,200 bytes` on deploy** — the template exceeds the direct-upload limit. Add `--s3-bucket <bucket>` (and optionally `--s3-prefix`) to `aws cloudformation deploy`. The uploaded template object stays in that bucket until you delete it.

## Cleaning Up

```bash
aws cloudformation delete-stack --stack-name kiro-cost-optimizer
```

Removes the Lambdas, their CloudWatch log groups, the IAM role, EventBridge rules, SNS topic, DynamoDB table, and (if the stack created it) the S3 bucket. An existing bucket (`UseExistingBucket=true`) and its contents are retained, including the archived reports under `{ReportPrefix}/reports/` and any template objects uploaded with `--s3-bucket`. With OpenTofu, run `tofu destroy` instead.

Even without teardown, the DynamoDB table never grows unbounded: every row (per-user daily rows and the per-month user roster) is written with an `ExpiresAt` timestamp, and DynamoDB TTL auto-deletes each row 90 days after it was written. TTL deletes are performed by DynamoDB itself, so they incur no IAM permission or write cost, and are best-effort (typically within a few days of expiry). 90 days comfortably outlives the longest read window (the current month), so month-to-date reports are never affected. TTL deletion applies only to rows written after this feature was deployed; rows written earlier have no `ExpiresAt` and persist until removed manually or with the stack.

The archived reports are bounded to match: when the stack creates the bucket, an `ExpireRenderedReports` lifecycle rule deletes each `.txt` under `{ReportPrefix}/reports/` 90 days after it is written, including every nested `YYYY/MM` folder. Because the bucket is versioned, expiration first writes a delete marker and `NoncurrentVersionExpiration` reclaims the bytes a day later, after which S3 removes the delete marker automatically. The rule applies no storage-class transitions (the reports are below S3's 128 KB transition threshold) and never touches the Kiro-delivered CSVs under `{ReportPrefix}/AWSLogs/`, which remain the customer's to manage. For `UseExistingBucket=true` no rule is applied — see [Using an existing bucket](#using-an-existing-bucket).

## Security

- S3 encryption (AES256), public access blocked, versioning enabled for audit trail
- Lambda follows least-privilege IAM
- Lambda execution role carries the permissions boundary you supply (`PermissionsBoundaryArn` / `permissions_boundary_arn`)

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for reporting security issues.

## Project Structure

```
.
├── README.md
├── cfn/
│   └── kiro-cost-optimizer.yaml  # Complete stack; all Lambda code embedded inline
└── tofu/                         # OpenTofu module equivalent of the stack
    ├── main.tf, variables.tf, outputs.tf, versions.tf
    ├── lambda/                   # Lambda sources (copied from the CFN inline code)
    └── tests/                    # Offline `tofu test` suite
```


## References

- [User Activity Reports](https://kiro.dev/docs/cli/enterprise/monitor-and-track/user-activity/)
- [Enterprise Settings](https://kiro.dev/docs/enterprise/settings/)
- [Pricing](https://kiro.dev/pricing/)

## Contributing & License

See [CONTRIBUTING](CONTRIBUTING.md). Licensed under MIT-0 — see [LICENSE](LICENSE.txt).
