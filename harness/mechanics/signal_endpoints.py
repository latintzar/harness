"""Owner-registered signed HTTPS effects in the workspace's signal ledger."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import ssl
import time
import uuid
from urllib.parse import urlsplit

import httpcore
from custom_http import public_addresses, validated_addresses, _PinnedBackend
from signal_chain import StepHeld, bounded


def target_url(value):
    if not isinstance(value,str) or len(value)>2048 or any(char.isspace() for char in value):
        raise ValueError('Use a bounded public HTTPS endpoint.')
    try:parsed=urlsplit(value);port=parsed.port
    except ValueError:raise ValueError('Use a public HTTPS endpoint.')from None
    host=parsed.hostname
    if (parsed.scheme!='https' or not host or parsed.username is not None or parsed.password is not None
            or port not in (None,443) or parsed.query or parsed.fragment or '.' not in host
            or host.endswith(('.local','.localhost','.internal','.home','.test'))
            or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',part) for part in host.split('.'))):
        raise ValueError('Use a public HTTPS hostname and fixed path.')
    try:ipaddress.ip_address(host)
    except ValueError:pass
    else:raise ValueError('Use a public endpoint hostname.')
    path=parsed.path or '/'
    if (len(path)>1024 or not re.fullmatch(r'/[A-Za-z0-9_./-]*',path) or '//' in path
            or path!='/' and any(part in {'','.','..'} for part in path.strip('/').split('/'))):
        raise ValueError('Use the endpoint’s fixed public path.')
    return 'https://'+host+path


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()


class SignalEndpoints:
    def __init__(self,store,*,authorized,owner_authorized,resolver=public_addresses,network_backend=None,ssl_context=None):
        if not callable(authorized) or not callable(owner_authorized):raise ValueError('Install current endpoint authority.')
        self.store=store;self.authorized=authorized;self.owner_authorized=owner_authorized
        self.resolver=resolver;self.network_backend=network_backend;self.ssl=ssl_context or ssl.create_default_context()
        if self.ssl.verify_mode!=ssl.CERT_REQUIRED or not self.ssl.check_hostname:
            raise ValueError('Verify endpoint TLS identity.')
        self.grants={}
        with store.connect() as db:db.execute('CREATE TABLE IF NOT EXISTS signal_endpoints(id TEXT PRIMARY KEY,status TEXT NOT NULL,body TEXT NOT NULL)')
        store.signals.register_producer('outbound_webhook',self._producer_current)
        store.signals.register('send_webhook',self._send,kind='effect',timeout_seconds=10,cleanup_seconds=5,metered=False,
            description='Send a signed event to the endpoint already registered to this actor and conversation.',
            config_schema={'type':'object','properties':{
                'endpoint_id':{'type':'string','pattern':'^endpoint-[a-f0-9]{32}$'},
                'event':{'type':'string','pattern':'^[a-z][a-z0-9_.]{0,63}$'}},
                'required':['endpoint_id','event'],'additionalProperties':False})

    @staticmethod
    def public(record):
        return {**{key:value for key,value in record.items() if key not in {'secret','revealed_at'}},
                'secret_available':record['revealed_at'] is None and record['status']=='active'}

    @staticmethod
    def _write(db,record):
        db.execute('UPDATE signal_endpoints SET status=?,body=? WHERE id=?',
                   (record['status'],json.dumps(record),record['id']))

    def get(self,key):
        with self.store.connect() as db:row=db.execute('SELECT body FROM signal_endpoints WHERE id=?',(key,)).fetchone()
        if row is None:raise ValueError('Choose a registered workspace endpoint.')
        return json.loads(row['body'])

    def current(self,record):
        try:return record['status']=='active' and self.authorized(record) is True
        except (ValueError,OSError):return False

    def register(self,context,*,label,url,daily_sends=100):
        context.require_parent()
        sender=getattr(context.agent,'_turn_senders',{}).get(context.chat_id,{})
        if (self.owner_authorized(context.actor) is not True or sender.get('channel','web') not in
                {'web','whatsapp','viber','sms','email','voice'} or context.record.get('active_run_id')
                or context.record.get('active_signal_id')):
            raise ValueError('Register the endpoint during an owner’s conversation turn.')
        if not isinstance(label,str) or not 1<=len(label.strip())<=80 or type(daily_sends) is not int or not 1<=daily_sends<=200:
            raise ValueError('Give the endpoint a short label and bounded daily sends.')
        record={'id':'endpoint-'+uuid.uuid4().hex,'status':'active','generation':1,'label':label.strip(),
            'url':target_url(url),'daily_sends':daily_sends,'owner_principal_id':context.data_owner,
            'actor_principal_id':context.actor,'chat_id':context.chat_id,'collection':context.collection,
            'created_at':time.time(),'secret':secrets.token_urlsafe(32),'revealed_at':None}
        if not self.current(record):raise ValueError('Use current workspace endpoint authority.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT COUNT(*) FROM signal_endpoints WHERE status='active'").fetchone()[0]>=20:
                raise ValueError('Review this workspace’s existing endpoints first.')
            context.require_current()
            db.execute('INSERT INTO signal_endpoints VALUES(?,?,?)',(record['id'],'active',json.dumps(record)))
        return self.public(record)

    def list(self,*,actor,chat_id):
        with self.store.connect() as db:rows=[json.loads(row['body']) for row in db.execute('SELECT body FROM signal_endpoints ORDER BY rowid DESC')]
        return [self.public(record) for record in rows if record['actor_principal_id']==actor and record['chat_id']==chat_id]

    def reveal(self,key,*,actor,chat_id):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record=self.get(key)
            if (record['actor_principal_id']!=actor or record['chat_id']!=chat_id or record['revealed_at'] is not None
                    or not self.current(record) or self.owner_authorized(actor) is not True):
                raise ValueError('Copy the current owner’s endpoint signing secret once.')
            record['revealed_at']=time.time();self._write(db,record)
            return record['secret']

    def revoke(self,key,*,actor,chat_id):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');record=self.get(key)
            if (record['actor_principal_id']!=actor or record['chat_id']!=chat_id
                    or self.owner_authorized(actor) is not True):
                raise ValueError('Revoke an endpoint from its owning conversation.')
            record.update(status='revoked',generation=record['generation']+1);self._write(db,record)
        return self.public(record)

    def _producer_current(self,signal):
        grant=self.grants.get(signal['event_id'])
        if grant is None:return False
        try:
            grant.require_parent()
            endpoint=self.get(signal['producer_id'])
            return self.current(endpoint) and endpoint['generation']==signal['source_scope']['generation']
        except (ValueError,OSError):return False

    async def send(self,context,*,endpoint_id,event,payload,call_identity):
        context.require_parent()
        if (not isinstance(call_identity,dict) or set(call_identity)!={'binding_id','request_id','turn_id','call_id'}
                or any(not isinstance(value,str) or not 1<=len(value)<=256 for value in call_identity.values())):
            raise ValueError('Use the current admitted provider function-call identity.')
        if not isinstance(event,str) or not re.fullmatch(r'[a-z][a-z0-9_.]{0,63}',event):
            raise ValueError('Use a short endpoint event type.')
        payload=bounded(payload)
        if not isinstance(payload,dict) or len(canonical(payload))>15000:raise ValueError('Send bounded endpoint object data.')
        endpoint=self.get(endpoint_id)
        if any(endpoint[key]!=value for key,value in {'owner_principal_id':context.data_owner,
                'actor_principal_id':context.actor,'collection':context.collection,'chat_id':context.chat_id}.items()):
            raise ValueError('Use the endpoint registered to this conversation and actor.')
        if not self.current(endpoint):raise ValueError('Use an active workspace endpoint.')
        call_key=digest([call_identity['binding_id'],call_identity['call_id']])
        if call_key in self.grants:raise ValueError('Read the original pending endpoint effect.')
        turn_key=digest([call_identity['binding_id'],call_identity['turn_id']])
        self.grants[call_key]=context
        try:
            signal=self.store.wakes.enqueue_job(producer='outbound_webhook',producer_id=endpoint_id,
                owner=context.data_owner,actor=context.actor,collection=context.collection,chat_id=context.chat_id,event_id=call_key,
                payload={'endpoint_id':endpoint_id,'event':event,'data':payload,'turn_key':turn_key,'chat_id':context.chat_id},
                source_scope={'generation':endpoint['generation']},chain=[{'kind':'effect','handler':'send_webhook'}, {'kind':'deliver'}],
                expires_at=time.time()+1800)
            await self.store.signals.process(signal)
            saved=self.store.wakes.get(signal['id'],owner=context.data_owner)
            receipt=self.store.signals.receipts(signal['id'],owner=context.data_owner)
            result=(receipt[0].get('output',{}).get('delivery') if receipt else None)
            return result or {'id':signal['id']+':0','endpoint_id':endpoint_id,'status':saved.get('chain_status','pending'),
                              'http_status':None,'bytes':0}
        finally:self.grants.pop(call_key,None)

    async def _send(self,payload,config,context):
        signal=context['signal']
        endpoint_id=config.get('endpoint_id') or payload.get('endpoint_id')
        endpoint=self.get(endpoint_id)
        event=config.get('event') or payload.get('event')
        data=payload.get('data',payload)
        if (set(config)-{'endpoint_id','event'} or not isinstance(event,str) or not re.fullmatch(r'[a-z][a-z0-9_.]{0,63}',event)
                or not isinstance(data,dict) or any(endpoint[key]!=signal.get(key) for key in
                    ('owner_principal_id','actor_principal_id','collection','chat_id'))):
            raise StepHeld('Use the endpoint and event belonging to this signal actor.')
        def current():
            try:
                latest=self.get(endpoint_id)
                return (context['current']() and self.current(latest) and latest['generation']==endpoint['generation'])
            except (ValueError,OSError):return False
        if not current():raise StepHeld('Current endpoint authority ended.')
        timestamp=str(int(time.time()));event_id=context['idempotency_key']
        body=canonical({'id':event_id,'type':event,'created_at':timestamp,'data':data})
        if len(body)>16384:raise StepHeld('Keep the signed endpoint body within 16 KB.')
        host=urlsplit(endpoint['url']).hostname
        try:addresses=validated_addresses(await self.resolver(host))
        except (ValueError,OSError):raise StepHeld('Resolve this endpoint to public addresses.')from None
        if not current():raise StepHeld('Current endpoint authority ended.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            turn=payload.get('turn_key')
            if turn:
                used=sum(json.loads(row['body']).get('payload',{}).get('turn_key')==turn for row in db.execute(
                    "SELECT w.body FROM signal_usage u JOIN agent_wakes w ON u.reservation=w.id||':0' WHERE u.kind='send'"))
                prior=db.execute("SELECT 1 FROM signal_usage WHERE kind='send' AND reservation=?",(event_id,)).fetchone()
                if used>=5 and not prior:raise StepHeld('This turn reached its endpoint send limit.')
            if not self.store.signals.reserve(db,'send',event_id,target='endpoint:'+endpoint_id,target_limit=endpoint['daily_sends']):
                raise StepHeld('Today’s endpoint send allowance is reserved.')
        if not current():raise StepHeld('Current endpoint authority ended.')
        context['external']({'event_id':event_id,'endpoint_id':endpoint_id,'generation':endpoint['generation'],
            'request_sha256':hashlib.sha256(body).hexdigest(),'timestamp':timestamp})
        signature='sha256='+hmac.new(endpoint['secret'].encode(),timestamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
        headers=[(b'host',host.encode()),(b'content-type',b'application/json'),(b'content-length',str(len(body)).encode()),
            (b'x-rocoto-event-id',event_id.encode()),(b'x-rocoto-timestamp',timestamp.encode()),(b'x-rocoto-signature',signature.encode())]
        backend=_PinnedBackend(host,addresses,current,self.network_backend)
        async with httpcore.AsyncConnectionPool(ssl_context=self.ssl,network_backend=backend,
                max_connections=1,max_keepalive_connections=0,http2=False,retries=0) as pool:
            async with pool.stream('POST',endpoint['url'],headers=headers,content=body,
                    extensions={'timeout':{'connect':5,'read':5,'write':5,'pool':1}}) as response:
                size=0
                async for chunk in response.aiter_stream():
                    size+=len(chunk)
                    if size>4096:raise TimeoutError('Review the bounded endpoint response.')
                status=response.status
        if status>=500 or status in {408,429}:raise TimeoutError('Reconcile the original endpoint effect.')
        result={'id':event_id,'endpoint_id':endpoint_id,'status':'sent' if 200<=status<300 else 'failed',
                'http_status':status,'bytes':size}
        return {'payload':{'delivery':result}}
