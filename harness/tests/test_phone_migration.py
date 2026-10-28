"""The channel dimension arrives in an existing messaging store in place, keeping every row.

HEAD_STORE is the SQL dump of a messaging.sqlite created by the code at commit
4eea39f8 (the last schema with one WhatsApp phone per member), through its real
enrollment, hello, text, alert, receipt and reply paths on a local mock
transport. Every value in it is synthetic.
"""
import asyncio
import os
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from messaging import Messaging, MessagingActor, MessagingError
from test_messaging import fixture, incoming, webhook

HEAD_STORE = '''
BEGIN TRANSACTION;
CREATE TABLE bindings(member TEXT NOT NULL,account TEXT NOT NULL,generation INTEGER NOT NULL,
        chat TEXT NOT NULL,collection TEXT NOT NULL,created REAL NOT NULL,
        PRIMARY KEY(member,account,chat));
INSERT INTO "bindings" VALUES('alice','12345:67890',1,'setup-alice','shop',1800000000.0);
INSERT INTO "bindings" VALUES('bob','12345:67890',1,'setup-bob','shop',1800000000.0);
INSERT INTO "bindings" VALUES('alice','12345:67890',1,'agent-chat','shop',1800000000.0);
INSERT INTO "bindings" VALUES('bob','12345:67890',1,'bob-chat','shop',1800000030.0);
CREATE TABLE challenges(id TEXT PRIMARY KEY,member TEXT NOT NULL,requester TEXT NOT NULL,
        phone TEXT NOT NULL,account TEXT NOT NULL,chat TEXT NOT NULL,collection TEXT NOT NULL,
        code_hash TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,attempts INTEGER NOT NULL,
        status TEXT NOT NULL,outbox_id TEXT NOT NULL);
INSERT INTO "challenges" VALUES('verify-6e55d813a4950f5a61a56c0d5e3921f8','alice','alice','+34611111111','12345:67890','setup-alice','shop','306caad9c5753a2b3b3cd8ac55d878e25904647570824b9a068a927d4583c0d8',1800000000.0,1800000300.0,0,'verified','msg-c40823cd9af90eb838149b7a2dadd388');
INSERT INTO "challenges" VALUES('verify-b359c2955637caf4a138d45ca6a27e80','bob','bob','+34622222222','12345:67890','setup-bob','shop','4939df290a01ec9aa2d53d570bb2768f9d411042c129be12e805d2687348ff73',1800000000.0,1800000300.0,0,'verified','msg-524eb1baec855e3623e87d28c3848e30');
INSERT INTO "challenges" VALUES('verify-aa530e0818196010127200185aa2149b','carol','carol','+34633333333','12345:67890','setup-carol','shop','ff6cb6e658a69802bedba3a563fe509f2e7660a545cfa381526c30dddd2d1fa2',1800000091.0,1800000391.0,0,'sent','msg-44e5c5133d4c787b289a69df022a0f6a');
CREATE TABLE contact_generations(member TEXT PRIMARY KEY,generation INTEGER NOT NULL);
INSERT INTO "contact_generations" VALUES('alice',1);
INSERT INTO "contact_generations" VALUES('bob',1);
CREATE TABLE contacts(member TEXT PRIMARY KEY,phone TEXT NOT NULL UNIQUE,
        account TEXT NOT NULL,generation INTEGER NOT NULL,setup_chat TEXT NOT NULL,collection TEXT NOT NULL,
        verified_at REAL NOT NULL,verified_challenge TEXT NOT NULL,last_incoming REAL);
INSERT INTO "contacts" VALUES('alice','+34611111111','12345:67890',1,'setup-alice','shop',1800000000.0,'verify-6e55d813a4950f5a61a56c0d5e3921f8',1800000030.0);
INSERT INTO "contacts" VALUES('bob','+34622222222','12345:67890',1,'setup-bob','shop',1800000000.0,'verify-b359c2955637caf4a138d45ca6a27e80',NULL);
CREATE TABLE delivery_events(account TEXT NOT NULL,provider_message_id TEXT NOT NULL,
        status TEXT NOT NULL,sent_at REAL NOT NULL,phone TEXT NOT NULL,PRIMARY KEY(account,provider_message_id,status));
INSERT INTO "delivery_events" VALUES('12345:67890','wamid.out-4','delivered',1800000000.0,'+34611111111');
INSERT INTO "delivery_events" VALUES('12345:67890','wamid.out-4','read',1800000000.0,'+34611111111');
CREATE TABLE identity(company TEXT NOT NULL);
INSERT INTO "identity" VALUES('company');
CREATE TABLE inbox(id TEXT PRIMARY KEY,account TEXT NOT NULL,provider_event_id TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,member TEXT,phone TEXT NOT NULL,chat TEXT,collection TEXT,
        contact_generation INTEGER,text TEXT,reply_context TEXT,sent_at REAL NOT NULL,received_at REAL NOT NULL,
        status TEXT NOT NULL,claim_id TEXT,admission_key TEXT NOT NULL,UNIQUE(account,provider_event_id));
INSERT INTO "inbox" VALUES('in-04d778ad0df4890e18fabf1376029250','12345:67890','wamid.in-1','4efd4fe48c222da3d48e2d79f0eff7d678fa52bf3712045ecc3202df0df99213','alice','+34611111111','setup-alice','shop',1,'Hello back','wamid.out-3',1800000000.0,1800000000.0,'admitted','b64075881267f4018c8d137b0869fdd2','whatsapp:7981f04809d9e67bc30cc08f276d1331924885e90108439ef2726c316303c687');
INSERT INTO "inbox" VALUES('in-086167e185ed269ddf00c8f2e1140321','12345:67890','wamid.in-2','a4f1784cf4ebef7c46fc1ce6b166c91baaa62dc138d71d4864cd1ea54366dd27','alice','+34611111111','agent-chat','shop',1,'Three new deliveries','wamid.out-4',1800000030.0,1800000030.0,'pending',NULL,'whatsapp:b76491d586584c1c8f4154ef28c0df4055d36cfdbede89ea830ee1b4b3eea3a8');
INSERT INTO "inbox" VALUES('in-ca001a56d21c6e6a67eda9e650a1e455','12345:67890','wamid.in-3','d1f9d69e939109bf467ca0bbdf3d878d248a8836c0809d62dc43b4023efd369e',NULL,'+34699999999',NULL,NULL,NULL,NULL,NULL,1800000030.0,1800000030.0,'unverified',NULL,'whatsapp:a22a693111f13cf2adbca808c120991bda873d3b65e8a0d55b66e55a9dd6b283');
CREATE TABLE outbox(id TEXT PRIMARY KEY,member TEXT NOT NULL,account TEXT NOT NULL,
        generation INTEGER,phone TEXT NOT NULL,chat TEXT NOT NULL,collection TEXT NOT NULL,
        kind TEXT NOT NULL,text TEXT,request_id TEXT NOT NULL,intent_sha256 TEXT NOT NULL,
        status TEXT NOT NULL,provider_message_id TEXT,created REAL NOT NULL,updated REAL NOT NULL,
        waiting INTEGER NOT NULL DEFAULT 0,UNIQUE(member,request_id),UNIQUE(account,provider_message_id));
INSERT INTO "outbox" VALUES('msg-c40823cd9af90eb838149b7a2dadd388','alice','12345:67890',NULL,'+34611111111','setup-alice','shop','otp',NULL,'otp-alice','f105714b0327e26ba89a1fc08d4780a3cff2df50ba96c14e4570d05afcb061aa','sent','wamid.out-1',1800000000.0,1800000000.0,0);
INSERT INTO "outbox" VALUES('msg-524eb1baec855e3623e87d28c3848e30','bob','12345:67890',NULL,'+34622222222','setup-bob','shop','otp',NULL,'otp-bob','6e5cf23e6c85a4a94638709f44c10d53bf27d5ad9fdf4327ea0092ea737c578a','sent','wamid.out-2',1800000000.0,1800000000.0,0);
INSERT INTO "outbox" VALUES('msg-4398bbc1e0fd47b86fc54273c143ec49','alice','12345:67890',1,'+34611111111','setup-alice','shop','hello','Hello from Rocoto. Reply here.','hello-alice','f9735e2dc6d67043bc5aedbd3ffd05721dff01f092ea786e57bb1ac587d12936','sent','wamid.out-3',1800000000.0,1800000000.0,0);
INSERT INTO "outbox" VALUES('msg-70c35d55b9d37a1e953f0ba83d4e9bd9','alice','12345:67890',1,'+34611111111','agent-chat','shop','text','What changed today?','agent-1','a702e2fe01271f3fa7d6c18865cc87e67b84ab9c7b29ef6688eea8e994535e16','read','wamid.out-4',1800000000.0,1800000000.0,0);
INSERT INTO "outbox" VALUES('msg-784099192eed281ace212409e2ff5385','bob','12345:67890',1,'+34622222222','bob-chat','shop','utility','Alert for Checkout: Six people waiting. Recorded 09:00.','alert-bob','eda7bcd9caa9f9c467d12969aca7424a78e86bc86da7d12d70358ae2e4b17833','sent','wamid.out-5',1800000030.0,1800000030.0,1);
INSERT INTO "outbox" VALUES('msg-44e5c5133d4c787b289a69df022a0f6a','carol','12345:67890',NULL,'+34633333333','setup-carol','shop','otp',NULL,'otp-carol','2cd88b22813a6626ecc0c0fb9f87c9d457628252497b0b67050dfb117bb7c9dc','sent','wamid.out-6',1800000091.0,1800000091.0,0);
INSERT INTO "outbox" VALUES('msg-2dc24c8d0252c5a999956fc7a902b7f7','bob','12345:67890',1,'+34622222222','setup-bob','shop','hello','Hello from Rocoto. Reply here.','hello-bob','bd095c992896b728adcf5248203356f0fb25ece69b23f99be0b3e16bfdce3375','uncertain',NULL,1800000091.0,1800000091.0,0);
CREATE INDEX challenge_member_created ON challenges(member,created);
CREATE INDEX inbox_status ON inbox(status,received_at);
COMMIT;
'''

TABLES = ('bindings', 'challenges', 'contact_generations', 'contacts', 'delivery_events', 'identity', 'inbox', 'outbox')
ALICE = MessagingActor('company', 'alice', 'shop', 'setup-alice', 'alice')
BOB = MessagingActor('company', 'bob', 'shop', 'setup-bob', 'bob')


def head_store(tmp_path):
    """An existing company store at the HEAD schema, with the provider it was created under."""
    import httpx
    issued = []
    def later(request):
        issued.append(request)
        return httpx.Response(200, json={'messages': [{'id': f'wamid.after-upgrade-{len(issued)}'}]})
    store, _, provider, sent, now = fixture(tmp_path, handler=later)
    with sqlite3.connect(store.path) as db:
        db.executescript(HEAD_STORE)
    os.chmod(store.path, 0o600)
    now[0] = 1_800_000_100.0
    return store, provider, sent, now


def rows(path, query):
    with sqlite3.connect(path) as db:
        return db.execute(query).fetchall()


def counts(path):
    return {table: rows(path, f'SELECT count(*) FROM {table}')[0][0] for table in TABLES}


def columns(path, table):
    return [row[1] for row in rows(path, f'PRAGMA table_info({table})')]


def test_a_head_store_upgrades_in_place_on_first_read_and_keeps_every_row(tmp_path):
    store, provider, sent, now = head_store(tmp_path)
    before = counts(store.path)
    assert before == {'bindings': 4, 'challenges': 3, 'contact_generations': 2, 'contacts': 2,
        'delivery_events': 2, 'identity': 1, 'inbox': 3, 'outbox': 7}
    assert rows(store.path, 'PRAGMA user_version') == [(0,)] and 'channel' not in columns(store.path, 'contacts')
    old = {table: rows(store.path, f'SELECT * FROM {table} ORDER BY 1,2,3') for table in ('challenges', 'outbox', 'inbox', 'delivery_events', 'bindings')}
    # The first operation after the upgrade is an ordinary read.
    described = store.describe(ALICE)
    assert described['contact'] == {**described['contact'], 'member_id': 'alice', 'phone': '+34611111111',
        'channel': 'whatsapp', 'verified': True, 'generation': 1, 'setup_chat_id': 'setup-alice'}
    assert rows(store.path, 'PRAGMA user_version') == [(1,)]
    assert counts(store.path) == before
    # Contacts became verified channel addresses on the one built channel.
    assert rows(store.path, 'SELECT member,channel,address,account,generation,setup_chat,collection,verified_at,verified_challenge,last_incoming,opted_out FROM contacts ORDER BY member') == [
        ('alice', 'whatsapp', '+34611111111', '12345:67890', 1, 'setup-alice', 'shop', 1800000000.0, 'verify-6e55d813a4950f5a61a56c0d5e3921f8', 1800000030.0, None),
        ('bob', 'whatsapp', '+34622222222', '12345:67890', 1, 'setup-bob', 'shop', 1800000000.0, 'verify-b359c2955637caf4a138d45ca6a27e80', None, None)]
    assert rows(store.path, 'SELECT member,channel,generation FROM contact_generations ORDER BY member') == [
        ('alice', 'whatsapp', 1), ('bob', 'whatsapp', 1)]
    # Every other row keeps its values and gains the channel it was sent or received on.
    for table, prior in old.items():
        current = rows(store.path, f'SELECT * FROM {table} ORDER BY 1,2,3')
        assert [row[:-1] for row in current] == prior and {row[-1] for row in current} == {'whatsapp'}
        assert columns(store.path, table)[-1] == 'channel' and 'phone' not in columns(store.path, table)
    assert rows(store.path, 'SELECT company FROM identity') == [('company',)]
    assert os.stat(store.path).st_mode & 0o777 == 0o600


def test_the_upgrade_is_idempotent_across_restarts_and_writes(tmp_path):
    store, provider, sent, now = head_store(tmp_path)
    assert store.recover() == {'uncertain_sends': 0, 'uncertain_replies': 0}
    first = rows(store.path, "SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name")
    data = {table: sorted(rows(store.path, f'SELECT * FROM {table}'), key=repr) for table in TABLES}
    for _ in range(2):
        restarted = Messaging(tmp_path, company_id='company', provider=provider, authorized=lambda actor: True, clock=lambda: now[0])
        restarted.recover()
        restarted.describe(BOB)
    assert rows(store.path, "SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name") == first
    assert {table: sorted(rows(store.path, f'SELECT * FROM {table}'), key=repr) for table in TABLES} == data
    assert rows(store.path, 'PRAGMA user_version') == [(1,)]


def test_an_upgraded_store_keeps_its_replies_receipts_fences_and_generations(tmp_path):
    async def scenario():
        store, provider, sent, now = head_store(tmp_path)
        # The reply that was waiting for admission is still waiting, in the same chat, under the same key.
        pending = store.pending_replies()
        assert [(row['provider_event_id'], row['chat_id'], row['member_id'], row['channel']) for row in pending] == [
            ('wamid.in-2', 'agent-chat', 'alice', 'whatsapp')]
        assert pending[0]['admission_key'] == 'whatsapp:b76491d586584c1c8f4154ef28c0df4055d36cfdbede89ea830ee1b4b3eea3a8'
        # A provider retry of an event admitted before the upgrade is still a duplicate.
        now[0] = 1_800_000_000.0
        earlier = incoming(now, id='wamid.in-1', context='wamid.out-3')
        now[0] = 1_800_000_100.0
        repeated = store.handle_webhook(*webhook(provider, [earlier]))
        assert (repeated['duplicates'], repeated['accepted']) == (1, 0)
        claim = store.claim_reply(pending[0]['id'])
        store.finish_reply(claim['id'], claim['claim_id'], admitted=True)
        # The uncertain send from before the upgrade still fences that member, and its request replays.
        assert (await store.send_hello(BOB, request_id='hello-bob'))['status'] == 'uncertain'
        with pytest.raises(MessagingError, match='Confirm the previous message delivery'):
            await store.send_hello(BOB, request_id='hello-again')
        # A late receipt still finds its message.
        event = {'id': 'wamid.out-5', 'status': 'delivered', 'timestamp': str(int(now[0])), 'recipient_id': '34622222222'}
        assert store.handle_webhook(*webhook(provider, statuses=[event]))['delivery_updates'] == 1
        assert rows(store.path, "SELECT status,channel FROM outbox WHERE provider_message_id='wamid.out-5'") == [('delivered', 'whatsapp')]
        assert store.describe(MessagingActor('company', 'carol', 'shop', 'setup-carol', 'carol'))['challenge']['status'] == 'sent'
        # Verifying another number continues the member's generation from before the upgrade.
        challenge = await store.request_code(ALICE, '+34655555555', request_id='otp-new')
        code = sent[-1]['template']['components'][0]['parameters'][0]['text']
        assert store.verify_code(ALICE, challenge['id'], code)['generation'] == 2
        assert sent == [sent[-1]]
    asyncio.run(scenario())


def test_a_store_from_a_later_schema_is_refused(tmp_path):
    store, provider, sent, now = head_store(tmp_path)
    store.describe(ALICE)
    with sqlite3.connect(store.path) as db:
        db.execute('PRAGMA user_version=99')
    before = counts(store.path)
    with pytest.raises(MessagingError, match='Use the current company messaging store'):
        store.describe(ALICE)
    with pytest.raises(MessagingError, match='Use the current company messaging store'):
        store.recover()
    assert counts(store.path) == before and rows(store.path, 'PRAGMA user_version') == [(99,)]


def test_a_new_store_starts_at_the_current_schema(tmp_path):
    async def scenario():
        store, actor, provider, sent, now = fixture(tmp_path)
        challenge = await store.request_code(actor, '+34611111111')
        store.verify_code(actor, challenge['id'], sent[-1]['template']['components'][0]['parameters'][0]['text'])
        assert rows(store.path, 'PRAGMA user_version') == [(1,)]
        assert rows(store.path, 'SELECT member,channel,address FROM contacts') == [('alice', 'whatsapp', '+34611111111')]
        for table in ('challenges', 'outbox', 'inbox', 'delivery_events', 'bindings', 'contacts', 'contact_generations'):
            assert 'channel' in columns(store.path, table) and 'phone' not in columns(store.path, table)
    asyncio.run(scenario())
