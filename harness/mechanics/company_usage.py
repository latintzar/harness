"""Immutable company money events, distinct from provider credit admission.

Public operations can change a cap or reserve existing funds; they cannot fund
an account. Funding imports and provider reconciliation are host-only operations
over protected operator/provider records. Every amount is integer micro-dollars.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from urllib.parse import urlsplit

from private_records import OPERATOR as OWNER, read_private
from workspace_identity import workspace_scope

_UNCHANGED=object()


def _read_private(path):
    source = Path(__file__).resolve()
    code_root = next((p for p in source.parents if (p / '.git').exists() or (p / '.release.json').is_file()), source.parents[1])
    return read_private(path, code_root=code_root)


def _money(value):
    try:
        if isinstance(value,(bool,float)) or not isinstance(value,(str,int,Decimal)):
            raise ValueError()
        amount=Decimal(value)
        if not amount.is_finite() or not 0<=amount<=1_000_000 or amount*1_000_000!=(amount*1_000_000).to_integral_value():
            raise ValueError()
        return int(amount*1_000_000)
    except (ValueError,InvalidOperation):
        raise ValueError('Use nonnegative USD amounts with up to six decimal places.') from None


def _usd(value):
    return f'{Decimal(value)/1_000_000:.6f}'


def _provider_money(value):
    """Provider reports can exceed six decimals; reserve the next micro-dollar."""
    try:
        amount=Decimal(str(value))
        if isinstance(value,bool) or not amount.is_finite() or not 0<=amount<=1_000_000:raise ValueError()
        return int((amount*1_000_000).to_integral_value(rounding=ROUND_CEILING))
    except (ValueError,InvalidOperation):
        raise ValueError('Reconcile a finite nonnegative provider amount.') from None


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _openai_pricing(value):
    required={'revision','source_url','input_usd_per_million',
              'cached_input_usd_per_million','cache_write_input_usd_per_million',
              'output_usd_per_million','tool_usd_per_call'}
    if not isinstance(value,dict) or set(value) not in (required,required|{'long_context'}):
        raise ValueError('Bind the exact verified token pricing before the request.')
    if not isinstance(value['revision'],str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',value['revision']):
        raise ValueError('Identify the verified pricing revision.')
    source=urlsplit(value['source_url']) if isinstance(value['source_url'],str) else None
    if (source is None or source.scheme!='https' or source.hostname not in
            {'openai.com','developers.openai.com','platform.openai.com','learn.chatgpt.com'}
            or source.username or source.password or source.query or source.fragment):
        raise ValueError('Retain an official public pricing reference.')
    for field in required-{'revision','source_url','tool_usd_per_call'}:
        _money(value[field])
    rates=value['tool_usd_per_call']
    if not isinstance(rates,dict) or set(rates)-{'web_search'}:
        raise ValueError('Use only explicitly priced supported provider tools.')
    for rate in rates.values():_money(rate)
    if Decimal(value['cached_input_usd_per_million'])>Decimal(value['input_usd_per_million']):
        raise ValueError('Verify the cached-input rate against the input rate.')
    if 'long_context' in value:
        tier=value['long_context']
        rates={'input_usd_per_million','cached_input_usd_per_million',
               'cache_write_input_usd_per_million','output_usd_per_million'}
        if (not isinstance(tier,dict) or set(tier)!=rates|{'threshold_tokens'}
                or type(tier['threshold_tokens']) is not int or not 1<=tier['threshold_tokens']<=100_000_000):
            raise ValueError('Bind the exact long-context threshold and complete rates.')
        for field in rates:_money(tier[field])
        if Decimal(tier['cached_input_usd_per_million'])>Decimal(tier['input_usd_per_million']):
            raise ValueError('Verify the long-context cached-input rate.')
    return json.loads(json.dumps(value))


class CompanyUsage:
    def __init__(self,state,*,provider=None):
        self.provider=provider
        self.state=Path(state)
        self.state.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.path=self.state/'company-usage.sqlite'
        self.company_scope=workspace_scope(self.state)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL, reservation_id TEXT, amount INTEGER NOT NULL,
                    at TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (
                    revision INTEGER PRIMARY KEY, monthly_limit INTEGER NOT NULL,
                    retail_multiplier TEXT, at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS provider_links (
                    provider_key TEXT PRIMARY KEY, reservation_id TEXT NOT NULL UNIQUE,
                    approval_path TEXT NOT NULL, provider_reservation_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS provider_app_charges (
                    app_id TEXT PRIMARY KEY, reservation_id TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS pool_binding (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), provider TEXT NOT NULL,
                    parent_company_scope TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS openai_requests (
                    reservation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    model TEXT NOT NULL, pricing TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS openai_response_charges (
                    response_id TEXT PRIMARY KEY, reservation_id TEXT NOT NULL UNIQUE,
                    receipt_sha256 TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT,'Money events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT,'Money events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_settings_update BEFORE UPDATE ON settings
                    BEGIN SELECT RAISE(ABORT,'Money settings are versioned'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_settings_delete BEFORE DELETE ON settings
                    BEGIN SELECT RAISE(ABORT,'Money settings are versioned'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_pool_update BEFORE UPDATE ON pool_binding
                    BEGIN SELECT RAISE(ABORT,'Provider pools are permanently bound'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_pool_delete BEFORE DELETE ON pool_binding
                    BEGIN SELECT RAISE(ABORT,'Provider pools are permanently bound'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_openai_request_update BEFORE UPDATE ON openai_requests
                    BEGIN SELECT RAISE(ABORT,'Request pricing is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_openai_request_delete BEFORE DELETE ON openai_requests
                    BEGIN SELECT RAISE(ABORT,'Request pricing is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_openai_charge_update BEFORE UPDATE ON openai_response_charges
                    BEGIN SELECT RAISE(ABORT,'Provider receipts are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_openai_charge_delete BEFORE DELETE ON openai_response_charges
                    BEGIN SELECT RAISE(ABORT,'Provider receipts are immutable'); END;
            ''')
        self.path.chmod(0o600)

    @classmethod
    def openai_pool(cls,parent_state):
        """Host-selected API pool; its funds never share the Modal trial ledger."""
        return cls._bound_openai_pool(parent_state, 'openai-api')

    @classmethod
    def native_agent_pool(cls, parent_state):
        """Separate host-commissioned native chat allowance; image workers never use it."""
        return cls._bound_openai_pool(parent_state, 'native-agent-api')

    @staticmethod
    def agent_pool_path(parent_state):
        parent=Path(parent_state).resolve()
        native=parent/'billing'/'native-agent-api'
        # Once commissioned, exhaustion or expiry must not switch funding sources.
        return native if (native/'company-usage.sqlite').exists() else parent/'billing'/'openai-api'

    @classmethod
    def agent_pool(cls, parent_state):
        return cls._bound_openai_pool(parent_state, cls.agent_pool_path(parent_state).name)

    @classmethod
    def _bound_openai_pool(cls, parent_state, pool_name):
        if pool_name not in {'openai-api','native-agent-api'}:
            raise ValueError('Choose a registered API funding purpose.')
        parent=Path(parent_state).resolve()
        directory=parent/'billing'/pool_name
        if directory.resolve()!=directory:
            raise ValueError('Keep the provider pool inside its owning company state.')
        store=cls(directory)
        parent_scope=workspace_scope(parent)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            binding=db.execute('SELECT * FROM pool_binding').fetchone()
            if binding:
                if binding['provider']!='openai' or binding['parent_company_scope']!=parent_scope:
                    raise ValueError('Use the provider pool belonging to this company.')
            else:
                if db.execute('SELECT 1 FROM events LIMIT 1').fetchone():
                    raise ValueError('Bind a new empty provider pool before funding it.')
                db.execute('INSERT INTO pool_binding VALUES(1,?,?)',('openai',parent_scope))
        return store

    @staticmethod
    def _openai_binding(db):
        row=db.execute('SELECT * FROM pool_binding').fetchone()
        if not row or row['provider']!='openai':
            raise ValueError('Use the company-bound OpenAI API pool for Responses accounting.')
        return dict(row)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row
        return db

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def _append(self,db,key,kind,amount=0,reservation_id=None,**data):
        db.execute('INSERT INTO events(event_key,kind,reservation_id,amount,at,data) VALUES(?,?,?,?,?,?)',
            (key,kind,reservation_id,amount,self._now(),json.dumps(data,allow_nan=False)))

    def _state(self,db):
        settings=db.execute('SELECT * FROM settings ORDER BY revision DESC LIMIT 1').fetchone()
        config=dict(settings) if settings else {'revision':0,'monthly_limit':0,'retail_multiplier':None}
        events=[{**dict(row),'data':json.loads(row['data'])} for row in db.execute('SELECT * FROM events ORDER BY sequence')]
        reservations={}
        for event in events:
            rid=event['reservation_id'];data=event['data'];kind=event['kind']
            if kind=='reserve':
                reservations[rid]={**data,'reservation_id':rid,'status':'reserved',
                    'maximum_usd':_usd(event['amount']),'held':event['amount'],'created_at':event['at']}
            elif rid in reservations:
                current=reservations[rid]
                if kind=='started':current['status']='running'
                elif kind=='provider_hold':
                    current.update(status='awaiting_provider_final',held=event['amount'],provider_reported=data.get('provider_reported',0),
                        calculated_hold_seen=current.get('calculated_hold_seen',False) or data.get('provider_status')=='completed_with_calculated_reserve')
                elif kind=='settled':current.update(status='settled',held=0,actual_usd=_usd(data['provider_actual']))
                elif kind=='released':current.update(status='released_before_remote',held=0)
        return config,events,reservations

    def _operator_authorization(self,db,events=None):
        """Return the current immutable total allowance, including legacy funding."""
        if events is None:_,events,_=self._state(db)
        return next((event for event in reversed(events)
            if event['kind']=='operator_authorization' or event['kind']=='funding'
            and event['data'].get('funding_kind')=='operator_demo_budget'),None)

    def renew_operator_budget(self,path,*,lifecycle,environment_id,expected_prior_record_sha256):
        """Host-only replacement of an expired, fully reconciled total allowance.

        The private record authorizes a new total including all historical costs;
        original funding and money events remain immutable. Monthly policy is separate.
        """
        raw=_read_private(Path(path));record=json.loads(raw)
        required={'schema_version','record_id','kind','environment_id','provider','company_scope',
            'parent_company_scope','verified_by','user_authorized','authorization_reference',
            'amount_usd','expires_at','previous_record_sha256','purpose','allowed_model'}
        if (not isinstance(record,dict) or set(record)!=required or lifecycle!='development'
                or environment_id not in {'demo','retail-demo'} or record['schema_version']!=1
                or record['kind']!='operator_demo_budget_renewal' or record['provider']!='openai'
                or record['company_scope']!=self.company_scope or record['environment_id']!=environment_id
                or record['verified_by']!=OWNER or record['user_authorized'] is not True
                or not isinstance(record['authorization_reference'],str)
                or not 1<=len(record['authorization_reference'])<=1000
                or not isinstance(record['record_id'],str)
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',record['record_id'])
                or not isinstance(expected_prior_record_sha256,str)
                or not re.fullmatch(r'[a-f0-9]{64}',expected_prior_record_sha256)
                or record['previous_record_sha256']!=expected_prior_record_sha256
                or record['purpose']!='managed_agent_chat' or record['allowed_model']!='gpt-6-astra'):
            raise ValueError('Renew the owner-authorized managed allowance for this exact provider workspace.')
        amount=_money(record['amount_usd'])
        if not 0<amount<=20_000_000:raise ValueError('Use a total internal demo allocation of up to $20.')
        try:
            expiry=datetime.fromisoformat(record['expires_at'].replace('Z','+00:00'))
            if expiry.tzinfo is None:raise ValueError()
        except (ValueError,TypeError,AttributeError):
            raise ValueError('Set an explicit future demo allowance expiry.') from None
        digest=hashlib.sha256(raw).hexdigest();key='operator-authorization:'+record['record_id']
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');binding=self._openai_binding(db)
            if record['parent_company_scope']!=binding['parent_company_scope']:
                raise ValueError('Renew the original company provider pool.')
            prior=db.execute('SELECT data FROM events WHERE event_key=?',(key,)).fetchone()
            if prior:
                if json.loads(prior['data'])['record_sha256']!=digest:
                    raise ValueError('Keep the original immutable renewal record.')
                return self._summary(db)
            _,events,reservations=self._state(db)
            authorization=self._operator_authorization(db,events)
            if (authorization is None or authorization['data']['record_sha256']!=expected_prior_record_sha256
                    or authorization['data'].get('environment_id')!=environment_id
                    or authorization['data']['expires_at']>self._now()):
                raise ValueError('Renew the exact expired operator authorization.')
            now=datetime.fromisoformat(self._now())
            if not 0<(expiry-now).total_seconds()<=31*86400:
                raise ValueError('Set a future demo budget expiry within 31 days.')
            if any(r['held'] or r['status'] not in {'settled','released_before_remote'} for r in reservations.values()):
                raise ValueError('Reconcile every original provider reservation before renewal.')
            managed=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_budgets'").fetchone()
            if managed and db.execute('SELECT 1 FROM managed_budgets WHERE closed=0 LIMIT 1').fetchone():
                raise ValueError('Close and reconcile original managed sessions before renewal.')
            spent=sum(event['amount'] for event in events if event['kind']=='settled')
            if amount<spent:raise ValueError('Include historical spending in the renewed total allowance.')
            self._append(db,key,'operator_authorization',amount,
                funding_kind='operator_demo_budget',record_sha256=digest,
                previous_record_sha256=expected_prior_record_sha256,
                authorization_reference=record['authorization_reference'],
                expires_at=expiry.astimezone(timezone.utc).isoformat(),environment_id=environment_id,
                provider='openai',parent_company_scope=binding['parent_company_scope'],
                verified_by=OWNER,purpose=record['purpose'],allowed_model=record['allowed_model'])
            return self._summary(db)

    def _summary(self,db):
        config,events,reservations=self._state(db)
        month=self._now()[:7]
        paid=sum(e['amount'] for e in events if e['kind']=='funding' and e['data']['funding_kind']=='prepaid_customer_funds')
        credit=sum(e['amount'] for e in events if e['kind']=='funding' and e['data']['funding_kind']=='provider_trial_credit')
        authorization=self._operator_authorization(db,events)
        operator=authorization['amount'] if authorization else 0
        operator_active=bool(authorization and authorization['data']['expires_at']>self._now())
        allocated=paid+credit+(operator if operator_active else 0)
        spent=sum(e['amount'] for e in events if e['kind']=='settled')
        monthly=sum(e['amount'] for e in events if e['kind']=='settled' and e['at'][:7]==month)
        held=sum(r['held'] for r in reservations.values())
        provider=sum(e['data']['provider_actual'] for e in events if e['kind']=='settled')
        retail=sum(e['data']['retail_charge'] for e in events if e['kind']=='settled')
        binding=db.execute('SELECT * FROM pool_binding').fetchone()
        return {**({'provider':binding['provider'],'parent_company_scope':binding['parent_company_scope']} if binding else {}),
            'company_scope':self.company_scope,'currency':'USD','revision':config['revision'],'month':month,
            'prepaid_funded_usd':_usd(paid),'provider_credit_funded_usd':_usd(credit),
            'operator_demo_budget_usd':_usd(operator),
            'operator_purpose':authorization['data'].get('purpose') if authorization else None,
            'operator_model':authorization['data'].get('allowed_model') if authorization else None,
            'funding_kind':'operator_demo_budget' if operator else 'provider_trial_credit' if credit else 'prepaid_customer_funds',
            'provider_actual_cost_usd':_usd(provider),'retail_charged_usd':_usd(retail),
            'provider_reported_cost_usd':_usd(provider+sum(r.get('provider_reported',0) for r in reservations.values() if r['status']!='settled')),
            'reserved_usd':_usd(held),'available_usd':_usd(max(0,allocated-spent-held)),
            'remaining_funded_usd':_usd(max(0,allocated-spent)),
            'monthly_limit_usd':_usd(config['monthly_limit']),'monthly_used_usd':_usd(monthly),
            'monthly_available_usd':_usd(max(0,config['monthly_limit']-monthly-held)),
            'retail_multiplier':config['retail_multiplier'],
            'pricing_status':'founder_passthrough_unconfigured' if config['retail_multiplier'] is None else 'explicit_cost_multiplier',
            'reservation_count':len(reservations),
            'subscription_usage':{'status':'separately_metered','amount_usd':None},
            'money_precision':'USD rounded upward to one micro-dollar; provider receipts retain their original amount.',
            'accounting':'Provider estimates remain reserved. Settled provider evidence records costs; month rollover preserves funds and holds.'}

    def summary(self):
        with self.connect() as db:
            db.execute('BEGIN')
            return self._summary(db)

    def reconcile_local(self):
        """Mirror already-recorded provider evidence without querying a provider."""
        with self.connect() as db:
            links=[dict(row) for row in db.execute('SELECT * FROM provider_links')]
        for link in links:
            try:self.sync_query_reservation(link['reservation_id'],link['approval_path'],link['provider_reservation_id'])
            except (ValueError,OSError):pass  # Keep the existing hold when evidence is unavailable.

    def set_monthly_limit(self,expected_revision,monthly_limit_usd,*,retail_multiplier=_UNCHANGED):
        limit=_money(monthly_limit_usd)
        if type(expected_revision) is not int or expected_revision<0:
            raise ValueError('Supply the current usage settings revision.')
        if retail_multiplier is not _UNCHANGED and retail_multiplier is not None:
            value=Decimal(_money(retail_multiplier))/1_000_000
            if not 1<=value<=10:raise ValueError('Use an explicit cost multiplier from one to ten.')
            retail_multiplier=str(value)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');config,_,_=self._state(db)
            if config['revision']!=expected_revision:raise ValueError('Refresh the usage settings revision before saving.')
            if retail_multiplier is _UNCHANGED:retail_multiplier=config['retail_multiplier']
            if self._summary(db)['funding_kind'] in {'provider_trial_credit','operator_demo_budget'} and retail_multiplier is not None:
                raise ValueError('The founder trial accounts directly for provider credit usage.')
            db.execute('INSERT INTO settings VALUES(?,?,?,?)',(expected_revision+1,limit,retail_multiplier,self._now()))
            return self._summary(db)

    def _fund(self,db,record_id,amount,kind,digest,**provenance):
        key='funding:'+record_id
        prior=db.execute('SELECT data FROM events WHERE event_key=?',(key,)).fetchone()
        if prior:
            if json.loads(prior['data'])['record_sha256']!=digest:raise ValueError('Reconcile the original immutable funding record.')
            return
        _,events,_=self._state(db)
        if any(e['kind']=='funding' and e['data']['funding_kind']!=kind for e in events):
            raise ValueError('Keep the provider trial and a prepaid customer account separate.')
        if kind in {'provider_trial_credit','operator_demo_budget'} and any(e['kind']=='funding' for e in events):
            raise ValueError('Reconcile the existing demo allowance before changing its authorization.')
        self._append(db,key,'funding',amount,funding_kind=kind,record_sha256=digest,**provenance)

    def import_operator_budget(self,path,*,lifecycle,environment_id):
        """Host-only internal demo allocation; neither deposit nor provider credit.

        The operator pins the authorization and expiry. Application/agent routes
        cannot call this operation. Ordinary customer funding remains unchanged.
        """
        raw=_read_private(Path(path));record=json.loads(raw)
        if (lifecycle!='development' or environment_id not in {'demo','retail-demo'}
                or record.get('schema_version')!=1 or record.get('kind')!='operator_demo_budget'
                or record.get('company_scope')!=self.company_scope
                or record.get('environment_id')!=environment_id
                or record.get('verified_by')!=OWNER or record.get('user_authorized') is not True
                or not isinstance(record.get('authorization_reference'),str)
                or not 1<=len(record['authorization_reference'])<=1000
                or not isinstance(record.get('record_id'),str) or not 1<=len(record['record_id'])<=160):
            raise ValueError('Import the owner-authorized internal demo allocation.')
        try:
            expiry=datetime.fromisoformat(record['expires_at'].replace('Z','+00:00'))
            now=datetime.fromisoformat(self._now().replace('Z','+00:00'))
            if expiry.tzinfo is None or not 0<(expiry-now).total_seconds()<=31*86400:raise ValueError()
        except (ValueError,TypeError,KeyError,AttributeError):
            raise ValueError('Set a future demo budget expiry within 31 days.') from None
        native=self.state.name=='native-agent-api'
        if native:
            if record.get('purpose')!='native_agent_chat' or record.get('allowed_model')!='gpt-6-astra':
                raise ValueError('Authorize native agent chats and their registered model explicitly.')
        elif record.get('purpose')=='native_agent_chat':
            raise ValueError('Keep the native chat allowance in its dedicated provider pool.')
        amount=_money(record['amount_usd'])
        if not 0<amount<=20_000_000:raise ValueError('Use a bounded internal demo allocation of up to $20.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');binding=self._openai_binding(db)
            if record.get('provider')!='openai' or record.get('parent_company_scope')!=binding['parent_company_scope']:
                raise ValueError('Use the demo company’s own provider budget.')
            self._fund(db,record['record_id'],amount,'operator_demo_budget',hashlib.sha256(raw).hexdigest(),
                authorization_reference=record['authorization_reference'],expires_at=expiry.astimezone(timezone.utc).isoformat(),
                environment_id=environment_id,verified_by=OWNER,
                purpose=record.get('purpose','catalog_image_inspection'),allowed_model=record.get('allowed_model'))
            config,_,_=self._state(db)
            if config['revision']==0:db.execute('INSERT INTO settings VALUES(1,?,NULL,?)',(amount,self._now()))
        return self.summary()

    def import_funding_record(self,path):
        """Host-only import of an actual owner-verified settled deposit receipt."""
        raw=_read_private(Path(path));record=json.loads(raw)
        if (record.get('schema_version')!=1 or record.get('company_scope')!=self.company_scope
                or record.get('kind')!='prepaid_customer_funds' or record.get('settled') is not True
                or record.get('verified_by')!=OWNER or record.get('user_verified') is not True
                or not isinstance(record.get('record_id'),str) or not 1<=len(record['record_id'])<=160
                or not isinstance(record.get('receipt_reference'),str) or not 1<=len(record['receipt_reference'])<=1000):
            raise ValueError('Import this company’s actual settled operator funding receipt.')
        amount=_money(record['amount_usd'])
        if amount<=0:raise ValueError('Import a positive settled funding amount.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            binding=db.execute('SELECT * FROM pool_binding').fetchone()
            if binding and (record.get('provider')!=binding['provider']
                    or record.get('parent_company_scope')!=binding['parent_company_scope']):
                raise ValueError('Import the funding receipt for this company and provider pool.')
            self._fund(db,record['record_id'],amount,'prepaid_customer_funds',hashlib.sha256(raw).hexdigest(),
                receipt_reference=record['receipt_reference'],verified_by=OWNER)
        return self.summary()

    def _provider(self,approval_path):
        if self.provider is None:
            raise ValueError('Install the provider accounting adapter before reading its allowance.')
        return self.provider.read(approval_path)

    def import_demo_allowance(self,approval_path,query_ledger_path=None):
        """Host-only Demo setup; mirrors existing owner-authorized free credit."""
        with self.connect() as db:
            if db.execute('SELECT 1 FROM pool_binding').fetchone():
                raise ValueError('Keep Modal trial credit in its original provider ledger.')
        path,authority,ledger=self._provider(approval_path)
        self.provider.validate(path)
        if query_ledger_path is not None and Path(query_ledger_path).absolute()!=path.parent/'query-ledger.json':
            raise ValueError('Use the query ledger belonging to this authorization.')
        limit=min(_money(authority['approved_run_gross_usd']),
            _money(authority['credit_remaining_usd'])-_money(authority['reserved_credit_usd']))
        if authority.get('gross_usage_limit_usd') is not None:
            limit=min(limit,_money(authority['gross_usage_limit_usd'])-_money(authority['current_gross_usage_usd']))
        if ledger:limit=min(limit,_money(ledger['limit_usd']))
        if not 0<limit<=18_000_000:raise ValueError('Use the existing authorized demo query allowance.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._fund(db,'demo:'+authority['approval_id'],limit,'provider_trial_credit',_digest(authority),
                approval_id=authority['approval_id'],expires_at=authority['expires_at'],workspace=authority['workspace'])
            config,_,_=self._state(db)
            if config['revision']==0:db.execute('INSERT INTO settings VALUES(1,?,NULL,?)',(limit,self._now()))
            for rid,entry in (ledger or {}).get('reservations',{}).items():
                provider_key=self._provider_key(path,rid)
                link=db.execute('SELECT reservation_id FROM provider_links WHERE provider_key=?',(provider_key,)).fetchone()
                company_id=link['reservation_id'] if link else 'usage-'+hashlib.sha256(provider_key.encode()).hexdigest()[:32]
                if not link:
                    maximum=_money(entry['maximum_usd'])
                    self._append(db,'reserve:'+company_id,'reserve',maximum,company_id,
                        idempotency_key='import:'+rid,provider_maximum_usd=_usd(maximum),retail_multiplier=None,
                        funding_kind='provider_trial_credit',imported=True)
                    db.execute('INSERT INTO provider_links VALUES(?,?,?,?)',(provider_key,company_id,str(path),rid))
                self._apply_provider(db,company_id,entry)
        return self.summary()

    def reserve(self,maximum_usd,idempotency_key):
        provider_maximum=_money(maximum_usd)
        if provider_maximum<=0 or not isinstance(idempotency_key,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',idempotency_key):
            raise ValueError('Provide a positive inspection bound and stable request identifier.')
        self.reconcile_local()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');config,_,reservations=self._state(db)
            return self._reserve_in_transaction(db,provider_maximum,idempotency_key)

    def _reserve_in_transaction(self,db,provider_maximum,idempotency_key):
        """Share the same atomic funded balance with provider-specific admissions."""
        config,_,reservations=self._state(db)
        existing=next((r for r in reservations.values() if r['idempotency_key']==idempotency_key),None)
        if existing:
            if existing['provider_maximum_usd']!=_usd(provider_maximum):raise ValueError('Keep a request’s original monetary bound.')
            return self._public_reservation(existing)
        summary=self._summary(db)
        factor=Decimal(config['retail_multiplier'] or '1')
        maximum=int((provider_maximum*factor).to_integral_value(rounding=ROUND_CEILING))
        if maximum>min(_money(summary['available_usd']),_money(summary['monthly_available_usd'])):
            raise ValueError('This company needs funded allowance and monthly headroom before fresh compute.')
        rid='usage-'+uuid.uuid4().hex
        self._append(db,'reserve:'+rid,'reserve',maximum,rid,idempotency_key=idempotency_key,
            provider_maximum_usd=_usd(provider_maximum),retail_multiplier=config['retail_multiplier'],funding_kind=summary['funding_kind'],
            **({'operator_authorization_sha256':self._operator_authorization(db)['data']['record_sha256']}
               if summary['funding_kind']=='operator_demo_budget' else {}))
        return self._public_reservation(self._state(db)[2][rid])

    @staticmethod
    def _public_reservation(record):
        return {**{k:v for k,v in record.items() if k not in {'held','provider_reported'}},'held_usd':_usd(record['held'])}

    def _reservation(self,db,rid):
        try:return self._state(db)[2][rid]
        except KeyError:raise ValueError('Choose a company reservation from this workspace.') from None

    def mark_started(self,reservation_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');record=self._reservation(db,reservation_id)
            if record['status']!='reserved':raise ValueError('A reservation admits one remote attempt.')
            if record.get('funding_kind')=='operator_demo_budget':
                authorization=self._operator_authorization(db)
                if (not authorization or authorization['data']['expires_at']<=self._now()
                        or record.get('operator_authorization_sha256') not in
                            ({None,authorization['data']['record_sha256']} if authorization['kind']=='funding'
                             else {authorization['data']['record_sha256']})):
                    raise ValueError('Use the reservation’s current operator authorization.')
                model=authorization['data'].get('allowed_model')
                request=db.execute('SELECT model FROM openai_requests WHERE reservation_id=?',(reservation_id,)).fetchone()
                if model is not None and (request is None or request['model']!=model):
                    raise ValueError('Use the operator-authorized model for this reservation.')
            if (db.execute('SELECT 1 FROM pool_binding').fetchone()
                    and not db.execute('SELECT 1 FROM openai_requests WHERE reservation_id=?',(reservation_id,)).fetchone()):
                raise ValueError('Bind request identity and pricing before remote API admission.')
            self._append(db,'start:'+reservation_id,'started',reservation_id=reservation_id)

    def bind_openai_request(self,reservation_id,*,request_id,model,pricing):
        """Pin the host's request identity and verified rate snapshot before sending."""
        if (not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',request_id)
                or not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',model)):
            raise ValueError('Bind a bounded request and exact model identity.')
        pricing=_openai_pricing(pricing)
        encoded=json.dumps(pricing,sort_keys=True,separators=(',',':'))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');self._openai_binding(db)
            summary=self._summary(db)
            if self.state.name=='native-agent-api' and (summary['operator_purpose']!='native_agent_chat'
                    or summary['operator_model']!=model):
                raise ValueError('Use the native allowance only for its authorized chat model.')
            record=self._reservation(db,reservation_id)
            prior=db.execute('SELECT * FROM openai_requests WHERE reservation_id=?',(reservation_id,)).fetchone()
            if prior:
                if (prior['request_id'],prior['model'],prior['pricing'])!=(request_id,model,encoded):
                    raise ValueError('Keep the original immutable request identity and pricing.')
                return
            if record['status']!='reserved':
                raise ValueError('Bind pricing before the remote attempt starts.')
            try:db.execute('INSERT INTO openai_requests VALUES(?,?,?,?)',(reservation_id,request_id,model,encoded))
            except sqlite3.IntegrityError:
                raise ValueError('Each API request belongs to one company reservation.') from None

    def settle_openai(self,reservation_id,receipt_path):
        """Settle measured completed Responses usage from a private gateway receipt.

        The gateway must retain the reservation if billable tools or ambiguous
        usage cannot be accounted for, even when some token events were received.
        This is priced provider usage, not a claim that an invoice has been finalized.
        """
        raw=_read_private(Path(receipt_path));receipt=json.loads(raw)
        fields={'schema_version','provider','kind','parent_company_scope','pool_scope','reservation_id',
                'request_id','response_id','model','status','usage','pricing','additional_usage',
                'provider_actual_usd','received_at'}
        if (not isinstance(receipt,dict) or set(receipt)!=fields or receipt['schema_version']!=1
                or receipt['provider']!='openai' or receipt['kind']!='responses_usage'
                or receipt['pool_scope']!=self.company_scope or receipt['reservation_id']!=reservation_id
                or receipt['status'] not in {'completed','incomplete'} or not isinstance(receipt['response_id'],str)
                or not re.fullmatch(r'resp_[A-Za-z0-9_-]{1,190}',receipt['response_id'])):
            raise ValueError('Settle with this request’s completed provider usage receipt.')
        try:
            moment=datetime.fromisoformat(receipt['received_at'].replace('Z','+00:00'))
            if moment.tzinfo is None:raise ValueError()
        except (ValueError,TypeError,AttributeError):
            raise ValueError('Record the receipt acquisition time with its timezone.') from None
        usage=receipt['usage']
        if (not isinstance(usage,dict) or set(usage)!=
                {'input_tokens','cached_input_tokens','cache_write_input_tokens','output_tokens'}
                or any(type(value) is not int or not 0<=value<=100_000_000 for value in usage.values())
                or usage['cached_input_tokens']+usage['cache_write_input_tokens']>usage['input_tokens']):
            raise ValueError('Use complete measured provider token usage.')
        pricing=_openai_pricing(receipt['pricing'])
        long=pricing.get('long_context')
        use_long=bool(long and usage['input_tokens']>long['threshold_tokens'])
        rates=long if use_long else pricing
        exact=((usage['input_tokens']-usage['cached_input_tokens']-usage['cache_write_input_tokens'])*Decimal(rates['input_usd_per_million'])
               +usage['cached_input_tokens']*Decimal(rates['cached_input_usd_per_million'])
               +usage['cache_write_input_tokens']*Decimal(rates['cache_write_input_usd_per_million'])
               +usage['output_tokens']*Decimal(rates['output_usd_per_million']))/1_000_000
        extra=receipt['additional_usage']
        if not isinstance(extra,list) or len(extra)>len(pricing['tool_usd_per_call']):
            raise ValueError('Provide measured usage for the bound provider tools.')
        seen=set()
        for item in extra:
            if (not isinstance(item,dict) or set(item)!={'kind','quantity','unit_usd'}
                    or not isinstance(item['kind'],str) or item['kind'] in seen
                    or item['kind'] not in pricing['tool_usd_per_call']
                    or type(item['quantity']) is not int or not 0<=item['quantity']<=100_000):
                raise ValueError('Provide distinct, complete measured provider tool usage.')
            _money(item['unit_usd'])
            if Decimal(item['unit_usd'])!=Decimal(pricing['tool_usd_per_call'][item['kind']]):
                raise ValueError('Use the tool rate pinned before the request.')
            seen.add(item['kind']);exact+=item['quantity']*Decimal(item['unit_usd'])
        actual=_provider_money(receipt['provider_actual_usd'])
        if Decimal(str(receipt['provider_actual_usd']))!=exact:
            raise ValueError('Reconcile the exact measured usage with its bound provider rates.')
        digest=hashlib.sha256(raw).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');binding=self._openai_binding(db)
            if receipt['parent_company_scope']!=binding['parent_company_scope']:
                raise ValueError('Use the parent company’s provider receipt.')
            record=self._reservation(db,reservation_id)
            request=db.execute('SELECT * FROM openai_requests WHERE reservation_id=?',(reservation_id,)).fetchone()
            if (not request or request['request_id']!=receipt['request_id'] or request['model']!=receipt['model']
                    or json.loads(request['pricing'])!=pricing):
                raise ValueError('The response must match its original request and pricing snapshot.')
            prior=db.execute('SELECT * FROM openai_response_charges WHERE response_id=? OR reservation_id=?',
                             (receipt['response_id'],reservation_id)).fetchone()
            if prior:
                if (prior['reservation_id']!=reservation_id or prior['response_id']!=receipt['response_id']
                        or prior['receipt_sha256']!=digest):
                    raise ValueError('Charge each response once using its immutable receipt.')
                return self._summary(db)
            if record['status']!='running':
                raise ValueError('Settle only an admitted remote API request.')
            db.execute('INSERT INTO openai_response_charges VALUES(?,?,?)',(receipt['response_id'],reservation_id,digest))
            charge=int((actual*Decimal(record['retail_multiplier'] or '1')).to_integral_value(rounding=ROUND_CEILING))
            self._append(db,'settled:'+reservation_id,'settled',charge,reservation_id,
                provider_actual=actual,retail_charge=charge if record['funding_kind']=='prepaid_customer_funds' else 0,provider_actual_usd_raw=str(exact),
                bound_exceeded=actual>_money(record['provider_maximum_usd']),
                source={'provider':'openai','request_id':receipt['request_id'],'response_id':receipt['response_id'],
                        'receipt_sha256':digest,'model':receipt['model'],'usage':usage,'pricing':pricing,
                        'pricing_tier':'long_context' if use_long else 'standard',
                        'additional_usage':extra,'response_status':receipt['status'],
                        'received_at':receipt['received_at'],'basis':'terminal_response_usage_at_pinned_rates',
                        'invoice_status':'unreconciled'})
            return self._summary(db)

    def release_before_remote(self,reservation_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');record=self._reservation(db,reservation_id)
            if record['status']=='released_before_remote':return
            if record['status']!='reserved':raise ValueError('Started work keeps its hold until provider evidence reconciles it.')
            link=db.execute('SELECT * FROM provider_links WHERE reservation_id=?',(reservation_id,)).fetchone()
            if link:
                _,_,ledger=self._provider(link['approval_path'])
                entry=(ledger or {}).get('reservations',{}).get(link['provider_reservation_id'])
                if not entry or entry['status'] not in {'reserved','cancelled_before_remote'}:
                    raise ValueError('The linked provider attempt keeps its company hold.')
            self._append(db,'release:'+reservation_id,'released',reservation_id=reservation_id)

    @staticmethod
    def _provider_key(path,rid):
        return str(Path(path).absolute())+'\0'+rid

    def link_provider(self,reservation_id,approval_path,provider_reservation_id):
        with self.connect() as db:
            if db.execute('SELECT 1 FROM pool_binding').fetchone():
                raise ValueError('Keep Modal billing links in their original provider ledger.')
        path,_,ledger=self._provider(approval_path)
        if not ledger or provider_reservation_id not in ledger['reservations']:raise ValueError('Link an actual provider reservation.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');record=self._reservation(db,reservation_id)
            entry=ledger['reservations'][provider_reservation_id]
            if _money(entry['maximum_usd'])!=_money(record['provider_maximum_usd']):raise ValueError('Company and provider bounds must match.')
            prior=db.execute('SELECT * FROM provider_links WHERE reservation_id=?',(reservation_id,)).fetchone()
            key=self._provider_key(path,provider_reservation_id)
            if prior:
                if prior['provider_key']!=key:raise ValueError('Keep the original provider reservation link.')
            else:
                try:db.execute('INSERT INTO provider_links VALUES(?,?,?,?)',(key,reservation_id,str(path),provider_reservation_id))
                except sqlite3.IntegrityError:raise ValueError('Each provider reservation belongs to one company hold.') from None

    def _apply_provider(self,db,rid,entry):
        record=self._reservation(db,rid)
        if record['status']=='settled':
            if entry['status']=='settled' and record['actual_usd']==_usd(_provider_money(entry['settled_gross_usd'])):return
            raise ValueError('Keep the immutable settled provider receipt.')
        if entry['status']=='cancelled_before_remote':
            if record['status'] not in {'reserved','running','released_before_remote'}:raise ValueError('Reconcile remote admission before releasing its hold.')
            if record['status']!='released_before_remote':self._append(db,'release:'+rid,'released',reservation_id=rid)
            return
        if record['status']=='released_before_remote':raise ValueError('A released company hold cannot admit provider work.')
        if entry['status']=='reserved':return
        if record['status']=='reserved':self._append(db,'start:'+rid,'started',reservation_id=rid)
        factor=Decimal(record['retail_multiplier'] or '1')
        if entry['status']=='settled':
            actual=_provider_money(entry['settled_gross_usd'])
            app_id=entry.get('app_id') or entry.get('billing_evidence',{}).get('app_id')
            if not isinstance(app_id,str) or not re.fullmatch(r'ap-[A-Za-z0-9]+',app_id):
                raise ValueError('A settled charge requires its actual provider app identity.')
            prior=db.execute('SELECT reservation_id FROM provider_app_charges WHERE app_id=?',(app_id,)).fetchone()
            if prior and prior['reservation_id']!=rid:raise ValueError('Charge each provider app once.')
            if not prior:db.execute('INSERT INTO provider_app_charges VALUES(?,?)',(app_id,rid))
            charge=int((actual*factor).to_integral_value(rounding=ROUND_CEILING))
            retail=charge if record['funding_kind']=='prepaid_customer_funds' else 0
            self._append(db,'settled:'+rid,'settled',charge,rid,provider_actual=actual,retail_charge=retail,
                provider_actual_usd_raw=entry['settled_gross_usd'],
                source={'provider':'modal','provider_reservation_id':entry['reservation_id'],
                    'app_id':app_id,
                    'receipt_sha256':_digest(entry.get('billing_evidence',{}))})
        else:
            # Runtime estimates do not release money. Only the provider broker's
            # verified, stopped-app lifecycle calculation may reduce a hold.
            provider_hold=_provider_money(entry['held_usd'])
            held=int((provider_hold*factor).to_integral_value(rounding=ROUND_CEILING))
            if entry['status']!='completed_with_calculated_reserve' or record.get('calculated_hold_seen'):
                held=max(held,record['held'])
            reported=max(record.get('provider_reported',0),_provider_money(entry.get('provider_evidence',{}).get('reported_gross_usd','0')))
            key='hold:'+rid+':'+_digest({'held':held,'reported':reported,'status':entry['status']})
            if db.execute('SELECT 1 FROM events WHERE event_key=?',(key,)).fetchone() is None:
                self._append(db,key,'provider_hold',held,rid,provider_reported=reported,provider_status=entry['status'])

    def sync_query_reservation(self,reservation_id,query_approval_path,provider_reservation_id):
        self.link_provider(reservation_id,query_approval_path,provider_reservation_id)
        _,_,ledger=self._provider(query_approval_path)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');self._apply_provider(db,reservation_id,ledger['reservations'][provider_reservation_id])
        return self.summary()

    def settle(self,reservation_id,actual_usd,source):
        if not isinstance(source,dict) or set(source)!={'query_approval_path','provider_reservation_id'}:
            raise ValueError('Settle using the linked private provider billing record.')
        _,_,ledger=self._provider(source['query_approval_path'])
        entry=(ledger or {}).get('reservations',{}).get(source['provider_reservation_id'])
        _provider_money(actual_usd)
        if not entry or entry['status']!='settled' or Decimal(str(entry['settled_gross_usd']))!=Decimal(str(actual_usd)):
            raise ValueError('Actual charges require a settled matching provider receipt.')
        return self.sync_query_reservation(reservation_id,source['query_approval_path'],source['provider_reservation_id'])
