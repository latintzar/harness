"""Toolset migration contracts, using fake RPC and no model calls."""
import asyncio
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from deploy.migration.legacy import native_thread_tools as native
from deploy.migration import history as migration_history


START = {'cwd':'/private/state','baseInstructions':'Current base',
         'developerInstructions':'Current developer','sandbox':'read-only',
         'approvalPolicy':'never','dynamicTools':[{'type':'function','name':'new_tool',
             'description':'Current tool','inputSchema':{'type':'object'}}]}


class Connection:
    def __init__(self, items=(), *, failure=None, status='completed'):
        self.calls=[]
        self.original={'id':'old','turns':[{'id':'turn-old','status':status,'items':list(items)}]}
        self.failure=failure

    async def rpc(self, method, params):
        self.calls.append((method,copy.deepcopy(params)))
        if method==self.failure:
            raise RuntimeError('RPC failed')
        if method=='thread/read':return {'thread':copy.deepcopy(self.original)}
        if method=='thread/start':return {'thread':{'id':'new','turns':[]}}
        if method=='thread/resume':return {'thread':{'id':'old','turns':[]}}
        if method=='thread/inject_items':return {}
        raise AssertionError(method)


def run(connection, existing='old', known=None, start=None):
    return asyncio.run(native.open_native_thread(connection,existing_id=existing,
        known_toolset=known,toolset_digest='v2',start_params=start or START))


def test_portable_history_continues_without_reading_old_host_thread(tmp_path):
    connection=Connection([{'type':'userMessage','id':'u','content':[{'type':'text','text':'Prior question'}]},
        {'type':'agentMessage','id':'a','text':'Prior evidence answer','phase':'final_answer'},
        {'type':'reasoning','id':'private','text':'excluded'}])
    connection.original['cwd']=str(tmp_path)
    export=native.history_export(connection.original,state=tmp_path,chat_id='chat-a',owner_principal_id='owner-a')
    path=native.history_export_path(tmp_path,'chat-a');path.parent.mkdir();path.write_text(json.dumps(export));path.chmod(0o600)
    loaded=native.load_history_export(tmp_path,chat_id='chat-a',existing_id='old',owner_principal_id='owner-a')
    result=asyncio.run(native.open_native_thread(connection,existing_id='old',known_toolset='v2',
        toolset_digest='v2',start_params=START,migration_export=loaded))
    assert [method for method,_ in connection.calls]==['thread/start','thread/inject_items']
    assert result['copied_messages']==2 and result['migrated_from']=='old'
    assert native.load_history_export(tmp_path,chat_id='chat-a',existing_id='new',owner_principal_id='owner-a') is None
    with pytest.raises(ValueError):native.load_history_export(tmp_path,chat_id='chat-a',existing_id='old',owner_principal_id='other')


def test_new_thread_and_same_toolset_resume_have_no_migration():
    fresh=Connection();result=run(fresh,existing=None)
    assert fresh.calls==[('thread/start',START)]
    assert result['thread']['id']=='new' and result['migrated_from'] is None
    resumed=Connection();result=run(resumed,known='v2')
    assert resumed.calls==[('thread/resume',{'threadId':'old',
        'baseInstructions':'Current base','developerInstructions':'Current developer',
        'cwd':'/private/state','sandbox':'read-only','approvalPolicy':'never'})]
    assert result['thread']['id']=='old' and result['copied_messages']==0


def test_migration_copies_exact_public_messages_and_preserves_original():
    items=[{'type':'userMessage','id':'u1','content':[{'type':'text','text':'Exact\nquestion'}]},
           {'type':'reasoning','id':'hidden','text':'Never copy this'},
           {'type':'dynamicToolCall','id':'tool','tool':'old_tool','arguments':{}},
           {'type':'agentMessage','id':'a1','text':'Exact answer.','phase':'final_answer'}]
    connection=Connection(items);before=copy.deepcopy(connection.original)
    result=run(connection)
    assert [x[0] for x in connection.calls]==['thread/read','thread/start','thread/inject_items']
    assert connection.calls[0][1]=={'threadId':'old','includeTurns':True}
    assert connection.calls[2][1]=={'threadId':'new','items':[
        {'type':'message','role':'user','content':[{'type':'input_text','text':'Exact\nquestion'}]},
        {'type':'message','role':'assistant','phase':'final_answer',
         'content':[{'type':'output_text','text':'Exact answer.'}]}]}
    assert connection.original==before and START['dynamicTools'][0]['name']=='new_tool'
    assert result['migrated_from']=='old' and result['copied_messages']==2
    assert result['omissions']['excluded_item_types']=={'reasoning':1,'dynamicToolCall':1}


def test_failed_read_or_injection_never_returns_new_thread():
    item={'type':'userMessage','id':'u','content':[{'type':'text','text':'History'}]}
    for failure in ['thread/read','thread/inject_items']:
        connection=Connection([item],failure=failure)
        with pytest.raises(RuntimeError,match='RPC failed'):run(connection)
        assert all(method not in ['thread/archive','thread/delete'] for method,_ in connection.calls)
        if failure=='thread/read':assert len(connection.calls)==1


def test_active_history_fails_before_starting_replacement():
    connection=Connection(status='inProgress')
    with pytest.raises(ValueError,match='active'):run(connection)
    assert [x[0] for x in connection.calls]==['thread/read']


def test_history_limit_copies_newest_whole_messages_with_explicit_omissions():
    items=[{'type':'userMessage','id':str(i),'content':[{'type':'text','text':f'Message{i}'}]} for i in range(4)]
    connection=Connection(items)
    with patch.object(migration_history,'MAX_MESSAGES',2):result=run(connection)
    copied=connection.calls[-1][1]['items']
    assert [x['content'][0]['text'] for x in copied]==['Message2','Message3']
    assert result['omissions']['public_messages']==2
    assert result['omissions']['reasons']['history_budget']==2


def test_local_images_use_owned_attachment_bytes_and_record_provenance(tmp_path):
    attachments=tmp_path/'attachments';attachments.mkdir()
    image=attachments/'photo.png';raw=b'\x89PNG\r\n\x1a\nfixture';image.write_bytes(raw)
    remote='https://example.com/prior-image.png'
    items=[{'type':'userMessage','id':'u','content':[
        {'type':'text','text':'Explain these'},
        {'type':'localImage','path':str(image),'detail':'original'},
        {'type':'image','url':remote},
        {'type':'localImage','path':str(tmp_path/'missing.png')}]}]
    connection=Connection(items);result=run(connection,start={**START,'cwd':str(tmp_path)})
    content=connection.calls[-1][1]['items'][0]['content']
    assert content[1]=={'type':'input_image','image_url':'data:image/png;base64,'+base64.b64encode(raw).decode(),'detail':'original'}
    assert content[2]=={'type':'input_image','image_url':remote}
    proof=result['image_provenance'][0]
    assert proof['sha256']==hashlib.sha256(raw).hexdigest()
    assert proof['historical_bytes_verified'] is False
    assert result['omissions']['image_parts']==1


def test_missing_or_outside_attachment_is_reported_without_reading_it(tmp_path):
    outside=tmp_path/'secret.png';outside.write_bytes(b'not an attachment')
    connection=Connection([{'type':'userMessage','id':'u','content':[
        {'type':'text','text':'Prior image'}, {'type':'localImage','path':str(outside)}]}])
    result=run(connection,start={**START,'cwd':str(tmp_path)})
    assert connection.calls[-1][1]['items'][0]['content']==[{'type':'input_text','text':'Prior image'}]
    assert result['omissions']['reasons']['local_image_outside_attachments']==1


def test_invalid_public_history_fails_before_new_thread():
    connection=Connection([{'type':'agentMessage','id':'bad','text':17}])
    with pytest.raises(ValueError,match='text'):run(connection)
    assert len(connection.calls)==1


def test_text_budget_preserves_exact_newest_suffix_without_truncation():
    connection=Connection([{'type':'userMessage','id':str(i),'content':[{'type':'text','text':text}]}
                           for i,text in enumerate(['Earlier','Oversized middle','Latest'])])
    with patch.object(migration_history,'MAX_TEXT_BYTES',8):result=run(connection)
    assert connection.calls[-1][1]['items']==[
        {'type':'message','role':'user','content':[{'type':'input_text','text':'Latest'}]}]
    assert result['omissions']['public_messages']==2


def test_attachment_directory_symlink_cannot_escape_company_state(tmp_path):
    state=tmp_path/'company';state.mkdir()
    outside=tmp_path/'outside';outside.mkdir()
    (state/'attachments').symlink_to(outside,target_is_directory=True)
    image=outside/'image.png';image.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
    connection=Connection([{'type':'userMessage','id':'u','content':[
        {'type':'text','text':'Context'}, {'type':'localImage','path':str(image)}]}])
    result=run(connection,start={**START,'cwd':str(state)})
    assert result['omissions']['image_parts']==1
    assert result['image_provenance']==[]
