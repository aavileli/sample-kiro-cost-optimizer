# Kiro Cost Optimizer - OpenTofu module

OpenTofu equivalent of `../cfn/kiro-cost-optimizer.yaml`. See the [top-level README](../README.md) for how the system works.

## Usage

```hcl
module "kiro_cost_optimizer" {
  source = "./tofu"

  use_existing_bucket    = true
  bucket_name            = "my-existing-kiro-reports-bucket"
  report_prefix          = "kiro-usage"
  kiro_profile_region    = "us-east-1"
  notification_email     = "you@example.com"
  notification_frequency = "30"

  permissions_boundary_arn = "arn:aws:iam::<account-id>:policy/SecOps-Boundary"
}
```

Requires OpenTofu 1.6+, AWS provider 6.0+ and the archive provider.

## Inputs

| Name | Default | Description |
|---|---|---|
| `notification_email` | required | Receives reports and alerts. Confirm the SNS subscription email. |
| `use_existing_bucket` | `false` | `true` to read an existing bucket. The module then changes nothing on the bucket. |
| `bucket_name` | `""` | Required when `use_existing_bucket` is true. |
| `report_prefix` | `kiro-user-activity` | Must match the prefix Kiro writes to. |
| `kiro_profile_region` | `us-east-1` | `us-east-1` or `eu-central-1`. A new bucket must be deployed in this Region. |
| `notification_frequency` | `"7"` | `"1"` daily, `"7"` weekly (Mon), `"30"` monthly (1st), 8 AM UTC. |
| `permissions_boundary_arn` | required | Boundary on the Lambda role. `""` disables it. |
| `enable_backfill` | `true` | Run the one-time backfill when `use_existing_bucket` is true. |
| `name` | `kiro-cost-optimizer` | Prefix for all resource names (the stack name equivalent). |
| `tags` | `{}` | Tags for all taggable resources. |

## Behaviour that differs from CloudFormation

- The backfill runs through `aws_lambda_invocation`. A backfill error fails the apply (the CloudFormation custom resource never fails the stack). Rerun it manually with the commands in the top-level README.
- The S3 bucket created for `use_existing_bucket = false` has no `DeletionPolicy: Retain` equivalent; `tofu destroy` fails on it while it still holds objects, because `force_destroy` is not set.
- Lambda log groups keep logs for 7 days.

## Lambda code

`lambda/*.py` is copied verbatim from the inline `ZipFile` code in the CloudFormation template. Keep the two in sync. `lambda/cfnresponse.py` is a shim for the import in `backfill.py` and is never called.

## Tests

```bash
tofu init -backend=false
tofu test   # offline, uses mocked AWS providers
```
