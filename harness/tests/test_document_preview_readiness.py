import hashlib,json,os,shutil
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parents[1]))
import pytest
from fastapi.testclient import TestClient
import document_preview_contract as contract
import document_preview_readiness as readiness
from test_server_access import fixture_app,FixtureAgent
from test_workflow_routes import login,HEADERS

def sha(value):return hashlib.sha256(value).hexdigest()

@pytest.fixture
def commissioned(tmp_path):
    root=tmp_path/'installed';root.mkdir()
    closure=Path(__file__).parents[1]/'deploy/document-preview/closure'
    for name in readiness.SOURCE_PINS:shutil.copyfile(closure/name,root/name)
    worker=sha(Path(readiness.__file__).with_name('document_preview_worker.py').read_bytes())
    value={'schema_version':1,'purpose':contract.PURPOSE,'profile':contract.PROFILE,'recipe':contract.RECIPE,'image_digest':contract.IMAGE,
        'worker_sha256':worker,'preview_uid':os.geteuid(),'preview_gid':os.getegid(),'provider_calls':0,
        'installed_proof_sha256':'a'*64,'private_runtime_proof_sha256':'b'*64,
        'sources':[{'path':name,'sha256':digest} for name,digest in readiness.SOURCE_PINS.items()]}
    path=root/'document-preview-readiness.json';path.write_text(json.dumps(value));path.chmod(0o644)
    probe=readiness.DocumentPreviewReadiness(enabled=True,receipt_sha256=sha(path.read_bytes()),receipt_path=path,
        reader=lambda file:readiness.root_bytes(file,uid=os.getuid()))
    return probe,value

def test_disabled_missing_pin_and_modified_root_source_fail_closed(commissioned):
    probe,value=commissioned
    assert probe.ready()
    probe.enabled=False;assert not probe.ready();probe.enabled=True
    exact=probe.receipt_sha256;probe.receipt_sha256='';assert not probe.ready();probe.receipt_sha256=exact
    (probe.path.parent/'document_preview_contract.py').write_text('changed deployed source')
    assert not probe.ready()

@pytest.mark.parametrize('field,new',[('profile','other'),('recipe','other'),('image_digest','sha256:'+'0'*64),('worker_sha256','0'*64),('preview_uid',-1),('installed_proof_sha256','bad'),('schema_version',True)])
def test_pinned_receipt_still_requires_exact_runtime_contract(commissioned,field,new):
    probe,value=commissioned;value[field]=new;probe.path.write_text(json.dumps(value));probe.receipt_sha256=sha(probe.path.read_bytes())
    assert not probe.ready()

def test_declared_source_cannot_add_paths_or_duplicate_another_source(commissioned):
    probe,value=commissioned
    value['sources'][-1]={'path':'/etc/passwd','sha256':'a'*64}
    probe.path.write_text(json.dumps(value));probe.receipt_sha256=sha(probe.path.read_bytes());assert not probe.ready()
    value['sources'][-1]=value['sources'][0]
    probe.path.write_text(json.dumps(value));probe.receipt_sha256=sha(probe.path.read_bytes());assert not probe.ready()

def test_reader_rejects_symlinks_hardlinks_writable_receipts_and_wrong_owner(commissioned):
    probe,value=commissioned
    probe.path.chmod(0o666);assert not probe.ready();probe.path.chmod(0o644)
    alias=probe.path.with_name('alias');alias.symlink_to(probe.path)
    with pytest.raises(OSError):readiness.root_bytes(alias,uid=os.getuid())
    alias.unlink();os.link(probe.path,alias);assert not probe.ready();alias.unlink()
    with pytest.raises(ValueError):readiness.root_bytes(probe.path,uid=os.getuid()+1)
    assert probe.ready()

@pytest.mark.parametrize('environment',[{}, {'ROCOTO_DOCUMENT_PREVIEW_ENABLED':'1'}, {'ROCOTO_DOCUMENT_PREVIEWS_ENABLED':'1'}])
def test_server_keeps_conversion_closed_until_plural_flag_and_verified_receipt(tmp_path,monkeypatch,environment):
    for name in ['ROCOTO_DOCUMENT_PREVIEW_ENABLED','ROCOTO_DOCUMENT_PREVIEWS_ENABLED','ROCOTO_DOCUMENT_PREVIEW_RECEIPT_SHA256']:monkeypatch.delenv(name,raising=False)
    for name,value in environment.items():monkeypatch.setenv(name,value)
    app=fixture_app(tmp_path,monkeypatch)
    record=FixtureAgent.instances['demo'].file_store.create({'name':'Generated.docx','content':'# Exact source'},owner_principal_id='alice',chat_id='root',dataset='cafe')
    with TestClient(app) as client:
        login(client,app,'alice')
        response=client.get('/api/files/'+record['id']+'/page-preview')
        assert response.status_code==200 and response.json()['file']['page_preview_available'] is False
        assert client.post('/api/files/'+record['id']+'/preview',json={},headers=HEADERS).status_code==503


def test_server_refreshes_readiness_before_every_request(tmp_path,monkeypatch,commissioned):
    import server
    probe,value=commissioned
    monkeypatch.setenv('ROCOTO_DOCUMENT_PREVIEWS_ENABLED','1');monkeypatch.setenv('ROCOTO_DOCUMENT_PREVIEW_RECEIPT_SHA256',probe.receipt_sha256)
    seen=[]
    def factory(**kwargs):seen.append(kwargs);return probe
    monkeypatch.setattr(server,'DocumentPreviewReadiness',factory)
    app=fixture_app(tmp_path,monkeypatch)
    assert seen==[{'enabled':True,'receipt_sha256':probe.receipt_sha256}]
    record=FixtureAgent.instances['demo'].file_store.create({'name':'Generated.docx','content':'# Exact source'},owner_principal_id='alice',chat_id='root',dataset='cafe')
    with TestClient(app) as client:
        login(client,app,'alice');url='/api/files/'+record['id']+'/page-preview'
        assert client.get(url).json()['file']['page_preview_available'] is True
        (probe.path.parent/'source_media.py').write_text('changed')
        assert client.get(url).json()['file']['page_preview_available'] is False
        assert client.post('/api/files/'+record['id']+'/preview',json={},headers=HEADERS).status_code==503
