"""A tool keeps the acting participant's original grant across nested scopes."""
import asyncio
from types import SimpleNamespace

import pytest

from scope_host import agent_scope, current_admission


@pytest.mark.parametrize('admit_root', [False, True])
@pytest.mark.parametrize('revoked_at', ['before_tool', 'during_tool'])
def test_nested_tool_keeps_the_acting_participants_current_membership(admit_root, revoked_at):
    members={'owner':{'role':'operator'}, 'participant':{'role':'operator'}}
    class Scope:
        company_id='company'
        def member(self, principal):return members.get(principal)
        def authority(self, actor):
            result=self.member(actor.principal_id)
            if result is None:raise ValueError('Membership revoked.')
            return result
    selected={'environment_id':'business','business_scope':Scope()}
    agent=SimpleNamespace(business_scope_host=selected,
        children={'chat':{'id':'chat','owner_principal_id':'owner','dataset':'company'}},
        phone_chat=SimpleNamespace(participant_chat=lambda chat, principal:
            principal=='participant' and principal in members))
    selected['agent']=agent
    entered=[]
    async def prove():
        with pytest.raises(ValueError):
            async with agent_scope(agent,'chat','company',sender={'principal_id':'participant'},admit=admit_root) as root:
                assert root.current()
                if revoked_at=='before_tool':members.pop('participant')
                # Tool calls have the saved chat ID, not a fresh sender argument.
                async with agent_scope(agent,'chat','company') as tool:
                    entered.append(True)
                    await asyncio.sleep(0)
                    members.pop('participant')
                    assert not root.current() and not tool.current()
        assert current_admission() is None
        assert not getattr(agent,'_harness_scope_runs',{})
    asyncio.run(prove())
    assert bool(entered) == (revoked_at=='during_tool')
