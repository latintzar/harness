"""Signed endpoint effects use pinned local transports and the shared signal ledger."""
import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
import httpcore
import pytest
from background_agents import AgentStore
from signal_endpoints import SignalEndpoints


class Stream(httpcore.AsyncMockStream):
    def __init__(self,owner):
        super().__init__([owner.response]);self.owner=owner
    async def write(self,buffer,timeout=None):self.owner.writes.append(buffer)
    async def start_tls(self,ssl_context,server_hostname=None,timeout=None):
        self.owner.hostnames.append(server_hostname);return self


class Network(httpcore.AsyncMockBackend):
    def __init__(self,status=200):
        self.response=f'HTTP/1.1 {status} Result\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok'.encode()
        self.connections=[];self.writes=[];self.hostnames=[]
    async def connect_tcp(self,host,port,**kwargs):
        self.connections.append((host,port));return Stream(self)


def setup(path,*,status=200):
    store=AgentStore(path);allowed=[True];network=Network(status)
    async def resolver(host):return ['93.184.216.34']
    service=SignalEndpoints(store,authorized=lambda record:allowed[0],owner_authorized=lambda actor:actor=='alice',
        resolver=resolver,network_backend=network)
    def current():
        if not allowed[0]:raise ValueError('Actor revoked')
    context=SimpleNamespace(actor='alice',data_owner='organization',collection='company',chat_id='chat',
        require_current=current,require_parent=current,record={},agent=SimpleNamespace(_turn_senders={}),run={})
    endpoint=service.register(context,label='Deliveries',url='https://hooks.example.com/events')
    return store,service,context,endpoint,network,allowed


def call(n='one',turn='turn-1'):return {'binding_id':'binding-1','request_id':'request-1','turn_id':turn,'call_id':n}


def test_endpoint_secret_once_and_one_signed_pinned_request(tmp_path):
    async def proof():
        store,service,context,endpoint,network,_=setup(tmp_path)
        secret=service.reveal(endpoint['id'],actor='alice',chat_id='chat')
        assert 'secret' not in endpoint and secret not in json.dumps(service.list(actor='alice',chat_id='chat'))
        with pytest.raises(ValueError):service.reveal(endpoint['id'],actor='alice',chat_id='chat')
        args={'endpoint_id':endpoint['id'],'event':'delivery.arrived','payload':{'quantity':4}}
        first=await service.send(context,**args,call_identity=call())
        again=await service.send(context,**args,call_identity=call())
        assert first==again and first['status']=='sent'
        assert network.connections==[('93.184.216.34',443)] and network.hostnames==['hooks.example.com']
        head,body=b''.join(network.writes).split(b'\r\n\r\n',1)
        headers=dict(line.split(b': ',1) for line in head.split(b'\r\n')[1:])
        expected=hmac.new(secret.encode(),headers[b'x-rocoto-timestamp']+b'.'+body,hashlib.sha256).hexdigest()
        assert headers[b'x-rocoto-signature']==('sha256='+expected).encode()
        assert json.loads(body)['data']=={'quantity':4}
        assert store.signals.usage()['send']['used']==1 and store.signals.usage()['turn']['used']==0
        with pytest.raises(ValueError):await service.send(context,**{**args,'payload':{'quantity':5}},call_identity=call())
        assert len(network.connections)==1
    asyncio.run(proof())


@pytest.mark.parametrize('url',['http://hooks.example.com','https://127.0.0.1','https://a.internal',
    'https://user:password@hooks.example.com','https://hooks.example.com/x?q=secret','https://hooks.example.com/a/../b',
    'https://hooks.example.com:444/events','https://hooks.example.com/%2fprivate'])
def test_endpoint_rejects_unreviewable_addresses(tmp_path,url):
    _,service,context,_,network,_=setup(tmp_path)
    with pytest.raises(ValueError):service.register(context,label='Other',url=url)
    assert not network.connections


def test_private_dns_and_revocation_while_resolving_send_nothing(tmp_path):
    async def proof():
        for private in (True,False):
            store,service,context,endpoint,network,allowed=setup(tmp_path/str(private))
            async def resolver(host):
                if private:return ['93.184.216.34','127.0.0.1']
                allowed[0]=False;return ['93.184.216.34']
            service.resolver=resolver
            result=await service.send(context,endpoint_id=endpoint['id'],event='ready',payload={},call_identity=call())
            assert result['status'] in {'held','uncertain'} and network.connections==[]
    asyncio.run(proof())


def test_shared_daily_and_per_turn_caps_precede_network(tmp_path):
    async def proof():
        store,service,context,endpoint,network,_=setup(tmp_path)
        args={'endpoint_id':endpoint['id'],'event':'ready','payload':{}}
        for n in range(5):assert (await service.send(context,**args,call_identity=call(str(n))))['status']=='sent'
        assert (await service.send(context,**args,call_identity=call('six')))['status']=='held'
        assert len(network.connections)==5
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE');assert store.signals.reserve(db,'send','phone-prior',amount=195)
        assert (await service.send(context,**args,call_identity=call('seven','turn-2')))['status']=='held'
        assert len(network.connections)==5 and store.signals.usage()['send']['used']==200
    asyncio.run(proof())


def test_redirect_refused_and_uncertain_result_never_replayed(tmp_path):
    async def proof():
        for status in (302,503):
            store,service,context,endpoint,network,_=setup(tmp_path/str(status),status=status)
            args={'endpoint_id':endpoint['id'],'event':'ready','payload':{}}
            result=await service.send(context,**args,call_identity=call())
            assert result['status']==('failed' if status==302 else 'uncertain')
            assert (await service.send(context,**args,call_identity=call()))==result
            store.recover()
            assert (await service.send(context,**args,call_identity=call()))==result
            assert len(network.connections)==1
    asyncio.run(proof())


def test_registration_requires_actual_owner_and_human_turn(tmp_path):
    _,service,context,_,network,_=setup(tmp_path)
    for actor,sender in [('bob','web'),('alice','timer'),('alice','webhook')]:
        context.actor=actor;context.agent._turn_senders={'chat':{'channel':sender}}
        with pytest.raises(ValueError):service.register(context,label='Other',url='https://hooks.example.com/other')
    assert not network.connections
