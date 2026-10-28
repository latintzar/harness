"""Real provider wire contracts under fixture transports; every send stays local."""
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, urlencode

import httpx
import pytest

from capability_providers import CapabilityConfiguration, UnipileEmail, TwilioVoice, ViberBot
from messaging_provider import ProviderError, SendNotStarted, SendUncertain


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def adapter(kind, transport=None):
    values = {
        'email': ('acc_fixture', 'sender@example.test', {'api_key': 'a' * 32, 'webhook_secret': 'b' * 32}),
        'voice': ('AC' + '1' * 32, '+12025550101', {'auth_token': 'c' * 32}),
        'viber': ('bot_fixture', 'Rocoto', {'auth_token': 'd' * 32}),
    }
    account, sender, secrets = values[kind]
    config = CapabilityConfiguration(kind, 'company', 'fixture', account, sender,
        'https://harness.example.test/api/capabilities/' + kind + '/webhook', True, True)
    cls = {'email': UnipileEmail, 'voice': TwilioVoice, 'viber': ViberBot}[kind]
    return cls(config, lambda: secrets, transport=transport)


@pytest.mark.anyio
@pytest.mark.parametrize('kind,address', [('email', 'recipient@example.test'), ('voice', '+12025550102'), ('viber', 'viber_user')])
async def test_provider_send_is_one_bounded_request_after_current_admission(kind, address):
    seen = []
    def response(request):
        seen.append(request)
        if kind == 'email':
            assert request.url.path == '/v2/acc_fixture/emails/send'
            assert request.headers['x-api-key'] == 'a' * 32
            body = json.loads(request.content)
            assert body['to'] == [{'email': address}] and body['plain_text'] == 'Fixture message'
            return httpx.Response(200, json={'object': 'EmailSent', 'id': 'mail_1'})
        if kind == 'voice':
            body = parse_qs(request.content.decode())
            assert body['To'] == [address] and body['TimeLimit'] == ['60']
            assert '<Gather' in body['Twiml'][0] and 'Fixture message' in body['Twiml'][0]
            return httpx.Response(201, json={'sid': 'CA' + '2' * 32})
        assert request.headers['x-viber-auth-token'] == 'd' * 32
        assert json.loads(request.content)['receiver'] == address
        return httpx.Response(200, json={'status': 0, 'message_token': 123456})
    provider = adapter(kind, httpx.MockTransport(response))
    prepared = provider.prepare('text', address, text='Fixture message', reference='outbox_1')
    with pytest.raises(SendNotStarted):
        await provider.send(prepared, lambda: False)
    assert not seen
    assert await provider.send(prepared, lambda: True)
    assert len(seen) == 1
    assert provider.readiness()['ready']
    with pytest.raises(SendNotStarted):
        provider.prepare('text', address, text='x' * 5000)


@pytest.mark.anyio
async def test_uncertain_provider_response_is_retained_for_reconciliation():
    provider = adapter('email', httpx.MockTransport(lambda request: httpx.Response(503)))
    with pytest.raises(SendUncertain):
        await provider.send(provider.prepare('text', 'a@example.test', text='Hello'), lambda: True)


def test_signed_email_reply_retains_account_and_verified_recipient_routing():
    provider = adapter('email')
    event = {'id': 'event_1', 'type': 'email.new', 'account_id': 'acc_fixture', 'payload': {
        'id': 'email_1', 'account_id': 'acc_fixture', 'from': [{'email': 'recipient@example.test'}],
        'to': [{'email': 'sender@example.test'}], 'plain_text': 'Reply', 'date': '2026-10-11T12:00:00Z',
        'in_reply_to': {'id': 'mail_1', 'message_id': '<mail@example.test>'}}}
    raw = json.dumps(event).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(b'b' * 32, stamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
    provider.verify(raw, {'unipile-signature': f't={stamp},v0={signature}'})
    message = provider.events(raw)['messages'][0]
    assert message['address'] == 'recipient@example.test' and message['reply_to'] == 'mail_1' and provider.require_reply_context
    with pytest.raises(ProviderError):
        provider.verify(raw + b' ', {'unipile-signature': f't={stamp},v0={signature}'})
    event['account_id'] = 'foreign'
    with pytest.raises(ProviderError):
        provider.events(json.dumps(event).encode())


def test_twilio_signed_speech_reply_uses_original_call_and_exact_callback_url():
    provider = adapter('voice')
    values = {'AccountSid': provider.account_id, 'CallSid': 'CA' + '2' * 32,
              'From': '+12025550101', 'To': '+12025550102', 'Direction': 'outbound-api',
              'SpeechResult': 'Please send the report', 'CallStatus': 'in-progress'}
    raw = urlencode(values).encode()
    material = provider.config.callback_url + ''.join(key + values[key] for key in sorted(values))
    signature = base64.b64encode(hmac.new(b'c' * 32, material.encode(), hashlib.sha1).digest()).decode()
    provider.verify(raw, {'x-twilio-signature': signature})
    event = provider.events(raw)['messages'][0]
    assert event['address'] == '+12025550102' and event['reply_to'] == values['CallSid']
    with pytest.raises(ProviderError):
        provider.verify(raw + b'&To=%2B12025550103', {'x-twilio-signature': signature})


def test_viber_callback_signature_and_tracking_context():
    provider = adapter('viber')
    raw = json.dumps({'event': 'message', 'timestamp': 1791720000000, 'message_token': 987,
        'sender': {'id': 'viber_user'}, 'message': {'type': 'text', 'text': 'Reply', 'tracking_data': 'outbox_1'}}).encode()
    signature = hmac.new(b'd' * 32, raw, hashlib.sha256).hexdigest()
    provider.verify(raw, {'x-viber-content-signature': signature})
    assert provider.events(raw)['messages'][0]['reply_to'] == 'outbox_1'
    with pytest.raises(ProviderError):
        provider.verify(raw, {'x-viber-content-signature': '0' * 64})


def test_private_configuration_rotates_credentials_with_exact_account_binding(tmp_path, monkeypatch):
    from capability_providers import provider_for
    from workspace_identity import workspace_scope
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    state = tmp_path / 'workspace'
    scope = workspace_scope(state)
    config_path, credentials_path = private / 'config.json', private / 'credentials.json'
    binding = {'schema_version': 1, 'environment_id': 'bare', 'company_scope': scope,
               'capability': 'email', 'account_id': 'acc_fixture'}
    secrets = {'api_key': 'a' * 32, 'webhook_secret': 'b' * 32}
    config = {'schema_version': 1, 'environment_id': 'bare', 'company_scope': scope, 'capabilities': {
        'email': {'account_id': 'acc_fixture', 'sender': 'sender@example.test',
            'callback_url': 'https://harness.example.test/api/capabilities/email/webhook',
            'commissioned': True, 'webhook_ready': True, 'credentials_path': str(credentials_path)}}}
    def save(path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    save(config_path, config)
    save(credentials_path, dict(binding, credentials=secrets))
    monkeypatch.setenv('ROCOTO_BUILTIN_CAPABILITIES_CONFIG', str(config_path))
    assert provider_for('email', state, 'another-environment') is None
    provider = provider_for('email', state, 'bare')
    assert provider.credentials() == secrets
    secrets['api_key'] = 'r' * 32
    save(credentials_path, dict(binding, credentials=secrets))
    assert provider.credentials()['api_key'] == 'r' * 32
    save(credentials_path, dict(binding, account_id='acc_foreign', credentials=secrets))
    with pytest.raises(ProviderError):
        provider.credentials()
    assert not provider.readiness()['ready']
    with pytest.raises(ProviderError):
        provider_for('email', tmp_path / 'foreign', 'bare')
    config_path.chmod(0o644)
    with pytest.raises(ProviderError):
        provider_for('email', state, 'bare')


def test_default_selected_capabilities_stay_uninstalled_without_host_configuration(tmp_path, monkeypatch):
    from builtin_capabilities import BuiltInCapabilities
    for name in ('ROCOTO_VIDEO_MESSAGING_CONFIG', 'ROCOTO_MESSAGING_CONFIG', 'ROCOTO_BUILTIN_CAPABILITIES_CONFIG'):
        monkeypatch.delenv(name, raising=False)
    selected = BuiltInCapabilities(tmp_path, 'bare', dict.fromkeys(('whatsapp', 'viber', 'email', 'voice'), True))
    assert all(row['selected'] and not row['installed'] and not row['ready'] for row in selected.status().values())
    assert selected.pollers() == ()
