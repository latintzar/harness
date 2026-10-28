"""Stock managed protocol fixture for behavior tests; every request stays local."""
from copy import deepcopy

from managed_codex import ManagedEvent
from managed_runtime import ManagedRuntime
from test_managed_runtime import Client, Policy


class ManagedClient(Client):
    def __init__(self, actions=()):
        super().__init__()
        self.actions = deepcopy(list(actions))
        self.tool_results = []
        self.sequence = 0
        self._next_turn()

    def _next_turn(self):
        self.sequence += 1
        turn = {'id': 'turn-' + str(self.sequence), 'subagent_id': None, 'status': 'in_progress'}
        self.turns.append(turn)
        self.session['required_actions'] = [{
            'type': 'function_call', 'turn_id': turn['id'],
            'call_id': item.get('call_id', 'call-' + str(index)),
            'name': item['name'], 'arguments': deepcopy(item['arguments'])}
            for index, item in enumerate(self.actions)]
        self.events = [ManagedEvent({'type': 'agent.session.turn.completed',
            'session_id': self.session['id'], 'turn': {**turn, 'status': 'completed'}})]

    async def create_session(self, config, *, submission_key):
        self.created.append((deepcopy(config), submission_key))
        self.session['status'] = 'in_progress'
        return self.session

    async def send_message(self, session_id, text, *, submission_key, images=()):
        await super().send_message(session_id, text, submission_key=submission_key, images=images)
        self._next_turn()

    async def interrupt(self, session_id, *, submission_key):
        await super().interrupt(session_id, submission_key=submission_key)
        self.session['required_actions'] = []

    async def submit_events(self, session_id, events, *, submission_key):
        self.tool_results.extend(deepcopy(events))
        delivered = {item['call_id'] for item in events}
        self.session['required_actions'] = [item for item in self.session['required_actions']
            if item['call_id'] not in delivered]
        return {}


def attach_runtime(agent, *, actions=(), policy=None):
    client = ManagedClient(actions)
    policy = policy or Policy()
    agent.managed_runtime = ManagedRuntime(lambda: client, policy, timeout=5)
    return client, policy
