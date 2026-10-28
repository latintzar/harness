"""The optional communication capabilities a vertical selects from the harness."""
from __future__ import annotations

NAMES = ('whatsapp', 'viber', 'email', 'voice')
LABELS = {'whatsapp': 'WhatsApp', 'viber': 'Viber', 'email': 'Email', 'voice': 'Voice'}


def selection(value=None):
    """Undeclared capabilities remain disabled; settings contain only booleans."""
    if value is None:
        value = {}
    if (not isinstance(value, dict) or set(value) - set(NAMES)
            or any(type(enabled) is not bool for enabled in value.values())):
        raise ValueError('Select built-in capabilities using their names and boolean switches.')
    return {name: value.get(name, False) for name in NAMES}
