"""Record an explicit local operator allowance using the existing funding ledger.

This host-only command makes no provider request and imports no deposit receipt.
Run it for a fresh private development workspace after choosing your own API key
and provider billing limits. Agent execution remains separately configured.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'harness'))
import layout
layout.install()


def authorize(state: Path, amount_usd: str, days: int, *, acknowledged: bool):
    if not acknowledged:
        raise ValueError('Acknowledge that your OpenAI project is billed for agent execution.')
    state = Path(state).expanduser().absolute()
    if state != state.resolve() or state.is_relative_to(ROOT):
        raise ValueError('Choose a private state directory outside the source checkout.')
    amount = Decimal(amount_usd)
    if not amount.is_finite() or not 0 < amount <= 20 or amount.as_tuple().exponent < -2:
        raise ValueError('Choose a local allowance above zero and at most $20, in whole cents.')
    if type(days) is not int or not 1 <= days <= 30:
        raise ValueError('Choose an expiry from 1 to 30 days.')
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    if state.stat().st_uid != os.geteuid() or state.stat().st_mode & 0o077:
        raise ValueError('The state directory requires owner-only access (0700).')
    from company_usage import CompanyUsage
    from private_records import OPERATOR
    from openai_gateway import MODEL
    from workspace_identity import workspace_scope
    pool = CompanyUsage.native_agent_pool(state)
    records = state / 'operator-authorizations'
    records.mkdir(mode=0o700, exist_ok=True)
    if records.is_symlink() or records.stat().st_mode & 0o077:
        raise ValueError('Authorization records require a private directory (0700).')
    identity = 'local-' + uuid.uuid4().hex
    record = {
        'schema_version': 1, 'kind': 'operator_demo_budget', 'record_id': identity,
        'company_scope': pool.company_scope, 'parent_company_scope': workspace_scope(state),
        'environment_id': 'demo', 'provider': 'openai', 'verified_by': OPERATOR,
        'user_authorized': True,
        'authorization_reference': 'Local operator explicitly authorized this development allowance through the host CLI.',
        'purpose': 'native_agent_chat', 'allowed_model': MODEL,
        'amount_usd': str(amount),
        'expires_at': (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(),
    }
    destination = records / (identity + '.json')
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(json.dumps(record, indent=2) + '\n')
    result = pool.import_operator_budget(destination, lifecycle='development', environment_id='demo')
    return {'allowance_usd': result['operator_demo_budget_usd'],
            'available_usd': result['available_usd'], 'model': MODEL,
            'expires_at': record['expires_at'], 'provider_requests': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--amount-usd', required=True)
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--acknowledge-provider-billing', action='store_true')
    args = parser.parse_args()
    result = authorize(args.state, args.amount_usd, args.days,
                       acknowledged=args.acknowledge_provider_billing)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
