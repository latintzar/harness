"""Workspace-owned optional communication services, without startup provider effects.

Verticals select product capabilities. Host commissioning supplies accounts,
credentials, senders and spending authority. A selected adapter alone never means
that an account, callback or monetary grant has been verified.
"""
from __future__ import annotations

from pathlib import Path

from capability_contract import NAMES, LABELS, selection
from workspace_identity import workspace_scope


def default_factory(name):
    if name == 'whatsapp':
        from messaging_config import provider_for
        return provider_for
    from capability_providers import provider_for
    return lambda state, environment: provider_for(name, state, environment)


class BuiltInCapabilities:
    def __init__(self, state, environment_id, selected, *, factories=None, spending=None, host_ready=None):
        self.state, self.environment_id = Path(state), environment_id
        self.selected = selection(selected)
        self.factories = dict(factories or {})
        self.spending = dict(spending or {})
        self.host_ready = selection(host_ready)
        self._providers = {}
        if set(self.factories) - set(NAMES) or set(self.spending) - set(NAMES):
            raise ValueError('Install declared built-in capabilities for this workspace.')

    def enabled(self, name):
        if name not in NAMES:
            raise ValueError('Choose a built-in capability.')
        return self.selected[name]

    def provider(self, name):
        if not self.enabled(name):
            return None
        if name not in self._providers:
            factory = self.factories[name] if name in self.factories else default_factory(name)
            provider = factory(self.state, self.environment_id)
            if provider is not None and (getattr(provider, 'company_id', None) != workspace_scope(self.state)
                    or getattr(provider, 'channel', None) != name or not getattr(provider, 'account_id', None)):
                raise ValueError('Install the sender account belonging to this workspace and capability.')
            self._providers[name] = provider
        return self._providers[name]

    def pollers(self):
        """Only explicitly installed providers contribute host polling callbacks."""
        result = []
        for name in NAMES:
            provider = self.provider(name)
            poll = getattr(provider, 'poll', None)
            if callable(poll):
                result.append((name, poll))
        return tuple(result)

    def status(self):
        rows = {}
        for name in NAMES:
            row = {'id': name, 'label': LABELS[name], 'selected': self.enabled(name),
                   'installed': False, 'configured': False, 'provider_ready': False,
                   'spending_ready': False, 'host_ready': self.host_ready[name] is True, 'ready': False}
            if row['selected']:
                try:
                    provider = self.provider(name)
                    ready = provider.readiness() if provider is not None else {}
                    row.update(installed=provider is not None, configured=ready.get('configured') is True,
                               provider_ready=ready.get('ready') is True)
                    allowance = self.spending.get(name)
                    row['spending_ready'] = bool(allowance and allowance() is True)
                    row['ready'] = row['provider_ready'] and row['spending_ready'] and row['host_ready']
                except (ValueError, OSError, RuntimeError, TypeError):
                    row['reason'] = 'commissioning_required'
            rows[name] = row
        return rows
