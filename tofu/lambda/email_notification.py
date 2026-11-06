import boto3
import os

s3 = boto3.client('s3')
sns = boto3.client('sns')

def find_latest_report(bucket, reports_prefix):
    """Return the key of the most recent archived report (.txt), or None.

    Archived reports are keyed as
      {REPORT_PREFIX}/reports/YYYY/MM/usage-report-YYYY-MM-DD-HHMMSS.txt
    which sorts lexicographically in chronological order, so the
    lexically-largest .txt key is the newest report.
    """
    latest = None
    token = None
    while True:
        kwargs = {'Bucket': bucket, 'Prefix': reports_prefix}
        if token:
            kwargs['ContinuationToken'] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get('Contents', []):
            key = obj['Key']
            if key.endswith('.txt') and (latest is None or key > latest):
                latest = key
        if resp.get('IsTruncated'):
            token = resp.get('NextContinuationToken')
        else:
            break
    return latest

def lambda_handler(event, context):
    bucket = os.environ['BUCKET_NAME']
    prefix = os.environ['REPORT_PREFIX']
    sns_topic = os.environ['SNS_TOPIC_ARN']

    # Reports are archived under {REPORT_PREFIX}/reports/YYYY/MM/.
    reports_prefix = f"{prefix}/reports/"
    latest_key = find_latest_report(bucket, reports_prefix)
    if latest_key is None:
        print(
            f"No archived reports found under "
            f"s3://{bucket}/{reports_prefix} - nothing to send"
        )
        return {'statusCode': 404, 'body': 'No archived report to send'}

    print(f"Publishing archived report: s3://{bucket}/{latest_key}")
    report = (
        s3.get_object(Bucket=bucket, Key=latest_key)['Body']
        .read().decode('utf-8')
    )

    # Use the report's first line as the email subject (SNS caps
    # subjects at 100 chars). Falls back to a generic subject.
    first_line = report.splitlines()[0] if report.strip() else "Kiro Usage Report"
    sns.publish(
        TopicArn=sns_topic,
        Subject=first_line[:100],
        Message=report,
    )
    return {'statusCode': 200, 'body': f'Sent {latest_key}'}
