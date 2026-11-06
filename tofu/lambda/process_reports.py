import boto3
import csv
import os
from datetime import datetime, timedelta
from io import StringIO
from decimal import Decimal
from boto3.dynamodb.conditions import Key

s3 = boto3.client('s3')
sns = boto3.client('sns')
table = boto3.resource('dynamodb').Table(os.environ['TABLE_NAME'])

# Live Kiro pricing (see https://kiro.dev/pricing/)
TIER_LIMITS = {'Pro': 1000, 'Pro+': 2000, 'Pro Max': 5000, 'Power': 10000}
TIER_PRICE = {'Pro': 20, 'Pro+': 40, 'Pro Max': 100, 'Power': 200}
OVERAGE_RATE = 0.04
TIER_ORDER = ['Pro', 'Pro+', 'Pro Max', 'Power']
TIER_MAP = {
    'PRO': 'Pro', 'PRO+': 'Pro+', 'PROPLUS': 'Pro+',
    'PRO_PLUS': 'Pro+',
    'PRO MAX': 'Pro Max', 'PROMAX': 'Pro Max',
    'PRO_MAX': 'Pro Max',
    'POWER': 'Power',
}

def normalize_tier(raw):
    key = (raw or '').strip().upper()
    # Try exact match first, then collapsed spaces
    return TIER_MAP.get(key) or TIER_MAP.get(key.replace(' ', ''), 'Pro')

# Suffix on per-model message-count columns (e.g. "auto_messages",
# "claude_opus_5_messages"). The model name is the header prefix.
MODEL_COL_SUFFIX = '_messages'

def extract_models(row):
    """Return the set of model names the user used in this CSV row.

    Any column whose header ends in "_messages" is a per-model message
    count; the model name is the header prefix ("auto_messages" ->
    "auto", "claude_opus_5_messages" -> "claude_opus_5", which includes
    Kiro's auto model picker). A model is counted only when its cell
    parses to a number > 0, so models untouched that day are skipped.
    Blank / non-numeric cells are ignored.
    """
    models = set()
    for header, value in row.items():
        if not header or not header.endswith(MODEL_COL_SUFFIX):
            continue
        model = header[:-len(MODEL_COL_SUFFIX)]
        if not model:
            continue
        try:
            count = float(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            models.add(model)
    return models

def fmt_clients(client_types):
    """Comma-separated, sorted client types with the KIRO_ prefix
    stripped (e.g. {"KIRO_WEB","KIRO_CLI"} -> "CLI, WEB"). Empty -> "-"."""
    names = sorted(
        ct[len('KIRO_'):] if ct.startswith('KIRO_') else ct
        for ct in client_types
    )
    return ', '.join(names) if names else '-'

def fmt_models(models):
    """Comma-separated, sorted model names (empty -> "-")."""
    names = sorted(models)
    return ', '.join(names) if names else '-'

def fmt_usage_pct(raw_tier, credits):
    """MTD credits as a percentage of the tier's monthly allowance
    (e.g. Pro at 900/1000 -> "90.0%"). Can exceed 100% when overage
    is enabled. Returns "-" for an unrecognized tier rather than
    silently measuring against the Pro default used by normalize_tier.
    Display-only: does not feed the recommendation logic."""
    key = (raw_tier or '').strip().upper()
    tier = TIER_MAP.get(key) or TIER_MAP.get(key.replace(' ', ''))
    if tier is None:
        return '-'
    return f"{credits / TIER_LIMITS[tier] * 100:.1f}%"

def tier_cost(tier, credits):
    """Total monthly cost on a tier for the given usage: base + overage."""
    return TIER_PRICE[tier] + max(0, credits - TIER_LIMITS[tier]) * OVERAGE_RATE

def is_end_of_month_window(date):
    """Return True if date is within the last 5 days of its month."""
    import calendar
    _, days_in_month = calendar.monthrange(date.year, date.month)
    return date.day > (days_in_month - 5)

def fmt_month_elapsed(date):
    """How far through its month the data date is, e.g. "day 15 of 30,
    50.0% elapsed". Each daily report covers the whole data date, so
    day N counts as complete. Lets readers compare a user's Usage %
    against the share of the month that has passed."""
    import calendar
    _, days_in_month = calendar.monthrange(date.year, date.month)
    pct = date.day / days_in_month * 100
    return f"day {date.day} of {days_in_month}, {pct:.1f}% elapsed"

def upgrade_recommendation(tier, credits):
    """Recommend upgrading when a lower tier's overage makes a higher tier cheaper."""
    if tier not in TIER_ORDER:
        return None
    current_cost = tier_cost(tier, credits)
    higher = TIER_ORDER[TIER_ORDER.index(tier) + 1:]
    candidates = [(t, tier_cost(t, credits)) for t in higher]
    candidates = [(t, c) for t, c in candidates if c <= current_cost]
    if not candidates:
        return None
    best_tier, best_cost = min(candidates, key=lambda x: x[1])
    savings = current_cost - best_cost
    if savings < 0.005:
        return f"Upgrade to {best_tier} (same cost, more allowance)"
    return f"Upgrade to {best_tier} (saves ${savings:.2f}/mo)"

def downgrade_recommendation(tier, credits):
    """Recommend downgrading when a lower tier (even with overage) is cheaper.

    Only called during the end-of-month window (last 5 days) so MTD
    usage is a reliable proxy for the full month.
    """
    if tier not in TIER_ORDER:
        return None
    idx = TIER_ORDER.index(tier)
    if idx == 0:
        return None  # Already on the lowest paid tier
    current_cost = tier_cost(tier, credits)
    lower = TIER_ORDER[:idx]
    candidates = [(t, tier_cost(t, credits)) for t in lower]
    candidates = [(t, c) for t, c in candidates if c < current_cost]
    if not candidates:
        return None
    best_tier, best_cost = min(candidates, key=lambda x: x[1])
    savings = current_cost - best_cost
    if savings < 0.005:
        return None
    return f"Downgrade to {best_tier} next month (saves ${savings:.2f}/mo)"

def cost_recommendation(tier, credits, end_of_month):
    """Unified recommendation: upgrade, downgrade (if end-of-month), or None."""
    up = upgrade_recommendation(tier, credits)
    if up:
        return up
    if end_of_month:
        down = downgrade_recommendation(tier, credits)
        if down:
            return down
    return None

def send_missing_report_alert(bucket, prefix, sns_topic, console_url):
    """Send an SNS alert when no reports are found in S3."""
    message = (
        "ACTION REQUIRED: Kiro User Activity Reports Not Detected\n"
        + ("=" * 60) + "\n\n"
        "The Kiro Cost Optimizer Lambda ran but could not find any\n"
        "user activity reports in S3 for the expected date.\n\n"
        "Possible causes:\n"
        "  - The 'Kiro user activity reports' feature is not enabled\n"
        "  - The S3 bucket policy was removed or modified\n"
        "  - No Kiro users were active on the reporting day\n"
        "  - A Kiro service-side delivery issue\n\n"
        "To verify or enable the feature:\n"
        f"  1. Open: {console_url}\n"
        "  2. Under 'Kiro user activity reports', click Edit\n"
        "  3. Ensure 'Collect granular metrics per user' is toggled ON\n"
        f"  4. Confirm S3 URI is: s3://{bucket}/{prefix}/\n"
        "  5. Click Save\n\n"
        "Reports are delivered at 2:00 AM UTC daily. This alert will\n"
        "repeat every day until reports resume.\n"
    )
    sns.publish(
        TopicArn=sns_topic,
        Subject="ACTION REQUIRED: Kiro User Activity Reports Missing",
        Message=message,
    )

def list_report_keys(bucket, base_prefix):
    """Return all report CSV keys under base_prefix (handles pagination)."""
    keys = []
    token = None
    while True:
        kwargs = {'Bucket': bucket, 'Prefix': base_prefix}
        if token:
            kwargs['ContinuationToken'] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get('Contents', []):
            if obj['Key'].endswith('.csv'):
                keys.append(obj['Key'])
        if resp.get('IsTruncated'):
            token = resp.get('NextContinuationToken')
        else:
            break
    return keys

def report_date_from_key(key, base_prefix):
    """Extract the data date (.../YYYY/MM/DD/HH/file.csv) from a report key."""
    rest = key[len(base_prefix):].split('/')
    if len(rest) >= 3:
        try:
            return datetime.strptime('/'.join(rest[:3]), '%Y/%m/%d')
        except ValueError:
            return None
    return None

# Sort-key sentinel for the per-month user roster item. The roster
# lives in the SAME table under UserId="ROSTER#YYYY-MM", Date=ROSTER_SK.
# It records every user active anywhere in the month so the daily run
# can report month-to-date for ALL of them -- not just the users in the
# day being processed -- without scanning the whole table.
ROSTER_SK = "#ROSTER"

# Every item written to the table carries an ExpiresAt attribute so
# DynamoDB TTL auto-deletes it 90 days after it was written. 90 days
# outlives the longest read window (the current month) with margin,
# so no read ever loses data it still needs.
TTL_DAYS = 90

def ttl_90d():
    """Epoch-second TTL timestamp: now + 90 days (write-time based)."""
    return int((datetime.utcnow() + timedelta(days=TTL_DAYS)).timestamp())

def roster_pk(month):
    """Roster partition key for a month prefix ("YYYY-MM-")."""
    return f"ROSTER#{month[:-1]}"

def read_month_roster(month):
    """Return the set of user IDs recorded in this month's roster (or empty)."""
    items = table.query(
        KeyConditionExpression=(
            Key('UserId').eq(roster_pk(month)) & Key('Date').eq(ROSTER_SK)
        )
    )['Items']
    if not items:
        return set()
    return set(items[0].get('Users', []) or [])

def merge_month_roster(month, users):
    """Union `users` into this month's roster (read-modify-write) and
    return the full set. Skips the write when the union is empty
    (DynamoDB string sets cannot be empty). Safe under the single
    daily invocation (no concurrent writers)."""
    merged = read_month_roster(month) | set(users)
    if merged:
        table.put_item(Item={
            'UserId': roster_pk(month),
            'Date': ROSTER_SK,
            'Users': merged,
            'ExpiresAt': ttl_90d(),
        })
    return merged

def send_processing_error_alert(sns_topic, error):
    """Notify via SNS when report processing fails unexpectedly."""
    message = (
        "ERROR: Kiro Cost Optimizer report processing failed\n"
        + ("=" * 60) + "\n\n"
        "The processing Lambda raised an unexpected error while\n"
        "ingesting or processing the Kiro user activity report.\n\n"
        f"Error: {error}\n\n"
        "No report was archived or sent for this run. Check the\n"
        "CloudWatch Logs for the function for the full traceback.\n"
    )
    sns.publish(
        TopicArn=sns_topic,
        Subject="ERROR: Kiro Cost Optimizer processing failed",
        Message=message,
    )

def lambda_handler(event, context):
    # Top-level guard: report processing (ingest -> DynamoDB ->
    # accumulate -> archive) runs in _process. Any unexpected failure
    # is reported to SNS so it does not fail silently, then re-raised
    # so Lambda records the invocation as failed (metrics / retries).
    sns_topic = os.environ['SNS_TOPIC_ARN']
    try:
        return _process(event, context)
    except Exception as e:
        print(f"Processing failed: {e}")
        send_processing_error_alert(sns_topic, e)
        raise

def _process(event, context):
    bucket = os.environ['BUCKET_NAME']
    prefix = os.environ['REPORT_PREFIX']
    sns_topic = os.environ['SNS_TOPIC_ARN']
    region = os.environ['KIRO_REGION']
    console_url = os.environ['CONSOLE_SETTINGS_URL']

    # Target date: from event {"date": "YYYY-MM-DD"} for backfill, else yesterday
    if event and event.get('date'):
        target = datetime.strptime(event['date'], '%Y-%m-%d')
    else:
        target = datetime.utcnow() - timedelta(days=1)

    account_id = context.invoked_function_arn.split(':')[4]

    # ── REPORT DISCOVERY (with carry-forward) ──────────────────────
    # List every available report and map it to its data date. Kiro
    # delivers separate CSVs per client type (IDE, CLI) so we collect
    # ALL keys per date and merge them during ingestion.
    base_prefix = (
        f"{prefix}/AWSLogs/{account_id}/KiroLogs/"
        f"user_report/{region}/"
    )
    dated = {}
    for key in list_report_keys(bucket, base_prefix):
        d = report_date_from_key(key, base_prefix)
        if d is None:
            continue
        dated.setdefault(d, []).append(key)

    # Only truly missing data (feature never enabled, bucket policy
    # removed, etc.) should trigger the admin alert.
    target_day = datetime(target.year, target.month, target.day)
    candidates = [d for d in dated if d <= target_day]
    if not candidates:
        print(f"No reports on or before {target_day:%Y-%m-%d} - sending alert")
        send_missing_report_alert(bucket, prefix, sns_topic, console_url)
        return {
            'statusCode': 404,
            'body': 'No reports on or before target - alert sent',
            'report_missing': True,
        }

    # ── PROCESS REPORTS ────────────────────────────────────────────
    data_date = max(candidates)
    is_stale = data_date != target_day
    report_keys = dated[data_date]
    if is_stale:
        print(
            f"No report for {target_day:%Y-%m-%d}; carrying forward "
            f"{data_date:%Y-%m-%d}: {len(report_keys)} file(s)"
        )
    else:
        print(f"Processing {len(report_keys)} file(s) for {data_date:%Y-%m-%d}")

    # Read all CSVs for the date and accumulate per-user totals.
    # Multiple files (e.g. KIRO_CLI + KIRO_IDE) contain the same user
    # with usage split by client type; we sum credits across files
    # before writing a single consolidated row to DynamoDB.
    user_accum = {}  # user_id -> {fields}
    for report_key in report_keys:
        print(f"  Reading: s3://{bucket}/{report_key}")
        obj = s3.get_object(Bucket=bucket, Key=report_key)
        reader = csv.DictReader(StringIO(obj['Body'].read().decode('utf-8')))
        for row in reader:
            user_id = row.get('UserId', '')
            if not user_id:
                continue
            credits = Decimal(row.get('Credits_Used') or '0')
            overage = Decimal(row.get('Overage_Credits_Used') or '0')
            # Client type (col C) and per-model usage are unioned into
            # sets across every client-type CSV for this user/day.
            client_type = (row.get('Client_Type') or '').strip()
            models = extract_models(row)
            if user_id in user_accum:
                acc = user_accum[user_id]
                acc['Credits_Used'] += credits
                acc['Overage_Credits_Used'] += overage
                if client_type:
                    acc['ClientTypes'].add(client_type)
                acc['Models'] |= models
            else:
                user_accum[user_id] = {
                    'Date': row.get('Date', ''),
                    'User_Email': row.get('User_Email', ''),
                    'Subscription_Tier': row.get('Subscription_Tier', ''),
                    'Credits_Used': credits,
                    'Overage_Credits_Used': overage,
                    'ClientTypes': {client_type} if client_type else set(),
                    'Models': models,
                }

    user_ids = set()
    for user_id, fields in user_accum.items():
        user_ids.add(user_id)
        # DynamoDB string sets cannot be empty, so ClientTypes/Models
        # are only written when non-empty (same rule as the roster).
        item = {
            'UserId': user_id,
            'Date': fields['Date'],
            'User_Email': fields['User_Email'],
            'Subscription_Tier': fields['Subscription_Tier'],
            'Credits_Used': fields['Credits_Used'],
            'Overage_Credits_Used': fields['Overage_Credits_Used'],
        }
        if fields['ClientTypes']:
            item['ClientTypes'] = fields['ClientTypes']
        if fields['Models']:
            item['Models'] = fields['Models']
        item['ExpiresAt'] = ttl_90d()
        table.put_item(Item=item)

    # Accumulate per user for the data date's MONTH (resets on the 1st)
    month = f"{data_date:%Y-%m}-"
    end_of_month = is_end_of_month_window(data_date)

    # Record today's users in the month roster, then accumulate over
    # EVERY user active anywhere in the month so far. Iterating only the
    # users in the day being processed would drop everyone whose last
    # activity was on an earlier day -- so a day with a single active
    # user would render a single-user month-to-date report. The roster
    # is one small item per month (keyed by UserId="ROSTER#YYYY-MM"),
    # so this stays scoped to the current month with no full-table scan.
    month_users = merge_month_roster(month, user_ids)

    # Build a record per user (cells for rendering + sort metadata)
    TIER_RANK = {'Power': 0, 'Pro Max': 1, 'Pro+': 2, 'Pro': 3}
    records = []
    for user_id in month_users:
        items = table.query(
            KeyConditionExpression=(
                Key('UserId').eq(user_id) & Key('Date').begins_with(month)
            )
        )['Items']
        if not items:
            continue
        latest = items[-1]
        total_credits = float(sum(i['Credits_Used'] for i in items))
        # Union client types + models across every day in the month.
        client_types = set()
        models = set()
        for i in items:
            client_types |= set(i.get('ClientTypes') or [])
            models |= set(i.get('Models') or [])
        tier = normalize_tier(latest.get('Subscription_Tier', ''))
        # MTD overage is derived from MTD credits against the CURRENT
        # tier's allowance, not summed from the daily CSV
        # Overage_Credits_Used values. Per Kiro billing, a mid-month
        # upgrade is recalculated at cycle end under the new tier's
        # limit for the whole month, so overage recorded in earlier
        # CSVs (under the old tier) may never be billed.
        total_overage = max(0.0, total_credits - TIER_LIMITS[tier])
        rec = cost_recommendation(tier, total_credits, end_of_month)
        records.append({
            'cells': [
                latest.get('User_Email') or user_id,
                latest.get('Subscription_Tier', ''),
                fmt_clients(client_types),
                f"{total_credits:.2f}",
                fmt_usage_pct(latest.get('Subscription_Tier', ''), total_credits),
                f"{total_overage:.2f}",
                fmt_models(models),
                rec or '-',
            ],
            'tier': tier,
            'credits': total_credits,
            'has_rec': rec is not None,
        })

    # Within a section: group by tier (Power, Pro+, Pro), heaviest first
    def section_key(rec):
        return (TIER_RANK.get(rec['tier'], 99), -rec['credits'])

    cost_opt_recs = sorted(
        (r for r in records if r['has_rec']), key=section_key
    )
    other_users = sorted(
        (r for r in records if not r['has_rec']), key=section_key
    )

    # Compute total estimated savings across all recommendations
    total_savings = 0.0
    for r in cost_opt_recs:
        rec_text = r['cells'][-1]
        # Extract dollar amount from "... (saves $X.XX/mo)"
        if 'saves $' in rec_text:
            try:
                total_savings += float(rec_text.split('saves $')[1].split('/mo')[0])
            except (IndexError, ValueError):
                pass

    # ── RENDER PLAIN-TEXT REPORT ───────────────────────────────────
    headers = ['User', 'Tier', 'Clients', 'MTD Credits', 'Usage %', 'MTD Overage', 'Models', 'Recommendation']
    aligns = ['l', 'l', 'l', 'r', 'r', 'r', 'l', 'l']
    widths = [len(h) for h in headers]
    for r in records:
        for i, cell in enumerate(r['cells']):
            widths[i] = max(widths[i], len(str(cell)))

    def fmt_row(cells):
        parts = []
        for i, c in enumerate(cells):
            s = str(c)
            parts.append(
                s.rjust(widths[i]) if aligns[i] == 'r' else s.ljust(widths[i])
            )
        return ' | '.join(parts)

    separator = '-+-'.join('-' * w for w in widths)

    def render_section(title, recs):
        out = [title]
        if not recs:
            out.append("(none)")
            return out
        out.append(fmt_row(headers))
        out.append(separator)
        out += [fmt_row(r['cells']) for r in recs]
        return out

    data_note = (
        f"Data as of: {data_date:%Y-%m-%d} (UTC)"
        + (f"  (carried forward - no new report for {target_day:%Y-%m-%d})"
           if is_stale else "")
    )
    lines = [
        f"Kiro Usage Report - {target:%Y-%m-%d}",
        f"Month-to-date accumulation ({data_date:%Y-%m}; {fmt_month_elapsed(data_date)})",
        data_note,
        f"Total users: {len(records)}",
        f"Total estimated savings: ${total_savings:.2f}/mo",
        "",
    ]
    lines += render_section(
        f"=== Cost Optimization Recommendations ({len(cost_opt_recs)} user(s)) ===",
        cost_opt_recs,
    )
    lines.append("")
    lines += render_section(
        f"=== All Other Users ({len(other_users)} user(s)) ===",
        other_users,
    )
    report = "\n".join(lines) + "\n"

    # Archive the report to S3. SNS delivery is handled separately by
    # the EmailNotificationLambda, which reads this archived report
    # from S3 on its own schedule. This function only ingests,
    # processes, and archives. (The missing-report admin alert above
    # is the one exception that still notifies directly from here.)
    now = datetime.utcnow()
    report_key = (
        f"{prefix}/reports/{now:%Y}/{now:%m}/"
        f"usage-report-{now:%Y-%m-%d-%H%M%S}.txt"
    )
    s3.put_object(Bucket=bucket, Key=report_key, Body=report)
    print(f"Archived report: s3://{bucket}/{report_key}")

    return {
        'statusCode': 200,
        'body': f'Processed {len(user_ids)} users',
        'data_date': f'{data_date:%Y-%m-%d}',
        'carried_forward': is_stale,
    }
