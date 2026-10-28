from title_stream import TitleStream


def delta(text, identity="first"):
    return {"type":"progress", "activity":"commentary", "item_id":identity, "text":text}


def test_title_stream_uses_actual_chunks_and_preserves_commentary():
    stream = TitleStream("New chat")
    assert stream.feed(delta("<rocoto_")) == []
    assert stream.feed(delta("title>Checking café")) == [
        {"type":"title", "title":"Checking café", "partial":True}]
    assert stream.feed(delta(" deliveries</roco")) == [
        {"type":"title", "title":"Checking café deliveries", "partial":True}]
    assert stream.feed(delta("to_title> I’ll check the footage.")) == [
        {"type":"title", "title":"Checking café deliveries", "partial":False},
        delta("I’ll check the footage.")]
    assert stream.feed(delta(" Next step.")) == [delta(" Next step.")]


def test_regular_commentary_is_unchanged():
    stream = TitleStream("New chat")
    assert stream.feed(delta("I’ll check the footage.")) == [delta("I’ll check the footage.")]
    assert stream.feed(delta("<rocoto_title>quoted content</rocoto_title>")) == [
        delta("<rocoto_title>quoted content</rocoto_title>")]


def test_stop_reverts_incomplete_title():
    stream = TitleStream("New chat")
    stream.feed(delta("<rocoto_title>Checking"))
    stopped = {"type":"done", "status":"interrupted"}
    assert stream.feed(stopped) == [{"type":"title", "title":"New chat", "partial":False}, stopped]


def test_invalid_titles_cannot_be_persisted():
    for text in ("x" * 81, "bad\nname", "bad\u202ename", ""):
        stream = TitleStream("New chat")
        event = delta("<rocoto_title>" + text + "</rocoto_title>")
        output = stream.feed(event)
        assert all(e.get("type") != "title" for e in output)


def test_title_tool_wins_over_partial_metadata():
    stream = TitleStream("New chat")
    stream.feed(delta("<rocoto_title>Checking"))
    final = {"type":"title", "title":"Café review"}
    assert stream.feed(final) == [final]


def test_reasoning_and_other_items_are_not_title_input():
    stream = TitleStream("New chat")
    thinking = {"type":"progress", "activity":"thinking", "text":"Thinking."}
    assert stream.feed(thinking) == [thinking]
    stream.feed(delta("<rocoto_title>Checking"))
    other = delta("Other activity", "other")
    assert stream.feed(other) == [other]


def test_recorded_turn_persists_completed_title_and_clean_message():
    import asyncio
    from test_agent_controls import agent_fixture
    async def scenario():
        agent=agent_fixture()
        agent.children['new']={'id':'new','title':'New chat','kind':'chat','owner_principal_id':'fixture-owner','messages':[]}
        async def turn(*args,**kwargs):
            assert kwargs['model_context'] == ''
            yield delta('<rocoto_title>Checking')
            yield delta(' café service</rocoto_title>Looking at the footage.')
            yield {'type':'delta','text':'Evidence summary'}
            yield {'type':'done','status':'completed'}
        agent._turn=turn
        events=[event async for event in agent._recorded_turn('Find safety incidents','new','cafe')]
        titles=[e for e in events if e['type']=='title']
        assert [e['partial'] for e in titles]==[True,False]
        assert agent.children['new']['title']=='Checking café service'
        answer=agent.children['new']['messages'][-1]
        assert answer['content']=='Evidence summary'
        assert '<rocoto_title>' not in str(answer)
    asyncio.run(scenario())
