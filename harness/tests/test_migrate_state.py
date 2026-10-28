"""Offline fixtures prove the migration without using provider or host access."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest
import numpy as np

sys.path.insert(0,str(Path(__file__).parents[1]))
sys.path.insert(0,str(Path(__file__).parents[1]/'deploy'))
from cosmos_retrieval import MODEL_ID, MODEL_REVISION, generation_paths
from workspace_identity import workspace_scope
from migrate_state import build_snapshot, install_snapshot, verify_snapshot


def write(path,data):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    path.write_text(json.dumps(data));path.chmod(0o600)


def fixture(tmp_path):
    source=tmp_path/'dev';workspace_scope(source);workspace_scope(source/'cosmos')
    video=source/'media'/'cafe'/'original.mp4';video.parent.mkdir(parents=True);video.write_bytes(b'original-evidence')
    thumbnail=source/'cosmos'/'thumbnails'/'clip-a.jpg';thumbnail.parent.mkdir();thumbnail.write_bytes(b'jpeg')
    write(source/'cosmos'/'clips.json',[{'id':'clip-a','video_id':'asset-a','dataset':'cafe','camera':'camera-a',
        'path':str(video),'thumbnail_path':str(thumbnail),'start':1.25,'end':4.75,'embedded_clip_sha256':'same-vector-source','model':MODEL_ID,'model_revision':MODEL_REVISION}])
    write(source/'cosmos'/'index-report.json',{'clip_count':1,'model':MODEL_ID,'model_revision':MODEL_REVISION,'embedding_dimensions':256})
    np.save(source/'cosmos'/'embeddings.npy',np.ones((1,256),dtype=np.float32)/16,allow_pickle=False)
    write(source/'cosmos'/'index-generation.json',{'schema_version':1,'generation_id':'original-generation','model':MODEL_ID,'model_revision':MODEL_REVISION,'dimensions':256,'clip_count':1,'files':{
        name:{'sha256':hashlib.sha256((source/'cosmos'/name).read_bytes()).hexdigest(),
              'bytes':(source/'cosmos'/name).stat().st_size}
        for name in ['clips.json','index-report.json','embeddings.npy']}})
    write(source/'cosmos'/'qdrant-index.json',{'collection':'old-process-index'})
    write(source/'descriptions'/'clip-a.json',[{'text':'Exact historical answer','source_path':str(video),'created_at':'2026-10-01'}])
    write(source/'chats'/'chat-a.json',{'id':'chat-a','owner_principal_id':'principal-a','running':False,
        'messages':[{'text':'Exact old path '+str(video)}]})
    write(source/'agent-chats.json',{})
    (source/'qa-code.txt').write_text('excluded-code')
    write(source/'.codex'/'auth.json',{'token':'excluded-token'})
    write(source/'compute'/'query-approval.json',{'immutable_authority':True})
    (source/'compute'/'staging-device-login.txt').write_text('excluded-login')
    write(source/'billing'/'openai-api'/'funding.json',{'target_is_authoritative':True})
    (source/'modal-pilot').mkdir();(source/'modal-pilot'/'text-encoder.pt').write_bytes(b'large-duplicate')
    (source/'media'/'unindexed.bin').write_bytes(b'not-an-imported-source')
    child=source/'environments'/'customer-a';workspace_scope(child)
    database=sqlite3.connect(child/'business-context.sqlite')
    database.execute('PRAGMA journal_mode=WAL')
    database.execute('CREATE TABLE business_revisions(body TEXT)')
    database.execute('INSERT INTO business_revisions VALUES (?)',(json.dumps({'historical_path':str(video),'revision':7}),))
    database.commit()
    return source,database


def test_snapshot_backs_up_wal_relocates_live_paths_and_preserves_evidence(tmp_path):
    source,db=fixture(tmp_path);bundle=tmp_path/'bundle';target=tmp_path/'staging'
    with pytest.raises(ValueError,match='quiesced'):build_snapshot(source,bundle,target)
    manifest=build_snapshot(source,bundle,target,quiesced=True)
    copied=bundle/'state';paths=generation_paths(copied/'cosmos');record=json.loads(paths['clips.json'].read_text())[0]
    assert record['path']==str(target/'media/cafe/original.mp4')
    assert (record['start'],record['end'])==(1.25,4.75)
    for rel in ['media/cafe/original.mp4','descriptions/clip-a.json','chats/chat-a.json']:
        assert (copied/rel).read_bytes()==(source/rel).read_bytes()
    assert paths['embeddings.npy'].read_bytes()==(source/'cosmos/embeddings.npy').read_bytes()
    newdb=sqlite3.connect((copied/'environments/customer-a/business-context.sqlite').as_uri()+'?mode=ro&immutable=1',uri=True)
    assert newdb.execute('SELECT body FROM business_revisions').fetchall()==db.execute('SELECT body FROM business_revisions').fetchall()
    assert json.loads((copied/'.workspace-identity.json').read_text())['company_scope']==workspace_scope(source)
    assert not (copied/'.codex').exists()
    assert not (copied/'qa-code.txt').exists()
    assert not (copied/'compute/staging-device-login.txt').exists()
    assert not (copied/'billing/openai-api').exists()
    assert not (copied/'cosmos/qdrant-index.json').exists()
    assert not (copied/'modal-pilot/text-encoder.pt').exists()
    assert not (copied/'media/unindexed.bin').exists()
    marker=json.loads((copied/'cosmos/index-generation.json').read_text())
    assert marker['generation_id']!='original-generation'
    assert marker['files']['clips.json']['sha256']==hashlib.sha256(paths['clips.json'].read_bytes()).hexdigest()
    assert manifest['sources'][0]['sha256']==hashlib.sha256(b'original-evidence').hexdigest()
    assert verify_snapshot(bundle)['verified'] is True
    db.close()


def test_stopped_sqlite_snapshot_preserves_source_files_exactly(tmp_path):
    import migrate_state as migration
    source,db=fixture(tmp_path);db.close()
    before={path.relative_to(source):(migration._stamp(path),path.read_bytes())
        for path in source.rglob('*') if path.is_file()}
    bundle=tmp_path/'bundle'
    build_snapshot(source,bundle,tmp_path/'target',quiesced=True)
    assert {path.relative_to(source):(migration._stamp(path),path.read_bytes())
        for path in source.rglob('*') if path.is_file()}==before
    with sqlite3.connect(bundle/'state/environments/customer-a/business-context.sqlite') as copied:
        assert json.loads(copied.execute('SELECT body FROM business_revisions').fetchone()[0])['revision']==7


@pytest.mark.parametrize('writer_open',[False,True])
def test_idle_inspection_uses_private_sqlite_copy_with_committed_wal(tmp_path,monkeypatch,writer_open):
    import migrate_state as migration
    source=tmp_path/'source';source.mkdir()
    path=source/'background-agents.sqlite'
    with sqlite3.connect(path) as setup:
        setup.execute('PRAGMA journal_mode=WAL')
        setup.execute('CREATE TABLE agents(body TEXT)')
        setup.execute('CREATE TABLE runs(body TEXT)')
    setup.close()
    writer=sqlite3.connect(path)
    writer.execute('INSERT INTO runs VALUES(?)',(json.dumps({'status':'running'}),));writer.commit()
    assert Path(str(path)+'-wal').stat().st_size>0
    if not writer_open:writer.close()
    before={item.name:(migration._stamp(item),item.read_bytes()) for item in source.iterdir()}
    original=sqlite3.connect
    def private_connect(database,*args,**kwargs):
        assert not str(database).startswith((str(source),source.as_uri())), 'SQLite opened original state'
        return original(database,*args,**kwargs)
    monkeypatch.setattr(migration.sqlite3,'connect',private_connect)
    with pytest.raises(ValueError,match='Pause active'):
        migration._check_idle(source)
    assert {item.name:(migration._stamp(item),item.read_bytes()) for item in source.iterdir()}==before
    if writer_open:writer.close()


@pytest.mark.parametrize('change_after_copy',[False,True])
def test_sqlite_snapshot_rejects_real_writer_during_copy_or_backup(tmp_path,monkeypatch,change_after_copy):
    import migrate_state as migration
    source,db=fixture(tmp_path)
    database=source/'environments/customer-a/business-context.sqlite'
    original_copy=migration.shutil.copy2
    changed=False
    def changing_copy(path,*args,**kwargs):
        nonlocal changed
        result=original_copy(path,*args,**kwargs)
        selected=(Path(path)==source/'media/cafe/original.mp4' if change_after_copy else Path(path)==database)
        if selected and not changed:
            changed=True
            db.execute('INSERT INTO business_revisions VALUES(?)',(json.dumps({'revision':8}),));db.commit()
        return result
    monkeypatch.setattr(migration.shutil,'copy2',changing_copy)
    with pytest.raises(ValueError,match='changed'):
        build_snapshot(source,tmp_path/'bundle',tmp_path/'target',quiesced=True)
    assert changed and not (tmp_path/'bundle/manifest.json').exists()
    db.close()


def test_install_preserves_commissioned_api_pool_and_rejects_tampered_snapshot(tmp_path):
    source,db=fixture(tmp_path);bundle=tmp_path/'bundle';target=tmp_path/'staging'
    build_snapshot(source,bundle,target,quiesced=True)
    write(target/'billing/openai-api/funding.json',{'funded':'40'})
    write(target/'.codex/auth.json',{'token':'host-auth-untouched'})
    before=(target/'billing/openai-api/funding.json').read_bytes()
    install_snapshot(bundle,target,quiesced=True)
    assert (target/'billing/openai-api/funding.json').read_bytes()==before
    assert json.loads((target/'.codex/auth.json').read_text())['token']=='host-auth-untouched'
    assert verify_snapshot(bundle,installed_root=target)['verified'] is True
    (bundle/'state/media/cafe/original.mp4').write_bytes(b'tampered')
    with pytest.raises(ValueError,match='checksum'):verify_snapshot(bundle)
    db.close()


def test_unknown_external_source_or_symlink_and_running_chat_stop_snapshot(tmp_path):
    source,db=fixture(tmp_path)
    chat=source/'chats/chat-a.json';body=json.loads(chat.read_text());body['running']=True;write(chat,body)
    with pytest.raises(ValueError,match='active'):build_snapshot(source,tmp_path/'active',tmp_path/'target',quiesced=True)
    body['running']=False;write(chat,body)
    clips=source/'cosmos/clips.json';records=json.loads(clips.read_text());records[0]['path']=str(tmp_path/'outside.mp4')
    (tmp_path/'outside.mp4').write_bytes(b'other-company');write(clips,records)
    marker=source/'cosmos/index-generation.json';proof=json.loads(marker.read_text())
    proof['files']['clips.json']={'sha256':hashlib.sha256(clips.read_bytes()).hexdigest(),'bytes':clips.stat().st_size};write(marker,proof)
    with pytest.raises(ValueError,match='outside'):build_snapshot(source,tmp_path/'outside',tmp_path/'target',quiesced=True)
    db.close()


def test_financial_operational_link_moves_but_money_events_remain_identical(tmp_path):
    source,db=fixture(tmp_path);ledger=source/'company-usage.sqlite'
    approval=str(source/'compute/query-approval.json')
    with sqlite3.connect(ledger) as money:
        money.execute('CREATE TABLE provider_links(provider_key TEXT, reservation_id TEXT, approval_path TEXT, provider_reservation_id TEXT)')
        money.execute('INSERT INTO provider_links VALUES(?,?,?,?)',(approval+'\0provider-1','hold-1',approval,'provider-1'))
        money.execute('CREATE TABLE events(body TEXT)')
        money.execute('INSERT INTO events VALUES (?)',(json.dumps({'original_path':approval,'held_usd':'2.5'}),))
        money.execute("CREATE TRIGGER immutable_events_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable'); END")
    bundle=tmp_path/'bundle';target=tmp_path/'target'
    build_snapshot(source,bundle,target,quiesced=True)
    with sqlite3.connect(bundle/'state/company-usage.sqlite') as moved:
        link=moved.execute('SELECT * FROM provider_links').fetchone()
        assert link==(str(target/'compute/query-approval.json')+'\0provider-1','hold-1',str(target/'compute/query-approval.json'),'provider-1')
        assert json.loads(moved.execute('SELECT body FROM events').fetchone()[0])=={'original_path':approval,'held_usd':'2.5'}
    assert (bundle/'original-metadata/company-usage.sqlite').is_file()
    db.close()


def test_install_rejects_cross_company_target_api_funding_before_any_copy(tmp_path):
    from company_usage import CompanyUsage
    source,db=fixture(tmp_path);bundle=tmp_path/'bundle';target=tmp_path/'target'
    build_snapshot(source,bundle,target,quiesced=True)
    CompanyUsage.openai_pool(target)  # Bound to this other path-derived company.
    with pytest.raises(ValueError,match='bound company'):
        install_snapshot(bundle,target,quiesced=True)
    assert not (target/'cosmos').exists()
    db.close()


def test_selected_symlink_is_rejected_and_source_change_is_detected(tmp_path,monkeypatch):
    import migrate_state
    source,db=fixture(tmp_path)
    outside=tmp_path/'outside.json';write(outside,{'private':'other scope'})
    (source/'descriptions/linked.json').symlink_to(outside)
    with pytest.raises(ValueError,match='symbolic'):
        build_snapshot(source,tmp_path/'links',tmp_path/'target',quiesced=True)
    (source/'descriptions/linked.json').unlink()
    copy=migrate_state.shutil.copy2
    def changing_copy(path,*args,**kwargs):
        result=copy(path,*args,**kwargs)
        if path==source/'chats/chat-a.json':path.write_text(path.read_text()+' ')
        return result
    monkeypatch.setattr(migrate_state.shutil,'copy2',changing_copy)
    with pytest.raises(ValueError,match='Source (changed|copy checksum)'):
        build_snapshot(source,tmp_path/'racy',tmp_path/'target',quiesced=True)
    assert not (tmp_path/'racy/manifest.json').exists()
    db.close()


def generation_fixture(tmp_path):
    import numpy as np
    from cosmos_retrieval import _publish_generation, MODEL_ID, MODEL_REVISION
    source=tmp_path/'source';workspace_scope(source);workspace_scope(source/'cosmos')
    original=source/'media/cafe/new.mp4';original.parent.mkdir(parents=True);original.write_bytes(b'actual fixture media')
    records=[{'id':'new','video_id':'asset-new','dataset':'cafe','camera':'Front','path':str(original),
        'start':0.,'end':2.,'duration':2.,'model':MODEL_ID,'model_revision':MODEL_REVISION}]
    vectors=np.zeros((1,256),dtype=np.float32);vectors[0,0]=1
    report={'model':MODEL_ID,'model_revision':MODEL_REVISION,'embedding_dimensions':256,'clip_count':1}
    prior=_publish_generation(source/'cosmos',records,vectors,report)
    records[0]['camera']='Current camera'
    current=_publish_generation(source/'cosmos',records,vectors,report)
    # A stray legacy catalogue must never select a different source occurrence.
    write(source/'cosmos/clips.json',[{'path':'/unrelated/old.mp4'}])
    return source,prior,current


def test_generation_snapshot_selects_committed_media_and_relocates_to_new_generation(tmp_path):
    from cosmos_retrieval import load_generation, generation_paths
    source,prior,current=generation_fixture(tmp_path)
    bundle=tmp_path/'bundle';target=tmp_path/'target'
    manifest=build_snapshot(source,bundle,target,quiesced=True)
    copied=bundle/'state/cosmos'
    records,vectors,report,marker,_=load_generation(copied)
    assert records[0]['path']==str(target/'media/cafe/new.mp4')
    assert records[0]['camera']=='Current camera' and vectors[0,0]==1
    assert marker['generation_id'] not in {prior['generation_id'],current['generation_id']}
    assert not (copied/'generations'/prior['generation_id']).exists()
    assert not (copied/'generations'/current['generation_id']).exists()
    assert not (copied/'clips.json').exists()
    assert (bundle/'original-metadata/cosmos/index-generation.json').is_file()
    assert manifest['sources'][0]['asset_id']=='asset-new'
    install_snapshot(bundle,target,quiesced=True)
    assert load_generation(target/'cosmos')[0]==records
    assert generation_paths(target/'cosmos')['clips.json'].stat().st_mode & 0o777==0o400
    assert verify_snapshot(bundle,installed_root=target)['verified']


def test_generation_snapshot_rejects_corrupt_committed_vectors(tmp_path):
    from cosmos_retrieval import generation_paths
    source,_,_=generation_fixture(tmp_path)
    vectors=generation_paths(source/'cosmos')['embeddings.npy'];vectors.chmod(0o600);vectors.write_bytes(b'broken')
    with pytest.raises(ValueError,match='SHA256'):
        build_snapshot(source,tmp_path/'bundle',tmp_path/'target',quiesced=True)


@pytest.mark.parametrize('legacy',[False,True])
def test_generation_change_after_verified_read_cannot_mix_source_manifest_and_files(tmp_path,monkeypatch,legacy):
    import copy
    import migrate_state as migration
    from cosmos_retrieval import _publish_generation
    source,_,_=generation_fixture(tmp_path)
    if legacy:
        for name,path in generation_paths(source/'cosmos').items():
            (source/'cosmos'/name).write_bytes(path.read_bytes())
        (source/'cosmos/index-generation.json').unlink()
    original=migration.load_generation
    changed=False
    def replace_after_verified_read(state,*args,**kwargs):
        nonlocal changed
        loaded=original(state,*args,**kwargs)
        if Path(state)==source/'cosmos' and not changed:
            changed=True
            records,vectors,report,_,_=loaded
            records=copy.deepcopy(records)
            extra=copy.deepcopy(records[0]);extra.update(id='second-interval',start=1.,end=2.)
            records.append(extra)
            if legacy:
                (Path(state)/'clips.json').write_text(json.dumps(records))
                np.save(Path(state)/'embeddings.npy',np.concatenate([vectors,vectors]),allow_pickle=False)
                (Path(state)/'index-report.json').write_text(json.dumps({**report,'clip_count':2}))
            else:
                _publish_generation(state,records,np.concatenate([vectors,vectors]),{**report,'clip_count':2})
        return loaded
    monkeypatch.setattr(migration,'load_generation',replace_after_verified_read)
    # The original bytes/source path remain the same, so comparing only the
    # selected file list cannot detect A's manifest paired with B's catalogue.
    with pytest.raises(ValueError,match='changed'):
        build_snapshot(source,tmp_path/'racy-bundle',tmp_path/'target',quiesced=True)
    assert not (tmp_path/'racy-bundle/manifest.json').exists()
