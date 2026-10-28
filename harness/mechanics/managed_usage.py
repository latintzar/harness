"""Agents API allowances inside the workspace's existing immutable money ledger.

Provider spend is cumulative and approximate. A higher limit reserves only its
increment; terminal estimates never release or settle money. Host reconciliation
requires a final receipt for the closed session, including every provider cost.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from company_usage import _read_private, _money, _usd, OWNER


def identity(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.:@/-]{1,256}',value):
        raise ValueError('Use the saved managed accounting identity.')
    return value


class ManagedUsage:
    def __init__(self,pool):
        self.pool=pool
        with pool.connect() as db:
            pool._openai_binding(db)
            db.executescript('''
                CREATE TABLE IF NOT EXISTS managed_budgets (
                    binding_id TEXT PRIMARY KEY, session_id TEXT UNIQUE,
                    model TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS managed_allowances (
                    request_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL,
                    actor TEXT NOT NULL, limit_cents INTEGER NOT NULL,
                    reservation_id TEXT UNIQUE, status TEXT NOT NULL,
                    consumed_cents INTEGER, terminal TEXT);
                CREATE TABLE IF NOT EXISTS managed_role_plans (
                    parent_request TEXT PRIMARY KEY, plan TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS managed_role_slots (
                    parent_request TEXT NOT NULL, role_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    model TEXT NOT NULL, limit_cents INTEGER NOT NULL, reservation_id TEXT UNIQUE NOT NULL,
                    state TEXT NOT NULL, child_binding TEXT, child_request TEXT,
                    PRIMARY KEY(parent_request, role_id, ordinal));
                CREATE TABLE IF NOT EXISTS managed_monitors (
                    request_id TEXT PRIMARY KEY, receipt TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS managed_final_receipts (
                    binding_id TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL,
                    digest TEXT NOT NULL);
            ''')

    def _request(self,db,request_id):
        row=db.execute('''SELECT a.*,b.session_id,b.model,b.closed FROM managed_allowances a
            JOIN managed_budgets b USING(binding_id) WHERE request_id=?''',(request_id,)).fetchone()
        return dict(row) if row else None

    def request(self,request_id):
        with self.pool.connect() as db:return self._request(db,identity(request_id))

    def monitor(self, request_id, initial=None):
        """Persist the original target/deadline beside its existing funding hold."""
        with self.pool.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._request(db, identity(request_id)) is None:
                raise ValueError('Bind monitoring to an existing funded request.')
            row = db.execute('SELECT receipt FROM managed_monitors WHERE request_id=?', (request_id,)).fetchone()
            if row is not None:
                return json.loads(row[0])
            if initial is None:
                return None
            encoded = json.dumps(initial, sort_keys=True, allow_nan=False)
            db.execute('INSERT INTO managed_monitors VALUES (?,?)', (request_id, encoded))
            return json.loads(encoded)

    def save_monitor(self, request_id, receipt):
        with self.pool.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT receipt FROM managed_monitors WHERE request_id=?', (identity(request_id),)).fetchone()
            if row is None:
                raise ValueError('Update the original funded spending monitor.')
            prior = json.loads(row[0])
            if any(prior.get(key) != receipt.get(key) for key in ('mode', 'target_cents', 'deadline_at', 'model', 'baseline')):
                raise ValueError('Keep the original monitored target, deadline and baseline.')
            value = dict(receipt)
            value['observed_cents'] = max(prior['observed_cents'], value['observed_cents'])
            value['stop_reason'] = prior.get('stop_reason') or value.get('stop_reason')
            db.execute('UPDATE managed_monitors SET receipt=? WHERE request_id=?',
                       (json.dumps(value, sort_keys=True, allow_nan=False), request_id))

    def capacity(self,binding_id,session_id):
        with self.pool.connect() as db:
            row=db.execute('SELECT * FROM managed_budgets WHERE binding_id=?',(identity(binding_id),)).fetchone()
            if row is None or row['session_id']!=session_id or row['closed']:
                raise ValueError('Read the original funded managed session capacity.')
            return db.execute('SELECT COALESCE(MAX(limit_cents),0) FROM managed_allowances WHERE binding_id=?',
                              (binding_id,)).fetchone()[0]

    def reserve(self, **arguments):
        with self.pool.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._reserve(db, **arguments)

    def _reserve(self,db,*,binding_id,request_id,actor,model,limit_cents,session_id=None,recovering=False):
        for value in (binding_id,request_id,actor,model):identity(value)
        if session_id is not None:identity(session_id)
        if type(limit_cents) is not int or not 0<limit_cents<=100_000_000:
            raise ValueError('Use a finite funded session allowance.')
        pool=self.pool
        pool._openai_binding(db)
        summary=pool._summary(db)
        if summary['funding_kind']=='operator_demo_budget':
            authorization=pool._operator_authorization(db)
            if (summary['operator_purpose'] not in {'native_agent_chat','managed_agent_chat'}
                    or summary['operator_model']!=model or not authorization
                    or authorization['data']['expires_at']<=pool._now()):
                raise ValueError('Use an active operator allocation authorized for this agent model.')
        prior=self._request(db,request_id)
        if prior:
            if (prior['binding_id'],prior['actor'],prior['model'],prior['limit_cents'],prior['session_id'])!=(
                    binding_id,actor,model,limit_cents,session_id) or prior['closed']:
                raise ValueError('Keep the request’s original session, actor and monetary allowance.')
            if recovering and prior['status']=='terminal':
                raise ValueError('Resume an unfinished managed request.')
            return prior
        if recovering:raise ValueError('Recover the existing funded request before admitting new work.')
        budget=db.execute('SELECT * FROM managed_budgets WHERE binding_id=?',(binding_id,)).fetchone()
        if budget:
            if budget['session_id']!=session_id or budget['model']!=model or budget['closed']:
                raise ValueError('Use the owning active managed budget.')
        elif session_id is not None:
            raise ValueError('Bind a provider session to its original funded creation.')
        else:db.execute('INSERT INTO managed_budgets(binding_id,model) VALUES(?,?)',(binding_id,model))
        prior_limit=db.execute('SELECT COALESCE(MAX(limit_cents),0) FROM managed_allowances WHERE binding_id=?',
                               (binding_id,)).fetchone()[0]
        increase=max(0,limit_cents-prior_limit)
        reservation_id=None
        if increase:
            record=pool._reserve_in_transaction(db,increase*10000,'managed:'+hashlib.sha256(request_id.encode()).hexdigest())
            reservation_id=record['reservation_id']
            # Durable admission precedes any network write. A crash here keeps
            # the allowance held and a retry uses the same request identity.
            pool._append(db,'start:'+reservation_id,'started',reservation_id=reservation_id,
                         provider='openai_agents',binding_id=binding_id,request_id=request_id)
        db.execute('INSERT INTO managed_allowances VALUES(?,?,?,?,?,?,NULL,NULL)',
                   (request_id,binding_id,actor,limit_cents,reservation_id,'admitted'))
        return self._request(db,request_id)

    def role_plan(self, parent_request):
        with self.pool.connect() as db:
            row = db.execute('SELECT plan FROM managed_role_plans WHERE parent_request=?',
                             (identity(parent_request),)).fetchone()
            return json.loads(row[0]) if row else None

    def reserve_parent(self, *, roles, **arguments):
        if not isinstance(roles, list) or len(roles) > 12:
            raise ValueError('Preallocate a bounded declared role plan.')
        total, names = 0, set()
        for role in roles:
            identity(role['id']); identity(role['model'])
            if (role['id'] in names or type(role['limit_cents']) is not int
                    or not 1 <= role['limit_cents'] <= 40 or type(role['per_turn']) is not int
                    or not 1 <= role['per_turn'] <= 2):
                raise ValueError('Keep each declared role within its finite allowance.')
            names.add(role['id'])
            total += role['limit_cents'] * role['per_turn']
        if total > 80:
            raise ValueError('Keep role allowances within eighty cents per parent turn.')
        if arguments.get('session_id') is None and arguments['limit_cents'] + total > 100:
            raise ValueError('Keep the parent and declared jobs within one dollar per turn.')
        plan = json.dumps(roles, sort_keys=True, separators=(',', ':'), allow_nan=False)
        parent_request = identity(arguments['request_id'])
        with self.pool.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            result = self._reserve(db, **arguments)
            prior = db.execute('SELECT plan FROM managed_role_plans WHERE parent_request=?', (parent_request,)).fetchone()
            if prior is not None:
                if prior[0] != plan:
                    raise ValueError('Keep the original parent role funding plan.')
                return result
            if arguments.get('recovering'):
                raise ValueError('Recover the original reserved role plan.')
            db.execute('INSERT INTO managed_role_plans VALUES (?,?)', (parent_request, plan))
            summary = self.pool._summary(db)
            for role in roles:
                if summary['funding_kind'] == 'operator_demo_budget' and summary['operator_model'] != role['model']:
                    raise ValueError('Use the operator-authorized model for each role.')
                for ordinal in range(role['per_turn']):
                    key = hashlib.sha256(f'{parent_request}:{role["id"]}:{ordinal}'.encode()).hexdigest()
                    reservation = self.pool._reserve_in_transaction(db, role['limit_cents'] * 10000, 'managed-role:' + key)
                    db.execute('INSERT INTO managed_role_slots VALUES (?,?,?,?,?,?,?,NULL,NULL)',
                        (parent_request, role['id'], ordinal, role['model'], role['limit_cents'],
                         reservation['reservation_id'], 'reserved'))
            return result

    def activate_role(self, *, parent_request, role_id, ordinal, binding_id, request_id,
                      actor, model, limit_cents, session_id=None, recovering=False):
        for value in (parent_request, role_id, binding_id, request_id, actor, model):
            identity(value)
        with self.pool.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            parent = self._request(db, parent_request)
            slot = db.execute('SELECT * FROM managed_role_slots WHERE parent_request=? AND role_id=? AND ordinal=?',
                              (parent_request, role_id, ordinal)).fetchone()
            if (parent is None or parent['actor'] != actor or parent['status'] == 'terminal' or slot is None
                    or (slot['model'], slot['limit_cents']) != (model, limit_cents)):
                raise ValueError('Use the current parent actor and exact preallocated role slot.')
            if slot['state'] == 'started':
                if (slot['child_binding'], slot['child_request']) != (binding_id, request_id):
                    raise ValueError('Keep the role slot bound to its original child request.')
                return self._reserve(db, binding_id=binding_id, request_id=request_id, actor=actor,
                    model=model, limit_cents=limit_cents, session_id=session_id, recovering=recovering)
            if slot['state'] != 'reserved' or recovering or session_id is not None:
                raise ValueError('Admit an unused role slot once before provider submission.')
            # Check current funding authority without reserving a second hold.
            summary = self.pool._summary(db)
            if summary['funding_kind'] == 'operator_demo_budget':
                authorization = self.pool._operator_authorization(db)
                if (not authorization or authorization['data']['expires_at'] <= self.pool._now()
                        or summary['operator_purpose'] not in {'native_agent_chat','managed_agent_chat'}
                        or summary['operator_model'] != model):
                    raise ValueError('Use an active operator allocation for the role.')
            db.execute('INSERT INTO managed_budgets(binding_id,model) VALUES(?,?)', (binding_id, model))
            db.execute('INSERT INTO managed_allowances VALUES(?,?,?,?,?,?,NULL,NULL)',
                       (request_id, binding_id, actor, limit_cents, slot['reservation_id'], 'admitted'))
            self.pool._append(db, 'start:' + slot['reservation_id'], 'started', reservation_id=slot['reservation_id'],
                              provider='openai_agents', binding_id=binding_id, request_id=request_id)
            db.execute("UPDATE managed_role_slots SET state='started',child_binding=?,child_request=? "
                       'WHERE parent_request=? AND role_id=? AND ordinal=?',
                       (binding_id, request_id, parent_request, role_id, ordinal))
            return self._request(db, request_id)

    def release_unused_roles(self, parent_request):
        with self.pool.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            parent = self._request(db, identity(parent_request))
            if parent is None or parent['status'] != 'terminal':
                raise ValueError('Finish the original parent before releasing unused role slots.')
            rows = db.execute("SELECT * FROM managed_role_slots WHERE parent_request=? AND state='reserved'",
                              (parent_request,)).fetchall()
            for slot in rows:
                reservation = self.pool._reservation(db, slot['reservation_id'])
                if reservation['status'] != 'reserved' or slot['child_binding'] is not None:
                    raise ValueError('Release only a role slot that never admitted provider input.')
                self.pool._append(db, 'release:' + slot['reservation_id'], 'released', reservation_id=slot['reservation_id'])
            db.execute("UPDATE managed_role_slots SET state='released' WHERE parent_request=? AND state='reserved'",
                       (parent_request,))

    def created(self,binding_id,session_id):
        identity(binding_id);identity(session_id)
        with self.pool.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT * FROM managed_budgets WHERE binding_id=?',(binding_id,)).fetchone()
            if prior is None or prior['session_id'] not in (None,session_id) or prior['closed']:
                raise ValueError('Bind the original funded provider session.')
            try:db.execute('UPDATE managed_budgets SET session_id=? WHERE binding_id=?',(session_id,binding_id))
            except sqlite3.IntegrityError as error:raise ValueError('Use the original funded provider session.') from error

    def finish(self,request_id,snapshot,terminal_status):
        if terminal_status not in (None,'completed','failed','cancelled'):
            raise ValueError('Record the verified provider terminal status.')
        with self.pool.connect() as db:
            db.execute('BEGIN IMMEDIATE');prior=self._request(db,identity(request_id))
            if prior is None:raise ValueError('Finish the saved funded request.')
            consumed=None
            if snapshot is not None:
                if snapshot.get('id')!=prior['session_id'] or prior['session_id'] is None:
                    raise ValueError('Reconcile the funded session’s own provider snapshot.')
                consumed=snapshot.get('spend_control',{}).get('consumed')
                if consumed is not None and (type(consumed) is not int or consumed<0):
                    raise ValueError('Record a bounded provider consumption estimate.')
            if prior['status']=='terminal':
                if terminal_status is not None and prior['terminal']!=terminal_status:
                    raise ValueError('Keep the original terminal accounting outcome.')
                return
            db.execute('UPDATE managed_allowances SET status=?,consumed_cents=?,terminal=? WHERE request_id=?',
                       ('terminal' if terminal_status else 'uncertain',consumed,terminal_status,request_id))

    def settle_closed(self,binding_id,receipt_path):
        """Operator-only, provider-final reconciliation; estimates cannot call this."""
        raw=_read_private(receipt_path);receipt=json.loads(raw);pool=self.pool
        expected={'schema_version','provider','kind','pool_scope','parent_company_scope','binding_id',
                  'session_id','session_closed','provider_final','verified_by','record_id','actual_usd','source_reference'}
        if (not isinstance(receipt,dict) or set(receipt)!=expected or receipt['schema_version']!=1
                or receipt['provider']!='openai' or receipt['kind']!='agents_session_final'
                or receipt['pool_scope']!=pool.company_scope or receipt['binding_id']!=binding_id
                or receipt['session_closed'] is not True or receipt['provider_final'] is not True
                or receipt['verified_by']!=OWNER or not isinstance(receipt['source_reference'],str)
                or not 1<=len(receipt['source_reference'])<=1000):
            raise ValueError('Import the verified final provider receipt for this closed session.')
        identity(receipt['record_id']);amount=_money(receipt['actual_usd']);digest=hashlib.sha256(raw).hexdigest()
        with pool.connect() as db:
            db.execute('BEGIN IMMEDIATE');binding=pool._openai_binding(db)
            budget=db.execute('SELECT * FROM managed_budgets WHERE binding_id=?',(binding_id,)).fetchone()
            if (receipt['parent_company_scope']!=binding['parent_company_scope'] or budget is None
                    or receipt['session_id']!=budget['session_id']):
                raise ValueError('Use the provider receipt belonging to this workspace and session.')
            prior=db.execute('SELECT digest FROM managed_final_receipts WHERE binding_id=?',(binding_id,)).fetchone()
            if prior:
                if prior[0]!=digest:raise ValueError('Keep the original final provider receipt.')
                return
            rows=db.execute('SELECT reservation_id FROM managed_allowances WHERE binding_id=? AND reservation_id IS NOT NULL ORDER BY rowid',
                            (binding_id,)).fetchall()
            if not rows:raise ValueError('Reconcile the original funded session reservations.')
            remainder=amount
            for index,row in enumerate(rows):
                reservation=pool._reservation(db,row[0]);bound=_money(reservation['provider_maximum_usd'])
                charge=remainder if index==len(rows)-1 else min(bound,remainder)
                remainder-=charge
                factor=_money(reservation['retail_multiplier'] or '1')
                retail=(charge*factor+999999)//1000000
                pool._append(db,'settled:'+row[0],'settled',retail,row[0],provider_actual=charge,
                             retail_charge=retail if reservation['funding_kind']=='prepaid_customer_funds' else 0,
                             source={'provider':'openai_agents','session_id':budget['session_id'],
                             'receipt_sha256':digest,'source_reference':receipt['source_reference']})
            db.execute('INSERT INTO managed_final_receipts VALUES(?,?,?)',(binding_id,budget['session_id'],digest))
            db.execute('UPDATE managed_budgets SET closed=1 WHERE binding_id=?',(binding_id,))
