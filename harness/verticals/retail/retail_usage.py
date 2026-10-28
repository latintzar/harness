"""Retail video query allowance adapter for the shared company ledger."""
import hashlib
import json
from pathlib import Path

from compute_guard import OWNER, _read_private, validate_approval
from query_budget import PURPOSE, WORKSPACE, QueryBudget


class RetailQueryProvider:
    def read(self,approval_path):
        path=Path(approval_path).absolute();raw=_read_private(path);authority=json.loads(raw)
        if (authority.get('purpose')!=PURPOSE or authority.get('workspace')!=WORKSPACE
                or authority.get('verified_by')!=OWNER or authority.get('user_verified') is not True):
            raise ValueError('Use the owner-held Rocoto query authority.')
        budget=QueryBudget(path)
        ledger=budget._read_ledger() if budget.ledger_path.exists() else None
        if ledger and (ledger['authorization_sha256']!=hashlib.sha256(raw).hexdigest() or ledger['workspace']!=WORKSPACE):
            raise ValueError('Reconcile provider authority before company accounting.')
        return path,authority,ledger

    def validate(self,path):
        validate_approval(path,workspace=WORKSPACE,planned_gross_usd='0.000001',purpose=PURPOSE)
