"""Channel adapters for participant phone chat. Credentials never enter public records.

Each channel is one adapter behind ChannelAdapter. The Meta WhatsApp transport
is the first implementation; Viber and SMS adapters arrive with their chosen
provider. Templates and webhook readiness are explicit host commissioning
facts. One send per prepared request; redirects, HTTP retries and proxy
environment are disabled. See CHANNELS.md and Meta's official Cloud API
template/webhook contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import inspect
import json
import re

import httpx

MAX_WEBHOOK_BYTES = 256 * 1024
# Channel order for an automatic choice: the first commissioned channel on which
# the member holds a verified address.
CHANNELS = ('viber', 'sms', 'whatsapp')
CHANNEL_LABELS = {'viber': 'Viber', 'sms': 'SMS', 'whatsapp': 'WhatsApp', 'email': 'Email', 'voice': 'Voice'}
ALL_CHANNELS = (*CHANNELS, 'email', 'voice')


class ProviderError(ValueError):
    """Safe protocol error; never carries provider bodies or credential values."""


class SendNotStarted(ProviderError):
    pass


class SendRejected(ProviderError):
    pass


class SendUncertain(ProviderError):
    pass


def e164(value):
    if not isinstance(value, str) or not re.fullmatch(r'\+[1-9][0-9]{6,14}', value):
        raise ProviderError('Enter an international number with its country code.')
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def bounded(value, maximum=128):
    return isinstance(value, str) and 1 <= len(value) <= maximum and all(ord(c) >= 32 for c in value)


@dataclass(frozen=True)
class Template:
    name: str
    language: str
    category: str
    status: str
    body: str | None = None

    def __post_init__(self):
        if (not re.fullmatch(r'[a-z0-9_]{1,128}', self.name)
                or not re.fullmatch(r'[a-z]{2,3}(?:_[A-Z]{2})?', self.language)
                or self.category not in {'AUTHENTICATION', 'UTILITY', 'MARKETING'}
                or self.status not in {'APPROVED', 'PENDING', 'REJECTED', 'PAUSED', 'DISABLED'}
                or self.body is not None and (not isinstance(self.body, str) or len(self.body) > 4096)):
            raise ProviderError('Review the commissioned message template.')


@dataclass(frozen=True)
class MetaCredentials:
    access_token: str = field(repr=False)
    app_secret: str = field(repr=False)
    verify_token: str = field(repr=False)

    def __post_init__(self):
        if any(not isinstance(value, str) or not 16 <= len(value) <= 8192
               or any(ord(c) < 32 for c in value)
               for value in (self.access_token, self.app_secret, self.verify_token)):
            raise ProviderError('Complete the private messaging credentials.')


@dataclass(frozen=True)
class MetaConfiguration:
    company_id: str
    waba_id: str
    phone_number_id: str
    sender_phone: str
    api_version: str
    commissioned: bool = False
    webhook_ready: bool = False
    otp_template: Template | None = None
    hello_template: Template | None = None
    utility_templates: tuple[Template, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.company_id, str) or not 1 <= len(self.company_id) <= 128
                or any(not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,32}', value)
                       for value in (self.waba_id, self.phone_number_id))
                or not re.fullmatch(r'v[0-9]{1,2}\.[0-9]', self.api_version)
                or type(self.commissioned) is not bool or type(self.webhook_ready) is not bool
                or any(value is not None and not isinstance(value, Template)
                       for value in (self.otp_template, self.hello_template))):
            raise ProviderError('Use the host messaging account configuration.')
        if (not isinstance(self.utility_templates, tuple) or len(self.utility_templates) > 32
                or any(not isinstance(t, Template) or t.category != 'UTILITY' for t in self.utility_templates)
                or len({(t.name,t.language) for t in self.utility_templates}) != len(self.utility_templates)):
            raise ProviderError('Review the operational message templates.')
        e164(self.sender_phone)


@dataclass(frozen=True)
class PreparedMessage:
    endpoint: str
    payload: dict = field(repr=False)
    token: str = field(repr=False)


class SentMessage(str):
    """Provider acknowledgement plus authenticated correlation keys, never credentials."""
    def __new__(cls, identifier, *, message_id=None):
        value = super().__new__(cls, identifier)
        value.message_id = message_id
        return value


class ChannelAdapter:
    """One channel behind the same interface (CHANNELS.md, "Channel adapters").

    An adapter is Rocoto's sender account for one channel and one environment.
    The store owns members, bindings, deduplication, routing and the outbox; the
    adapter owns the provider's wire format, its signature and its readiness.

    readiness() answers the built shape {'provider': <channel>, 'configured',
    'ready', 'reason', 'otp_ready', 'hello_ready', 'hello_reason',
    'webhook_ready', 'sender'}. events() answers one neutral shape:

        messages       {'id', 'address', 'text', 'sent_at', 'reply_to', 'payload_sha256'}
        receipts       {'provider_message_id', 'status', 'at', 'address'}
                       status is 'sent', 'delivered', 'read' or 'failed'
        opt_outs       {'address', 'at', 'opted_out'}   a stop keyword or unsubscribe event,
                       and the start keyword that ends it
        subscriptions  {'address', 'at', 'context'}
        ignored        the count of events that are neither

    `reply_to` is whatever the channel returns to name the message a reply
    answers; `reply_rule` says how the store reads it.
    """

    channel = None
    # 'context': reply_to is the provider message ID of the answered message.
    # 'tracking': reply_to is the outbox ID the adapter attached to its last message.
    # 'recent': the channel has no reply context; the latest delivery inside 24 hours is answered.
    reply_rule = 'context'
    require_reply_context = False
    # Seconds a free-text message may follow the member's last message, where the channel has such a window.
    reply_window = None
    signature_header = None
    company_id = None    # the company this sender account serves
    account_id = None    # Rocoto's sender account for the environment; one per channel
    commissioned = False  # the host's explicit commissioning fact for the signed inbound route

    def readiness(self):
        raise NotImplementedError

    def otp_hash(self, challenge_id, code):
        """Hash a security code under a secret only the host holds."""
        raise NotImplementedError

    def address(self, value):
        """Validate one recipient address for this channel; E.164 for sms and whatsapp."""
        return e164(value)

    def prepare(self, kind, address, *, text=None, code=None, alert=None, reference=None):
        """kind is 'otp', 'hello', 'text' or 'utility'; `reference` is the outbox ID."""
        raise NotImplementedError

    def prepare_reply(self, prepared, provider_message_id, *, message_id=None):
        """Attach a previously admitted incoming message when the channel supports threads."""
        return prepared

    def preview(self, kind, *, text=None, alert=None):
        """The text the member reads, saved with the outbox row."""
        return text if kind == 'text' else None

    def units(self, prepared):
        """Billable units, read ahead of the send."""
        return 1

    async def send(self, prepared, current):
        """One provider request. Calls `current` immediately before it leaves and sends only on True."""
        raise NotImplementedError

    def verify(self, raw_body, headers):
        """Raise ProviderError on an invalid signature, ahead of any parsing."""
        raise NotImplementedError

    def events(self, raw_body):
        raise NotImplementedError


class MetaWhatsApp(ChannelAdapter):
    channel = 'whatsapp'
    reply_rule = 'context'
    reply_window = 24 * 3600  # Meta's customer-service window for free text.
    signature_header = 'x-hub-signature-256'

    def __init__(self, config, credentials, *, transport=None):
        if not isinstance(config, MetaConfiguration) or not callable(credentials):
            raise ProviderError('Use the commissioned messaging account.')
        self.config, self._credentials, self.transport = config, credentials, transport

    @property
    def company_id(self):
        return self.config.company_id

    @property
    def account_id(self):
        return self.config.waba_id + ':' + self.config.phone_number_id

    @property
    def commissioned(self):
        return self.config.commissioned

    def credentials(self):
        try:
            value = self._credentials()
            if not isinstance(value, MetaCredentials):
                raise ValueError
            return value
        except Exception:
            raise ProviderError('Complete the private messaging credentials.') from None

    def readiness(self):
        config = self.config
        otp = bool(config.otp_template and config.otp_template.category == 'AUTHENTICATION'
                   and config.otp_template.status == 'APPROVED')
        hello = bool(config.hello_template and config.hello_template.category == 'UTILITY'
                     and config.hello_template.status == 'APPROVED')
        try:
            self.credentials()
            credentials = True
        except ProviderError:
            credentials = False
        reason = ('auth_template_required' if not otp else 'provider_setup_required' if not config.commissioned
                  else 'credentials_required' if not credentials else 'webhook_setup_required' if not config.webhook_ready
                  else None)
        return {'provider': 'whatsapp', 'configured': True, 'ready': reason is None, 'reason': reason,
            'otp_ready': reason is None, 'hello_ready': reason is None and hello,
            'hello_reason': None if reason is None and hello else reason or 'hello_template_required',
            'webhook_ready': config.webhook_ready, 'sender_phone': config.sender_phone,
            'sender': config.sender_phone}

    def otp_hash(self, challenge_id, code):
        # The provider app secret is the host-only pepper; the random challenge
        # ID salts each six-digit code without retaining plaintext OTP material.
        key = self.credentials().app_secret.encode()
        message = (self.config.company_id + '\0' + self.account_id + '\0' + challenge_id + '\0' + code).encode()
        return hmac.new(key, message, hashlib.sha256).hexdigest()

    def prepare(self, kind, address, *, text=None, code=None, alert=None, reference=None):
        ready = self.readiness()
        if not ready['ready']:
            raise SendNotStarted(ready['reason'])
        payload = {'messaging_product': 'whatsapp', 'recipient_type': 'individual', 'to': e164(address)[1:]}
        if kind in {'otp', 'hello'}:
            template = self.config.otp_template if kind == 'otp' else self.config.hello_template
            if kind == 'hello' and not ready['hello_ready']:
                raise SendNotStarted('hello_template_required')
            payload.update(type='template', template={'name': template.name, 'language': {'code': template.language}})
            if kind == 'otp':
                if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
                    raise SendNotStarted('Use the current verification challenge.')
                payload['template']['components'] = [
                    {'type': 'body', 'parameters': [{'type': 'text', 'text': code}]},
                    {'type': 'button', 'sub_type': 'url', 'index': '0', 'parameters': [{'type': 'text', 'text': code}]}]
        elif kind == 'utility':
            template, parameters, _ = self.utility_message(alert)
            payload.update(type='template', template={'name':template.name,
                'language':{'code':template.language}, 'components':[{'type':'body',
                'parameters':[{'type':'text','text':value} for value in parameters]}]})
        elif kind == 'text' and isinstance(text, str) and 1 <= len(text.strip()) <= 4096:
            payload.update(type='text', text={'preview_url': False, 'body': text})
        else:
            raise SendNotStarted('Use a supported bounded message.')
        endpoint = f'https://graph.facebook.com/{self.config.api_version}/{self.config.phone_number_id}/messages'
        return PreparedMessage(endpoint, payload, self.credentials().access_token)

    def preview(self, kind, *, text=None, alert=None):
        if kind == 'utility':
            return self.utility_message(alert)[2]
        if kind == 'hello':
            return self.config.hello_template.body if self.config.hello_template else None
        return text if kind == 'text' else None

    def utility_message(self, alert):
        names = {'monitoring':'rocoto_monitoring_alert_v1', 'urgent':'rocoto_urgent_alert_v1',
            'reminder':'rocoto_task_reminder_v1', 'weekly':'rocoto_weekly_task_update_v1'}
        if (not isinstance(alert, dict) or set(alert) != {'purpose','language','parameters'}
                or alert.get('purpose') not in names or alert.get('language') not in {'en_US','es'}):
            raise SendNotStarted('Choose the operational alert and recipient language.')
        parameters = alert['parameters']
        if (not isinstance(parameters, list) or len(parameters) != 3 or any(
                not isinstance(v,str) or not 1 <= len(v.strip()) <= 800
                or any(ord(c)<32 for c in v) for v in parameters)):
            raise SendNotStarted('Supply the three short alert details.')
        template = next((t for t in self.config.utility_templates
            if t.name == names[alert['purpose']] and t.language == alert['language']), None)
        if not template or template.status != 'APPROVED' or not template.body:
            raise SendNotStarted('utility_template_required')
        if set(re.findall(r'\{\{([0-9]+)\}\}', template.body)) != {'1','2','3'}:
            raise SendNotStarted('Review the approved operational template parameters.')
        # One substitution pass keeps user data inert even if it contains braces.
        values = [v.strip() for v in parameters]
        body = re.sub(r'\{\{([123])\}\}',lambda m:values[int(m[1])-1],template.body)
        return template, values, body

    async def send(self, prepared, current):
        if (not isinstance(prepared, PreparedMessage) or prepared.endpoint !=
                f'https://graph.facebook.com/{self.config.api_version}/{self.config.phone_number_id}/messages'):
            raise SendNotStarted('Prepare the current message.')
        started = False
        async def before(request):
            nonlocal started
            admitted = current()
            if inspect.isawaitable(admitted): admitted = await admitted
            if admitted is not True:
                raise SendNotStarted('The messaging action has finished.')
            started = True
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(20),
                    trust_env=False, follow_redirects=False, event_hooks={'request': [before]}) as client:
                async with client.stream('POST', prepared.endpoint,
                        headers={'Authorization': 'Bearer ' + prepared.token}, json=prepared.payload) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 64 * 1024:
                            raise SendUncertain('Confirm the previous message delivery.')
                    if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
                        raise SendRejected('Review the provider delivery configuration.')
                    if response.status_code != 200:
                        raise SendUncertain('Confirm the previous message delivery.')
                    value = json.loads(raw)
                    messages = value.get('messages')
                    if not isinstance(messages, list) or len(messages) != 1:
                        raise ValueError
                    identifier = messages[0]['id']
                    if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_.:/+=-]{1,256}', identifier):
                        raise ValueError
                    return identifier
        except (SendNotStarted, SendRejected, SendUncertain):
            raise
        except Exception:
            if started:
                raise SendUncertain('Confirm the previous message delivery.') from None
            raise SendNotStarted('Review the messaging connection.') from None

    def verify_subscription(self, mode, token, challenge):
        if (mode != 'subscribe' or not isinstance(token, str) or not isinstance(challenge, str)
                or len(token) > 8192 or len(challenge) > 1024 or not challenge.isascii()):
            raise ProviderError('Verify this messaging subscription.')
        try:
            matched = hmac.compare_digest(token.encode(), self.credentials().verify_token.encode())
        except UnicodeError:
            matched = False
        if not matched:
            raise ProviderError('Verify this messaging subscription.')
        return challenge

    def verify(self, raw_body, headers):
        """Meta signs the raw body with the app secret; nothing is parsed before this passes."""
        if not isinstance(raw_body, bytes) or not 1 <= len(raw_body) <= MAX_WEBHOOK_BYTES:
            raise ProviderError('Use a bounded provider event.')
        try:
            signature = next((value for key, value in headers.items() if key.lower() == self.signature_header), None)
        except (AttributeError, TypeError):
            signature = None
        expected = 'sha256=' + hmac.new(self.credentials().app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if (not isinstance(signature, str) or not re.fullmatch(r'sha256=[0-9a-f]{64}', signature)
                or not hmac.compare_digest(expected, signature)):
            raise ProviderError('Verify this messaging event.')

    def webhook(self, raw, signature):
        """The Meta payload of one verified request: its messages and its statuses."""
        self.verify(raw, {self.signature_header: signature})
        return self._parse(raw)

    def events(self, raw_body):
        """Meta's messages and statuses in the neutral shape. WhatsApp sends no opt-out or subscription here."""
        messages, statuses = self._parse(raw_body)
        result = {'messages': [], 'receipts': [], 'opt_outs': [], 'subscriptions': [], 'ignored': 0}
        for value in statuses:
            try:
                identifier, status = value['id'], value['status']
                if status not in {'sent', 'delivered', 'read', 'failed'} or not bounded(identifier, 256):
                    continue
                result['receipts'].append({'provider_message_id': identifier, 'status': status,
                    'at': float(value['timestamp']), 'address': e164('+' + value.get('recipient_id', ''))})
            except (KeyError, ValueError, TypeError):
                continue
        for value in messages:
            try:
                if value.get('type') != 'text':
                    raise ValueError
                identifier, address = value['id'], e164('+' + value['from'])
                text, sent_at = value['text']['body'], float(value['timestamp'])
                context = value.get('context', {}).get('id')
                if (address == self.config.sender_phone or not bounded(identifier, 256) or not bounded(text, 4096)
                        or context is not None and not bounded(context, 256)):
                    raise ValueError
            except (KeyError, TypeError, ValueError, AttributeError):
                result['ignored'] += 1
                continue
            result['messages'].append({'id': identifier, 'address': address, 'text': text, 'sent_at': sent_at,
                'reply_to': context, 'payload_sha256': digest(value)})
        return result

    def _parse(self, raw):
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_WEBHOOK_BYTES:
            raise ProviderError('Use a bounded provider event.')
        try:
            body = json.loads(raw)
            if body['object'] != 'whatsapp_business_account' or not isinstance(body['entry'], list) or len(body['entry']) > 20:
                raise ValueError
            messages, statuses = [], []
            for entry in body['entry']:
                if entry['id'] != self.config.waba_id:
                    raise ValueError
                for change in entry['changes']:
                    if change.get('field') != 'messages':
                        continue
                    value = change['value']
                    if (value.get('messaging_product') != 'whatsapp'
                            or value['metadata']['phone_number_id'] != self.config.phone_number_id):
                        raise ValueError
                    messages.extend(value.get('messages', []))
                    statuses.extend(value.get('statuses', []))
            if len(messages) + len(statuses) > 100 or any(not isinstance(item, dict) for item in messages + statuses):
                raise ValueError
            return messages, statuses
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ProviderError('Use an event from the commissioned messaging account.') from None
