"""Compose optional channels into one message outbox and ordinary reply ingress."""
import time

from capability_contract import NAMES
from capability_spend import CapabilitySpend
from messaging import Messaging
from workspace_identity import workspace_scope


def compose_messaging(capabilities, *, authorized, clock=time.time, spend=None, send_admission=None,
                      capability_spend=None):
    company = workspace_scope(capabilities.state)
    budget = capability_spend or CapabilitySpend(capabilities.state, capabilities.environment_id, company)
    names = tuple(name for name in NAMES if capabilities.enabled(name))
    adapters = [provider for name in names if (provider := capabilities.provider(name)) is not None]
    service = Messaging(capabilities.state, company_id=company, adapters=adapters, channel_names=names,
                        authorized=authorized, clock=clock, spend=spend, send_admission=send_admission,
                        capability_spend=budget)
    for name in ('email', 'voice'):
        capabilities.host_ready[name] = name in names
        capabilities.spending[name] = lambda name=name: (
            name in service.adapters and budget.ready(service.adapters[name], clock()))
    return service


def callback_response(channel):
    """Use only after signature verification and durable ingress acknowledgement."""
    if channel == 'voice':
        return '<Response><Hangup/></Response>', 'application/xml'
    return '{"accepted":true}', 'application/json'
