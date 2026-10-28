import hashlib,json,os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"deploy/source-intake/demo"))
import pytest
from verify_release import verify,regular

@pytest.fixture
def fixture(tmp_path):
 releases=tmp_path/'releases';release=releases/('a'*40);source=release/'app/src/prototypes/video';source.mkdir(parents=True)
 current=tmp_path/'current';current.symlink_to(release)
 files={}
 for name in ['source_service.py']+[f'module_{n}.py'for n in range(14)]:
  (source/name).write_bytes(b'# pinned source\n');files[name]=hashlib.sha256((source/name).read_bytes()).hexdigest()
 receipt=b'{"synthetic":true}\n';record={'schema_version':1,'source_commit':'a'*40,'release_path':str(release),'files':files,'media_receipt_path':'/opt/rocoto-source-media/readiness-receipt.json','media_receipt_sha256':hashlib.sha256(receipt).hexdigest()}
 manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps(record))
 kwargs={'owner':os.geteuid(),'current':current,'releases':releases,'receipt_reader':lambda _:receipt}
 return manifest,source,record,kwargs

def test_exact_release_provider_free(fixture):
 manifest,source,record,kwargs=fixture
 assert verify(manifest,**kwargs)=={'source_commit':'a'*40,'module_count':15}

def test_switched_web_release_rejected(fixture,tmp_path):
 manifest,source,record,kwargs=fixture
 kwargs['current'].unlink();kwargs['current'].symlink_to(tmp_path)
 with pytest.raises(ValueError,match='releases differ'):verify(manifest,**kwargs)

def test_changed_source_rejected(fixture):
 manifest,source,record,kwargs=fixture;(source/'source_service.py').write_text('# changed')
 with pytest.raises(ValueError,match='digest'):verify(manifest,**kwargs)

def test_extra_module_rejected(fixture):
 manifest,source,record,kwargs=fixture;(source/'extra.py').write_text('# extra')
 with pytest.raises(ValueError,match='inventory'):verify(manifest,**kwargs)

def test_symlink_module_rejected(fixture,tmp_path):
 manifest,source,record,kwargs=fixture;path=source/'module_0.py';data=path.read_bytes();path.unlink();target=tmp_path/'other.py';target.write_bytes(data);path.symlink_to(target)
 with pytest.raises(OSError):verify(manifest,**kwargs)

def test_writable_directory_rejected(fixture):
 manifest,source,record,kwargs=fixture;source.chmod(0o777)
 with pytest.raises(ValueError,match='directory'):verify(manifest,**kwargs)

def test_untrusted_manifest_rejected(fixture):
 manifest,source,record,kwargs=fixture;manifest.chmod(0o666)
 with pytest.raises(ValueError,match='file'):verify(manifest,**kwargs)

def test_changed_receipt_rejected(fixture):
 manifest,source,record,kwargs=fixture;kwargs['receipt_reader']=lambda _:b'changed'
 with pytest.raises(ValueError,match='receipt changed'):verify(manifest,**kwargs)

def test_pin_cannot_add_parent_path(fixture):
 manifest,source,record,kwargs=fixture;record['files']['../bad.py']='a'*64;manifest.write_text(json.dumps(record))
 with pytest.raises(ValueError):verify(manifest,**kwargs)

def test_unit_is_inert_and_scoped():
 text=(Path(__file__).resolve().parents[1]/'deploy/source-intake/demo/unit.template').read_text()
 assert '\n[Install]' not in text
 assert 'ConditionPathExists=/etc/rocoto/source-intake-demo.enabled' in text
 assert 'InaccessiblePaths=-/var/lib/rocoto-video-preview/environments' in text
 assert 'ReadOnlyPaths=/var/lib/rocoto-video-preview/provider-credentials' in text
 assert 'KillSignal=SIGTERM' in text and 'KillMode=mixed' in text
 assert 'EnvironmentFile=' not in text
 assert '/current/' not in text
