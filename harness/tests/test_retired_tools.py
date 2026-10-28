"""The live tool boundary rejects retired work before any source/model dispatch."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]))
from types import SimpleNamespace
import pytest
from agent_bridge import CodexVideoAgent,tool_specs
from native_mcp import NativeMCP
from public_mcp import ReadTools
from retired_tools import RETIRED_TOOLS
from monitoring_tools import READ_NAMES
from test_native_mcp import fixture,client


def test_parent_and_child_advertise_annotations_instead_of_archive_tools():
    for children in (True,False):
        names={s['name'] for s in tool_specs(children)}
        assert READ_NAMES<=names and not names&RETIRED_TOOLS


@pytest.mark.parametrize('name',sorted(RETIRED_TOOLS))
def test_agent_dispatch_refuses_retired_name_before_source_or_model_access(name):
    result=asyncio.run(CodexVideoAgent._tool(SimpleNamespace(),name,{},'cafe','root'))
    assert result[2] is False and 'list_tracker_events' in result[0][0]['text']
    with pytest.raises(ValueError,match='list_tracker_events'):
        ReadTools.call(SimpleNamespace(),None,name,{})


def test_native_descendant_reads_events_and_rejects_every_retired_entrypoint():
    async def run():
        agent,scope,calls=fixture();app=NativeMCP(agent,scope,company_id='rocoto')
        grant=app.issue(actor_chat_id='child',actor_thread_id='native-child')
        async with app.lifespan(),client(app,grant.token) as session:
            assert READ_NAMES<={t.name for t in (await session.list_tools()).tools}
            assert not (await session.call_tool('list_tracker_events',{})).isError
            for name in RETIRED_TOOLS:
                assert (await session.call_tool(name,{})).isError
        assert [r[0] for r in calls]==['list_tracker_events'] and calls[0][3]=='child'
    asyncio.run(run())
