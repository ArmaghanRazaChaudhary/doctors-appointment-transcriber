"""Compare local call estimates with a saved Costs API response; makes no API calls."""
import argparse
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from clinical_app.config import OUTPUT_DIR
from clinical_app.storage import save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--costs-json', type=Path, required=True,
                        help='Complete Costs API JSON response, grouped by project_id and line_item.')
    parser.add_argument('--date', required=True, help='Billing day in UTC, YYYY-MM-DD.')
    parser.add_argument('--project-id', required=True)
    args = parser.parse_args()
    try:
        start = datetime.strptime(args.date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        payload = json.loads(args.costs_json.read_text(encoding='utf-8-sig'))
        if payload.get('has_more'):
            parser.error('Costs response is paginated. Supply all pages merged into data with has_more=false.')
        buckets = [b for b in payload['data'] if b['start_time'] == int(start.timestamp())
                   and b['end_time'] == int(end.timestamp())]
        if not buckets:
            parser.error('No complete UTC-day bucket found for that date.')
        all_rows = [row for bucket in buckets for row in bucket['results']]
        if any(row.get('project_id') is None for row in all_rows):
            parser.error('Export Costs grouped by project_id; ungrouped totals cannot be scoped safely.')
        rows = [row for row in all_rows if row['project_id'] == args.project_id]
        if not rows:
            parser.error('No reported costs for this project/day; missing data is not zero spend.')
        if any(row.get('amount', {}).get('currency', '').lower() != 'usd' for row in rows):
            parser.error('Expected USD amounts.')
        reported = sum((Decimal(str(row['amount']['value'])) for row in rows), Decimal(0))
        estimates, unknown, calls = [], [], []
        for ledger in OUTPUT_DIR.glob('*/calls.json'):
            for call in json.loads(ledger.read_text(encoding='utf-8'))['calls']:
                when = datetime.fromisoformat(call['started_at'])
                if start <= when < end:
                    amount = (call.get('estimated_cost') or {}).get('amount')
                    item = {'run': ledger.parent.name, 'stage': call['stage'],
                            'request_id': call.get('request_id'), 'estimated_cost_usd': amount}
                    calls.append(item)
                    if amount is None:
                        unknown.append(item)
                    else:
                        estimates.append(Decimal(str(amount)))
        legacy = [p.parent.name for p in OUTPUT_DIR.glob('*/run.json')
                  if p.parent.name.startswith(start.strftime('%Y%m%d'))
                  and not (p.parent / 'calls.json').exists()]
        subtotal = sum(estimates, Decimal(0))
        report = {'date_utc': args.date, 'project_id': args.project_id,
                  'costs_source_file': str(args.costs_json.resolve()),
                  'reported_cost_usd': float(reported), 'reported_line_items': rows,
                  'local_known_estimated_subtotal_usd': float(subtotal),
                  'reported_minus_known_estimates_usd': float(reported - subtotal),
                  'calls': calls, 'calls_without_estimates': unknown, 'legacy_runs_without_call_logs': legacy,
                  'status': 'aggregate_comparison_requires_scope_review',
                  'limitations': ['Local call logs do not establish billing project identity.',
                                  'Other project activity, older runs, and reporting updates may affect totals.',
                                  'Daily costs cannot be assigned to individual request IDs.',
                                  'Calls crossing UTC midnight may be bucketed differently by the provider.']}
        destination = OUTPUT_DIR / f'reconciliation_{args.date}.json'
        save_json(destination, report)
        print(f'Reported project cost: ${reported:.6f}')
        print(f'Known local estimated subtotal: ${subtotal:.6f}')
        print(f'Difference (not a per-call charge): ${reported - subtotal:.6f}')
        print(f'Unknown estimates: {len(unknown)}; older runs without logs: {len(legacy)}')
        print(f'Saved aggregate comparison: {destination}')
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        parser.error('Cannot reconcile: check the Costs JSON and date format.')


if __name__ == '__main__':
    raise SystemExit(main())
