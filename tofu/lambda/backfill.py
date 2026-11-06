import boto3
import csv
import os
from datetime import datetime, timedelta
from io import StringIO
from decimal import Decimal
from boto3.dynamodb.conditions import Key
import cfnresponse

s3 = boto3.client('s3')
table = boto3.resource('dynamodb').Table(os.environ['TABLE_NAME'])

# Live Kiro pricing (see https://kiro.dev/pricing/). Mirrors the
# process-kiro-reports Lambda so the install-time report is identical
# in format and recommendations to the daily one.
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

# Sort-key sentinel for the per-month user roster item (must match the
# process-kiro-reports Lambda). The roster lives in the SAME table under
# UserId="ROSTER#YYYY-MM", Date=ROSTER_SK, and records every user active
# in the month so the daily run can report month-to-date for ALL of
# them. Backfill writes it so the first scheduled run after a mid-month
# install starts from the complete user set instead of an empty roster.
ROSTER_SK = "#ROSTER"

# Every item written to the table carries an ExpiresAt attribute so
# DynamoDB TTL auto-deletes it 90 days after it was written (must
# match the process-kiro-reports Lambda). 90 days outlives the
# longest read window (the current month) with margin.
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
    (DynamoDB string sets cannot be empty)."""
    merged = read_month_roster(month) | set(users)
    if merged:
        table.put_item(Item={
            'UserId': roster_pk(month),
            'Date': ROSTER_SK,
            'Users': merged,
            'ExpiresAt': ttl_90d(),
        })
    return merged

def ingest_keys(bucket, keys):
    """Read multiple report CSVs for a single date and write consolidated
    per-user rows to DynamoDB. Kiro delivers separate CSVs per client
    type (IDE, CLI); this merges them by summing credits per user.

    Idempotent: put_item overwrites on (UserId, Date). Uses put_item
    (granted by the shared role) rather than batch_writer/BatchWriteItem.
    """
    user_accum = {}
    for key in keys:
        print(f"  Reading: s3://{bucket}/{key}")
        obj = s3.get_object(Bucket=bucket, Key=key)
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
    rows = 0
    users = set()
    for user_id, fields in user_accum.items():
        users.add(user_id)
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
        rows += 1
    return rows, users

def normalize_tier(raw):
    key = (raw or '').strip().upper()
    return TIER_MAP.get(key) or TIER_MAP.get(key.replace(' ', ''), 'Pro')

# Suffix on per-model message-count columns (must match the
# process-kiro-reports Lambda). The model name is the header prefix.
MODEL_COL_SUFFIX = '_messages'

def extract_models(row):
    """Return the set of model names the user used in this CSV row.

    Any column whose header ends in "_messages" is a per-model message
    count; the model name is the header prefix ("auto_messages" ->
    "auto", which includes Kiro's auto model picker). A model is counted
    only when its cell parses to a number > 0. Blank / non-numeric cells
    are ignored.
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
    (must match the process-kiro-reports Lambda). Can exceed 100% when
    overage is enabled; "-" for an unrecognized tier. Display-only."""
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

def build_and_archive_report(bucket, prefix, data_date, user_ids):
    """Accumulate MTD totals from DynamoDB and archive a .txt report to S3.

    Mirrors the process-kiro-reports Lambda's accumulate -> render ->
    archive flow. Reports month-to-date totals (scoped to data_date's
    calendar month) for EVERY user seeded during the backfill -- i.e.
    everyone active anywhere in the month so far, not just the most
    recent day -- so the install report reflects the whole month rather
    than a single day's snapshot. Returns the archived S3 key.
    """
    month = f"{data_date:%Y-%m}-"
    end_of_month = is_end_of_month_window(data_date)
    TIER_RANK = {'Power': 0, 'Pro Max': 1, 'Pro+': 2, 'Pro': 3}
    records = []
    for user_id in user_ids:
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

    def section_key(rec):
        return (TIER_RANK.get(rec['tier'], 99), -rec['credits'])

    cost_opt_recs = sorted((r for r in records if r['has_rec']), key=section_key)
    other_users = sorted((r for r in records if not r['has_rec']), key=section_key)

    total_savings = 0.0
    for r in cost_opt_recs:
        rec_text = r['cells'][-1]
        if 'saves $' in rec_text:
            try:
                total_savings += float(rec_text.split('saves $')[1].split('/mo')[0])
            except (IndexError, ValueError):
                pass

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

    lines = [
        f"Kiro Usage Report - {data_date:%Y-%m-%d}",
        f"Month-to-date accumulation ({data_date:%Y-%m}; {fmt_month_elapsed(data_date)})",
        f"Data as of: {data_date:%Y-%m-%d} (UTC)  (install backfill)",
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

    now = datetime.utcnow()
    report_key = (
        f"{prefix}/reports/{now:%Y}/{now:%m}/"
        f"usage-report-{now:%Y-%m-%d-%H%M%S}.txt"
    )
    s3.put_object(Bucket=bucket, Key=report_key, Body=report)
    print(f"Archived install report: s3://{bucket}/{report_key}")
    return report_key

def do_backfill(context):
    """Seed every available report from the 1st of this month to yesterday."""
    bucket = os.environ['BUCKET_NAME']
    prefix = os.environ['REPORT_PREFIX']
    region = os.environ['KIRO_REGION']
    account_id = context.invoked_function_arn.split(':')[4]

    today = datetime.utcnow()
    first_of_month = datetime(today.year, today.month, 1)
    yesterday = datetime(today.year, today.month, today.day) - timedelta(days=1)

    base_prefix = (
        f"{prefix}/AWSLogs/{account_id}/KiroLogs/"
        f"user_report/{region}/"
    )
    # Map data date -> list of keys, restricted to this month, <= yesterday.
    # Kiro delivers separate CSVs per client type (IDE, CLI) so we
    # collect ALL keys per date and merge them during ingestion.
    dated = {}
    for key in list_report_keys(bucket, base_prefix):
        d = report_date_from_key(key, base_prefix)
        if d is None:
            continue
        if first_of_month <= d <= yesterday:
            dated.setdefault(d, []).append(key)

    days = sorted(dated)
    rows = 0
    all_users = set()
    for d in days:
        print(f"Backfilling {d:%Y-%m-%d}: {len(dated[d])} file(s)")
        n, users = ingest_keys(bucket, dated[d])
        rows += n
        all_users |= users

    # After seeding DynamoDB, produce an immediate month-to-date report
    # (same format as the daily process Lambda) and archive it to S3,
    # so the admin gets a report right after install instead of waiting
    # for the notification schedule. The report accumulates the WHOLE
    # month for every user seeded above (not just the most recent day),
    # using the latest seeded data date as the "as of" date.
    report_key = None
    if all_users:
        data_date = max(days)
        # Mirror the daily Lambda: record every seeded user in this
        # month's roster so the first scheduled process run reports
        # month-to-date for ALL of them, instead of only the next day's
        # active users (which would otherwise re-trigger the original
        # "single-user report" bug right after a mid-month install).
        merge_month_roster(f"{data_date:%Y-%m}-", all_users)
        report_key = build_and_archive_report(
            bucket, prefix, data_date, all_users
        )
    return len(days), rows, report_key

def _skip_reason(props):
    """Return a reason string to skip the backfill, or None to proceed."""
    if str(props.get('UseExistingBucket', 'false')).lower() != 'true':
        return "UseExistingBucket != true: new bucket has no history"
    if datetime.utcnow().day == 1:
        return "1st of month: nothing delivered yet"
    return None

def _run_backfill(context):
    days, rows, report_key = do_backfill(context)
    result = {'days_seeded': days, 'rows_written': rows}
    if report_key:
        result['report_key'] = report_key
    print(f"Backfill complete: {result}")
    return result

def lambda_handler(event, context):
    # Dual-mode entry point:
    #  * CloudFormation custom resource: the event carries 'ResponseURL'.
    #    Runs best-effort (never rolls back the stack) and signals CFN.
    #  * Standalone / manual invocation (console "Test", CLI invoke, etc.):
    #    no 'ResponseURL'. Runs the backfill directly and RETURNS the
    #    result without calling cfnresponse (which is what caused the
    #    KeyError: 'ResponseURL'). By default a manual run is forced
    #    (bypassing the UseExistingBucket / 1st-of-month skips). Pass
    #    {"force": false} to exercise the normal skip logic, and
    #    {"UseExistingBucket": "true"} to set the property.
    if 'ResponseURL' not in event:
        props = event.get('ResourceProperties') or {
            'UseExistingBucket': event.get('UseExistingBucket', 'true')
        }
        force = str(event.get('force', 'true')).lower() == 'true'
        skip = None if force else _skip_reason(props)
        if skip:
            print(f"Skipping backfill: {skip}")
            return {'skipped': skip}
        return _run_backfill(context)

    # CloudFormation custom resource path (best-effort, Create-only).
    request_type = event.get('RequestType')
    props = event.get('ResourceProperties', {})
    result = {}
    try:
        if request_type in ('Delete', 'Update'):
            print(f"RequestType={request_type}: no-op (Create-only)")
        else:
            skip = _skip_reason(props)
            if skip:
                print(f"Skipping backfill: {skip}")
            else:
                result = _run_backfill(context)
    except Exception as e:
        # Best-effort: log loudly but do not fail/roll back the stack.
        print(f"Backfill error (non-fatal): {e}")
        result = {'error': str(e)}
    cfnresponse.send(event, context, cfnresponse.SUCCESS, result)
