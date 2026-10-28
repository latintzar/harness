"""The fixed SFTP driver shares the funded intake gate with browser uploads."""
import pytest

from test_source_host_routes import commission_fixture
from source_intake import SourceStore, IntakeActor, IntakeError
from source_service import SourceHost


def setup(tmp_path):
    host=commission_fixture(tmp_path)
    store=SourceStore(tmp_path);actor=IntakeActor('alice')
    source=store.ensure_connection_source(actor,collection='cafe',connection_id='custom-fixture',
        target_sha256='a'*64,name='Recording archive',kind='sftp')
    return host,store,actor,source


def begin(host,store,actor,source,**kwargs):
    body={'filename':'clip.mp4','size':8,'idempotency_key':'sftp-version-one',
        'object_key':'clip.mp4','source_version':'b'*64,'current':lambda:True}
    return host.begin_remote(store,actor,source['id'],**{**body,**kwargs})


def test_remote_reservation_binds_object_version_and_survives_retry(tmp_path):
    host,store,actor,source=setup(tmp_path)
    upload=begin(host,store,actor,source)
    assert upload['source_id']==source['id']
    receipt=host.records()[0]
    assert receipt['binding']['remote']=={'object_key':'clip.mp4','source_version':'b'*64,'kind':'sftp'}
    (host.root/'heartbeat.json').unlink()
    assert begin(host,store,actor,source)==upload
    with pytest.raises(IntakeError): begin(host,store,actor,source,source_version='c'*64)
    with pytest.raises(IntakeError): begin(host,store,actor,source,current=lambda:False)
    assert len(host.records())==1


def test_remote_admission_checks_funding_actor_and_source_kind(tmp_path):
    host,store,actor,source=setup(tmp_path)
    with pytest.raises(IntakeError): begin(host,store,IntakeActor('bob'),source)
    with pytest.raises(IntakeError): begin(host,store,IntakeActor('alice',read_only=True),source)
    upload_source=store.ensure_upload_source(actor,collection='cafe')
    with pytest.raises(IntakeError): begin(host,store,actor,upload_source)
    (host.root/'heartbeat.json').unlink()
    with pytest.raises(IntakeError): begin(host,store,actor,source)
    assert host.records()==[]
    assert store.list_imports(actor)==[]


def test_second_remote_version_cannot_reuse_single_file_allowance(tmp_path):
    host,store,actor,source=setup(tmp_path)
    begin(host,store,actor,source)
    with pytest.raises(IntakeError,match='embedding_allowance_reserved'):
        begin(host,store,actor,source,idempotency_key='second-version',source_version='c'*64)
    assert len(store.list_imports(actor))==1


def test_background_recovery_waits_for_remote_authority_to_resume(tmp_path,monkeypatch):
    host,store,actor,source=setup(tmp_path)
    original=store.begin_upload
    def interrupted(*args,**kwargs): raise RuntimeError('Synthetic process loss')
    monkeypatch.setattr(store,'begin_upload',interrupted)
    with pytest.raises(RuntimeError): begin(host,store,actor,source)
    assert host.records()[0]['status']=='reserving'
    monkeypatch.setattr(store,'begin_upload',original)
    host.recover_admissions(store)
    assert store.list_imports(actor)==[]
    with pytest.raises(IntakeError): begin(host,store,actor,source,current=lambda:False)
    assert begin(host,store,actor,source)['status']=='uploading'


def test_stop_before_transfer_releases_proven_unused_allowance(tmp_path):
    host,store,actor,source=setup(tmp_path)
    checks=iter([True,True,False])
    with pytest.raises(IntakeError,match='acquisition_stopped'):
        begin(host,store,actor,source,current=lambda:next(checks))
    assert store.list_imports(actor)==[]
    assert host.records()==[]
    assert begin(host,store,actor,source)['status']=='uploading'


def test_lost_reply_after_committed_transfer_retains_allowance(tmp_path,monkeypatch):
    host,store,actor,source=setup(tmp_path)
    original=store.begin_upload
    def interrupted(*args,**kwargs):
        original(*args,**kwargs)
        raise IntakeError('acquisition_stopped')
    monkeypatch.setattr(store,'begin_upload',interrupted)
    with pytest.raises(IntakeError):begin(host,store,actor,source)
    assert len(store.list_imports(actor))==1
    assert len(host.records())==1
    monkeypatch.setattr(store,'begin_upload',original)
    assert begin(host,store,actor,source)['id']==store.list_imports(actor)[0]['upload']['id']
