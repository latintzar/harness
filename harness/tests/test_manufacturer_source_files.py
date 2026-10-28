"""Source price files enter native shared Chat and the same immutable configuration history."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_native_pdf_csv_upload_agent_price_edit_shared_history_and_scope(tmp_path):
    script = r'''
import asyncio, hashlib, io, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
import layout
layout.install('manufacturer')
import server, manufacturer_tools
from fastapi.testclient import TestClient
import fitz
from tool_families import ToolContext, ToolRegistry
state=Path(sys.argv[2]);app=server.create_app(state)
app.state.access.create_environment('other','Other company')
headers={'X-Rocoto-Page':'1'}
with TestClient(app) as client:
    def login(env,person,owner=False):
        code=app.state.access.issue(env,principal_id=person,owner=owner)
        result=client.post('/api/session',json={'code':code},headers=headers)
        assert result.status_code==200,result.text
    assert client.post('/api/manufacturer/files',files={'file':('prices.csv',b'code,price\n101,12.50')},headers=headers).status_code==401
    login('demo','alice',True)
    page=client.get('/')
    assert page.status_code==200,page.text
    policy=page.headers['content-security-policy']
    expected="default-src 'self'; img-src 'self' data: https://raw.githubusercontent.com https://orders-demo.soyrocoto.com; media-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'self'"
    assert policy==expected,policy
    assert page.headers['referrer-policy']=='no-referrer'
    api=client.get('/api/manufacturer/overview')
    assert 'https://orders-demo.soyrocoto.com' not in api.headers['content-security-policy']
    raw=io.BytesIO();pdf=fitz.open();page=pdf.new_page();page.insert_text((30,30),'Article 101 - EUR 12.50 per piece - net price');pdf.save(raw);pdf.close()
    response=client.post('/api/manufacturer/files',files={'file':('New prices.pdf',raw.getvalue(),'application/pdf')},headers=headers)
    assert response.status_code==200,response.text
    uploaded=response.json();original=uploaded['file'];reading=uploaded['read_file'];chat_id=uploaded['chat_id']
    assert original['sha256']==hashlib.sha256(raw.getvalue()).hexdigest()
    assert uploaded['extraction']['status']=='ready',uploaded
    text=client.get('/api/files/'+reading['id']+'/content').json()['content']
    assert 'EUR 12.50 per piece' in text
    selected=app.state.workspaces['demo'];ext=selected['vertical'];chat=selected['agent'].children[chat_id]
    assert selected['organization_chats'].canonical(chat)
    assert original['id'] in {f['id'] for f in selected['files'].list(owner_principal_id=selected['organization_chats'].owner,chat_id=chat_id,dataset='company')}
    # Agent edits and manual edits invoke the same store and preserve the original source reference.
    current=ext.configurator('alice');document=current['document']
    document['catalogue']['cats']=[{'code':'locks','items':[{'c':'101','p':12.5,'q':0,'u':'pc'}]}]
    document['attachments']=[{'file_id':original['id'],'name':'forged.pdf','sha256':'0'*64,'purpose':'price_list'}]
    run={'cancel':asyncio.Event()};test_agent=SimpleNamespace(active={chat_id:run})
    context=ToolContext(run=run,agent=test_agent,state=selected['state'],company_id=ext.company_id,actor='alice',data_owner=chat['owner_principal_id'],collection='company',chat_id=chat_id,record=chat,extension=ext,admission=ext.read_admission('alice','company'))
    saved=asyncio.run(ToolRegistry([manufacturer_tools]).invoke(context,'manufacturer_save_configurator',{'document':document,'expected_revision':0,'evidence':'Supplied PDF states EUR 12.50 net per piece','label':'New supplier prices'}))
    assert saved['revision']==1
    attachment=saved['document']['attachments'][0]
    assert attachment['sha256']==original['sha256'] and attachment['name']=='New prices.pdf'
    manual=client.post('/api/manufacturer/configurator',json={'settings':{'accent':'#235577'},'expected_revision':1},headers=headers)
    assert manual.status_code==200,manual.text
    assert manual.json()['document']['attachments']==saved['document']['attachments']
    history=client.get('/api/manufacturer/configurator/versions').json()['versions']
    assert [v['author']['type'] for v in history][:2]==['user','agent']
    restored=client.post('/api/manufacturer/configurator/versions/'+saved['version_id']+'/restore',json={'expected_revision':2},headers=headers)
    assert restored.status_code==200,restored.text
    assert restored.json()['document']==saved['document']
    # Another real team member reads the same source; a different company cannot.
    login('demo','bob')
    assert client.get('/api/files/'+reading['id']+'/content').status_code==200
    source=client.post('/api/manufacturer/files',files={'file':('prices.csv',b'code,price,currency\n101,15.00,EUR','text/csv')},headers=headers)
    assert source.status_code==200,source.text
    assert source.json()['chat_id']==chat_id
    login('other','carol')
    assert client.get('/api/files/'+original['id']).status_code==404
    other=client.get('/api/manufacturer/configurator').json()['document']
    other['attachments']=[{'file_id':original['id']}]
    denied=client.post('/api/manufacturer/configurator',json={'document':other,'expected_revision':0},headers=headers)
    assert denied.status_code==400,denied.text
    bad=client.post('/api/manufacturer/files',files={'file':('fake.pdf',b'not a PDF','application/pdf')},headers=headers)
    assert bad.status_code==400,bad.text
    # The full file allowance remains available alongside the multipart overhead.
    boundary=client.post('/api/manufacturer/files',files={'file':('boundary.txt',b'a'*(16*1024*1024),'text/plain')},headers=headers)
    assert boundary.status_code==200,boundary.text
    oversized=client.post('/api/manufacturer/files',files={'file':('large.txt',b'a'*(16*1024*1024+1),'text/plain')},headers=headers)
    assert oversized.status_code==413,oversized.text
    # Declared oversize is refused before reading; chunked multipart has the same bound.
    oversized=client.post('/api/manufacturer/files',content=b'',headers={**headers,'Content-Type':'multipart/form-data; boundary=source','Content-Length':str(17*1024*1024)})
    assert oversized.status_code==413,oversized.text
    def chunks():
        yield b'--source\r\nContent-Disposition: form-data; name="file"; filename="prices.txt"\r\nContent-Type: text/plain\r\n\r\n'
        for _ in range(17):
            yield b'a'*(1024*1024)
        yield b'\r\n--source--\r\n'
    oversized=client.post('/api/manufacturer/files',content=chunks(),headers={**headers,'Content-Type':'multipart/form-data; boundary=source'})
    assert oversized.status_code==413,oversized.text
print('source-files-shared-history-ok')
'''
    environment = {**os.environ, 'ROCOTO_VERTICAL': 'manufacturer', 'PYTHONDONTWRITEBYTECODE': '1'}
    environment.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-B', '-c', script, str(Path(__file__).resolve().parents[1]), str(tmp_path)], env=environment, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'source-files-shared-history-ok' in result.stdout


def test_stream_limit_stops_before_forwarding_oversize_chunk(monkeypatch):
    import asyncio
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'verticals' / 'manufacturer'))
    from manufacturer_source_files import UploadRequestLimit
    forwarded = []
    sent = []
    chunks = iter([b'a' * 1024 * 1024] * 18)
    async def receive():
        return {'type': 'http.request', 'body': next(chunks), 'more_body': True}
    async def send(message):
        sent.append(message)
    async def parser(scope, incoming, outgoing):
        while True:
            forwarded.append(await incoming())
    scope = {'type': 'http', 'method': 'POST', 'path': '/api/manufacturer/files', 'headers': []}
    asyncio.run(UploadRequestLimit(parser)(scope, receive, send))
    assert sent[0]['status'] == 413
    assert sum(len(part['body']) for part in forwarded) == 16 * 1024 * 1024
    assert len(forwarded) == 16
