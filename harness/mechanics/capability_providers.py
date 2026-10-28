"""Optional company-owned Viber, email and bounded voice transports.

The adapters reuse ChannelAdapter and its durable outbox contract. Email uses
Unipile v2, already used by Rocoto's Cedar implementation. Voice uses Twilio's
Calls resource with a finite TimeLimit and one signed speech response. It does
not install a live audio agent. Host configuration and credentials are private,
scoped to one environment/company/account, and create no provider resources.

Wire references: https://developers.viber.com/rest/;
https://developer.unipile.com/v2.0/reference/api-usage;
https://developer.unipile.com/v2.0/docs/configure-a-webhook;
https://www.twilio.com/docs/voice/api/call-resource.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
import hashlib
import hmac
import inspect
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import httpx

from messaging_provider import (ChannelAdapter, MAX_WEBHOOK_BYTES, PreparedMessage,
    ProviderError, SendNotStarted, SendRejected, SendUncertain, SentMessage, bounded, digest, e164)
from registered_oauth import read_private_configuration
from workspace_identity import workspace_scope


def email_address(value):
    if (not isinstance(value, str) or len(value) > 254
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value)):
        raise ProviderError('Enter a complete email address.')
    return value.lower()


def body_bytes(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_WEBHOOK_BYTES:
        raise ProviderError('Use a bounded provider event.')
    return raw


def json_body(raw):
    try:
        value = json.loads(body_bytes(raw))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError, RecursionError):
        raise ProviderError('Use a provider event object.') from None


def signature_header(headers, name):
    if not hasattr(headers, 'items'):
        return ''
    value = next((value for key, value in headers.items() if isinstance(key, str) and key.lower() == name), '')
    return value if isinstance(value, str) and value.isascii() and len(value) <= 256 else ''


def empty_events():
    return {'messages': [], 'receipts': [], 'opt_outs': [], 'subscriptions': [], 'ignored': 0}


def message(identifier, address, text, sent_at, context, payload):
    if (not bounded(identifier, 256) or not isinstance(text, str) or not 1 <= len(text) <= 4096
            or any(ord(char) < 32 and char not in '\n\r\t' for char in text)
            or context is not None and not bounded(context, 256)
            or not math.isfinite(sent_at) or sent_at <= 0):
        raise ProviderError('Use a bounded text message.')
    return {'id': identifier, 'address': address, 'text': text, 'sent_at': sent_at,
            'reply_to': context, 'payload_sha256': digest(payload)}


@dataclass(frozen=True)
class CapabilityConfiguration:
    channel: str
    company_id: str
    environment_id: str
    account_id: str
    sender: str
    callback_url: str
    commissioned: bool = False
    webhook_ready: bool = False
    maximum_seconds: int = 60
    email_reply_id: str = 'provider'

    def __post_init__(self):
        if not isinstance(self.callback_url, str) or not bounded(self.callback_url, 2048):
            raise ProviderError('Review the commissioned callback URL.')
        url = urlsplit(self.callback_url)
        if (self.channel not in {'viber', 'email', 'voice'} or not bounded(self.company_id)
                or not bounded(self.environment_id) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', self.account_id)
                or url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment
                or type(self.commissioned) is not bool or type(self.webhook_ready) is not bool
                or self.email_reply_id not in {'provider', 'rfc822'}
                or self.channel != 'email' and self.email_reply_id != 'provider'
                or type(self.maximum_seconds) is not int or not 10 <= self.maximum_seconds <= 120):
            raise ProviderError('Review the commissioned capability account.')
        if self.channel == 'email':
            email_address(self.sender)
        elif self.channel == 'voice':
            e164(self.sender)
            if not re.fullmatch(r'AC[0-9a-fA-F]{32}', self.account_id):
                raise ProviderError('Review the commissioned voice account.')
        elif not bounded(self.sender, 28):
            raise ProviderError('Review the Viber sender name.')


class Provider(ChannelAdapter):
    secret_fields = ()

    def __init__(self, config, credentials, *, transport=None):
        if not isinstance(config, CapabilityConfiguration) or config.channel != self.channel or not callable(credentials):
            raise ProviderError('Use the commissioned capability account.')
        self.config, self._credentials, self.transport = config, credentials, transport
        self.company_id, self.account_id = config.company_id, config.account_id
        self.commissioned = config.commissioned

    def credentials(self):
        try:
            value = self._credentials()
            if (not isinstance(value, dict) or set(value) != set(self.secret_fields)
                    or any(not isinstance(secret, str) or not 16 <= len(secret) <= 8192
                           or any(ord(char) < 32 for char in secret) for secret in value.values())):
                raise ValueError
            return value
        except Exception:
            raise ProviderError('Complete the private capability credentials.') from None

    def readiness(self):
        try:
            self.credentials()
            credentials = True
        except ProviderError:
            credentials = False
        reason = ('provider_setup_required' if not self.commissioned else 'credentials_required' if not credentials
                  else 'webhook_setup_required' if not self.config.webhook_ready else None)
        return {'provider': self.channel, 'configured': True, 'ready': reason is None, 'reason': reason,
                'otp_ready': reason is None, 'hello_ready': reason is None, 'hello_reason': reason,
                'webhook_ready': self.config.webhook_ready, 'sender': self.config.sender}

    def otp_hash(self, challenge_id, code):
        secret = self.credentials()[self.secret_fields[-1]]
        value = '\0'.join((self.company_id, self.account_id, str(challenge_id), str(code)))
        return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def text(self, kind, text, code):
        if not self.readiness()['ready']:
            raise SendNotStarted('Complete this capability setup.')
        if kind == 'otp' and isinstance(code, str) and re.fullmatch(r'[0-9]{6}', code):
            return 'Rocoto verification code: ' + code
        if kind == 'hello':
            return 'You can reply to Rocoto here.'
        if (kind != 'text' or not isinstance(text, str) or not 1 <= len(text.strip()) <= 4096
                or any(ord(char) < 32 and char not in '\n\r\t' for char in text)):
            raise SendNotStarted('Write a short message for this recipient.')
        return text

    def preview(self, kind, *, text=None, alert=None):
        return 'You can reply to Rocoto here.' if kind == 'hello' else text if kind == 'text' else None

    async def send(self, prepared, current):
        if not isinstance(prepared, PreparedMessage) or prepared.endpoint != self.endpoint:
            raise SendNotStarted('Prepare the current message.')
        started = False

        async def before(request):
            nonlocal started
            admitted = current()
            if inspect.isawaitable(admitted):
                admitted = await admitted
            if admitted is not True:
                raise SendNotStarted('The messaging action has finished.')
            # Admission may await storage or authority. Credentials remain host-owned
            # and must match this prepared request at the last synchronous boundary.
            credentials = self.credentials()
            if (not self.config.commissioned or not self.config.webhook_ready
                    or not hmac.compare_digest(prepared.token, credentials[self.secret_fields[0]])):
                raise SendNotStarted('Prepare the current messaging connection.')
            started = True

        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(20), trust_env=False,
                    follow_redirects=False, event_hooks={'request': [before]}) as client:
                async with client.stream('POST', prepared.endpoint, **self.request(prepared)) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 64 * 1024:
                            raise SendUncertain('Confirm the previous message delivery.')
                    if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
                        raise SendRejected('Review the provider delivery configuration.')
                    if response.status_code not in {200, 201, 202}:
                        raise SendUncertain('Confirm the previous message delivery.')
                    identifier = self.identifier(json.loads(raw))
                    if not bounded(identifier, 256):
                        raise ValueError
                    return identifier
        except (SendNotStarted, SendRejected, SendUncertain):
            raise
        except Exception:
            if started:
                raise SendUncertain('Confirm the previous message delivery.') from None
            raise SendNotStarted('Review the messaging connection.') from None


class UnipileEmail(Provider):
    channel = 'email'
    # Only an authenticated provider parent ID can select a bound chat.
    # RFC Message-ID strings and sender-only matches carry no conversation authority.
    reply_rule = 'context'
    require_reply_context = True
    signature_header = 'unipile-signature'
    secret_fields = ('api_key', 'webhook_secret')

    @property
    def endpoint(self):
        return f'https://api.unipile.com/v2/{self.account_id}/emails/send'

    def address(self, value):
        return email_address(value)

    def prepare(self, kind, address, *, text=None, code=None, alert=None, reference=None):
        body = {'to': [{'email': self.address(address)}], 'subject': 'Rocoto', 'plain_text': self.text(kind, text, code)}
        return PreparedMessage(self.endpoint, body, self.credentials()['api_key'])

    def prepare_reply(self, prepared, provider_message_id, *, message_id=None):
        identifier = message_id if self.config.email_reply_id == 'rfc822' else provider_message_id
        if not bounded(identifier, 256):
            raise SendNotStarted('Use the admitted email conversation.')
        return PreparedMessage(prepared.endpoint, dict(prepared.payload, reply_to_message_id=identifier), prepared.token)

    def request(self, prepared):
        return {'headers': {'X-API-KEY': prepared.token}, 'json': prepared.payload}

    def identifier(self, value):
        identifier, message_id = value['id'], value.get('message_id')
        if not bounded(identifier, 256) or message_id is not None and not bounded(message_id, 256):
            raise ValueError
        return SentMessage(identifier, message_id=message_id)

    def verify(self, raw_body, headers):
        raw = body_bytes(raw_body)
        match = re.fullmatch(r't=([0-9]{1,12}),v0=([0-9a-f]{64})', signature_header(headers, self.signature_header))
        if not match or abs(time.time() - int(match[1])) > 300:
            raise ProviderError('Verify this email event.')
        expected = hmac.new(self.credentials()['webhook_secret'].encode(), match[1].encode() + b'.' + raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, match[2]):
            raise ProviderError('Verify this email event.')

    def events(self, raw_body):
        event, result = json_body(raw_body), empty_events()
        envelope = event.get('payload')
        if not isinstance(envelope, dict) or event.get('account_id') != self.account_id:
            raise ProviderError('Use an event from the commissioned email account.')
        if event.get('type') != 'email.new':
            result['ignored'] = 1
            return result
        payload = envelope.get('email', envelope)  # v2 envelope; old signed adapters retain compatibility.
        if not isinstance(payload, dict) or any(value.get('account_id', self.account_id) != self.account_id
                                                for value in (envelope, payload)):
            raise ProviderError('Use an event from the commissioned email account.')
        try:
            sender = payload['from']
            if not isinstance(sender, list) or len(sender) != 1:
                raise ValueError
            address = self.address(sender[0]['email'])
            recipients = {self.address(item['email']) for key in ('to', 'cc') for item in payload.get(key, [])}
            if address == self.address(self.config.sender) or self.address(self.config.sender) not in recipients:
                result['ignored'] = 1
                return result
            stamp = datetime.fromisoformat(payload['date'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError
            parent = payload.get('in_reply_to')
            context = parent.get('id') if isinstance(parent, dict) else None
            context_kind = 'provider'
            headers = payload.get('headers', [])
            if not isinstance(headers, list) or len(headers) > 128:
                raise ValueError
            reply_headers = [item.get('value') for item in headers if isinstance(item, dict)
                             and str(item.get('name', '')).lower() == 'in-reply-to']
            if reply_headers:
                if len(reply_headers) != 1 or not bounded(reply_headers[0], 256):
                    raise ValueError
                context, context_kind = reply_headers[0].strip(), 'message_id'
            text = payload.get('plain_text')
            if text is None:
                text = email_text(payload['body'])
            incoming = message(payload['id'], address, text, stamp.timestamp(), context, payload)
            incoming['reply_kind'] = context_kind
            own_message_id = payload.get('message_id')
            if own_message_id is not None and not bounded(own_message_id, 256):
                raise ValueError
            incoming['message_id'] = own_message_id
            result['messages'].append(incoming)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError('Use a bounded incoming email.') from None
        return result


def email_text(body):
    """Bounded ordinary text from the provider's HTML body; scripts/styles stay out."""
    if not isinstance(body, str) or not 1 <= len(body) <= 65536:
        raise ProviderError('Use a bounded incoming email.')
    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts, self.hidden = [], []
        def handle_starttag(self, tag, attrs):
            if tag in {'script', 'style', 'head'}:
                self.hidden.append(tag)
            if not self.hidden and tag in {'p', 'div', 'br', 'li', 'tr'}:
                self.parts.append('\n')
        def handle_endtag(self, tag):
            if self.hidden and tag == self.hidden[-1]:
                self.hidden.pop()
            if not self.hidden and tag in {'p', 'div', 'li', 'tr'}:
                self.parts.append('\n')
        def handle_data(self, value):
            if not self.hidden:
                self.parts.append(value)
    parser = Text()
    parser.feed(body)
    return re.sub(r'\n{3,}', '\n\n', ''.join(parser.parts)).strip()


class TwilioVoice(Provider):
    channel = 'voice'
    require_reply_context = True
    signature_header = 'x-twilio-signature'
    secret_fields = ('auth_token',)

    @property
    def endpoint(self):
        return f'https://api.twilio.com/2010-04-01/Accounts/{self.account_id}/Calls.json'

    def prepare(self, kind, address, *, text=None, code=None, alert=None, reference=None):
        recipient, text = self.address(address), self.text(kind, text, code)
        response = ElementTree.Element('Response')
        gather = ElementTree.SubElement(response, 'Gather', input='speech', action=self.config.callback_url,
                                       method='POST', speechTimeout='auto', timeout='5')
        ElementTree.SubElement(gather, 'Say').text = text
        ElementTree.SubElement(response, 'Hangup')
        payload = {'To': recipient, 'From': self.config.sender, 'Twiml': ElementTree.tostring(response, encoding='unicode'),
            'TimeLimit': str(self.config.maximum_seconds), 'Timeout': '20', 'StatusCallback': self.config.callback_url,
            'StatusCallbackMethod': 'POST', 'StatusCallbackEvent': ['completed']}
        return PreparedMessage(self.endpoint, payload, self.credentials()['auth_token'])

    def units(self, prepared):
        return math.ceil(self.config.maximum_seconds / 60)

    def request(self, prepared):
        return {'auth': (self.account_id, prepared.token), 'data': prepared.payload}

    def identifier(self, value):
        identifier = value['sid']
        if not re.fullmatch(r'CA[0-9a-fA-F]{32}', identifier):
            raise ValueError
        return identifier

    def form(self, raw_body):
        try:
            parsed = parse_qs(body_bytes(raw_body).decode(), keep_blank_values=True, strict_parsing=True, max_num_fields=100)
            if any(len(values) != 1 for values in parsed.values()):
                raise ValueError
            return {key: values[0] for key, values in parsed.items()}
        except (ValueError, UnicodeError):
            raise ProviderError('Use a bounded voice event.') from None

    def verify(self, raw_body, headers):
        values = self.form(raw_body)
        material = self.config.callback_url + ''.join(key + values[key] for key in sorted(values))
        expected = base64.b64encode(hmac.new(self.credentials()['auth_token'].encode(), material.encode(), hashlib.sha1).digest()).decode()
        if not hmac.compare_digest(expected, signature_header(headers, self.signature_header)):
            raise ProviderError('Verify this voice event.')

    def events(self, raw_body):
        values, result = self.form(raw_body), empty_events()
        if (values.get('AccountSid') != self.account_id or values.get('From') != self.config.sender
                or values.get('Direction') not in {'outbound-api', 'outbound-dial'}):
            raise ProviderError('Use an event from the commissioned voice account.')
        identifier = self.identifier({'sid': values.get('CallSid', '')})
        address = self.address(values.get('To'))
        if values.get('SpeechResult'):
            result['messages'].append(message(identifier + ':speech', address, values['SpeechResult'], time.time(), identifier, values))
        elif values.get('CallStatus') in {'completed', 'busy', 'failed', 'no-answer', 'canceled'}:
            status = 'delivered' if values['CallStatus'] == 'completed' else 'failed'
            result['receipts'].append({'provider_message_id': identifier, 'status': status, 'at': time.time(), 'address': address})
        else:
            result['ignored'] = 1
        return result


class ViberBot(Provider):
    channel = 'viber'
    reply_rule = 'tracking'
    signature_header = 'x-viber-content-signature'
    secret_fields = ('auth_token',)
    endpoint = 'https://chatapi.viber.com/pa/send_message'

    def address(self, value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_+/=-]{1,128}', value):
            raise ProviderError('Use a subscribed Viber address.')
        return value

    def prepare(self, kind, address, *, text=None, code=None, alert=None, reference=None):
        payload = {'receiver': self.address(address), 'sender': {'name': self.config.sender},
                   'type': 'text', 'text': self.text(kind, text, code)}
        if reference is not None:
            if not bounded(reference, 256):
                raise SendNotStarted('Use the current outbox reference.')
            payload['tracking_data'] = reference
        return PreparedMessage(self.endpoint, payload, self.credentials()['auth_token'])

    def request(self, prepared):
        return {'headers': {'X-Viber-Auth-Token': prepared.token}, 'json': prepared.payload}

    def identifier(self, value):
        if type(value.get('status')) is not int or value['status'] != 0:
            raise SendRejected('Review the Viber recipient and account.')
        return str(value['message_token'])

    def verify(self, raw_body, headers):
        expected = hmac.new(self.credentials()['auth_token'].encode(), body_bytes(raw_body), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature_header(headers, self.signature_header)):
            raise ProviderError('Verify this Viber event.')

    def events(self, raw_body):
        event, result = json_body(raw_body), empty_events()
        try:
            kind, stamp = event['event'], float(event['timestamp']) / 1000
            if not math.isfinite(stamp) or stamp <= 0:
                raise ValueError
            if kind == 'message' and event['message'].get('type') == 'text':
                content = event['message']
                result['messages'].append(message(str(event['message_token']), self.address(event['sender']['id']),
                    content['text'], stamp, content.get('tracking_data'), event))
            elif kind in {'delivered', 'seen', 'failed'}:
                result['receipts'].append({'provider_message_id': str(event['message_token']),
                    'status': 'read' if kind == 'seen' else kind, 'at': stamp, 'address': self.address(event['user_id'])})
            elif kind == 'unsubscribed':
                result['opt_outs'].append({'address': self.address(event['user_id']), 'at': stamp, 'opted_out': True})
            elif kind == 'subscribed':
                address = self.address(event['user']['id'])
                result['subscriptions'].append({'address': address, 'at': stamp, 'context': None})
            else:
                result['ignored'] = 1
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ProviderError('Use a bounded Viber event.') from None
        return result


def provider_for(name, state, environment_id):
    """Read one selected capability; absent host configuration installs nothing.

    ROCOTO_BUILTIN_CAPABILITIES_CONFIG is owner-only JSON with schema_version=1,
    environment_id, company_scope and capabilities. Each capability names the
    configuration fields (account_id, sender, callback_url, commissioned,
    webhook_ready, optional maximum_seconds/email_reply_id) plus an absolute credentials_path.
    The credential document repeats schema/environment/company/capability/account
    and contains the provider's `credentials` dictionary. Files are bounded,
    owner-held, regular and symlink-free through read_private_configuration.
    """
    location = os.environ.get('ROCOTO_BUILTIN_CAPABILITIES_CONFIG')
    if not location:
        return None
    try:
        path = Path(location)
        if not path.is_absolute():
            raise ValueError
        raw = read_private_configuration(path, max_bytes=32 * 1024)
        if (not isinstance(raw, dict) or set(raw) != {'schema_version', 'environment_id', 'company_scope', 'capabilities'}
                or type(raw['schema_version']) is not int or raw['schema_version'] != 1
                or not isinstance(raw['capabilities'], dict) or set(raw['capabilities']) - {'email', 'voice', 'viber'}):
            raise ValueError
        if raw['environment_id'] != environment_id:
            return None
        if raw['company_scope'] != workspace_scope(state):
            raise ValueError
        entry = raw['capabilities'].get(name)
        if entry is None:
            return None
        required = {'account_id', 'sender', 'callback_url', 'commissioned', 'webhook_ready', 'credentials_path'}
        if not isinstance(entry, dict) or set(entry) - {'maximum_seconds', 'email_reply_id'} != required:
            raise ValueError
        credential_path = Path(entry['credentials_path'])
        if not credential_path.is_absolute():
            raise ValueError
        config = CapabilityConfiguration(name, raw['company_scope'], environment_id,
            **{key: value for key, value in entry.items() if key != 'credentials_path'})
        binding = {'schema_version': 1, 'environment_id': environment_id, 'company_scope': raw['company_scope'],
                   'capability': name, 'account_id': config.account_id}
    except (ValueError, TypeError, KeyError, OSError):
        raise ProviderError('Review the private capability configuration.') from None

    def credentials():
        value = read_private_configuration(credential_path, max_bytes=32 * 1024)  # gitleaks:allow -- bounded file read, no literal credential
        if (not isinstance(value, dict) or set(value) != set(binding) | {'credentials'}
                or type(value['schema_version']) is not int or any(value[key] != expected for key, expected in binding.items())):
            raise ProviderError('Review the private capability credentials.')
        return value['credentials']

    cls = {'email': UnipileEmail, 'voice': TwilioVoice, 'viber': ViberBot}[name]
    return cls(config, credentials)
