"""Prepare/verify/install private candidate state; never deploy or call a model.

Stop application/index writers before snapshot or installation. SQLite backup is
transactionally consistent per database; cross-file consistency additionally
requires that explicit quiescence and the source-change checks below.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
if (Path(__file__).resolve().parents[1] / 'layout.py').is_file():
    import layout
    layout.install()
from workspace_identity import IDENTITY_FILE
from cosmos_retrieval import GENERATION_FILE, GENERATION_FILES, generation_paths, load_generation, _publish_generation

DATABASES={'access.sqlite','background-agents.sqlite','business-context.sqlite',
    'camera-context.sqlite','chat-permissions.sqlite','company-usage.sqlite',
    'investigation-plans.sqlite','skills.sqlite'}
ROOT_JSON={'agent-chats.json','agent-chat-scopes.json','native-toolsets.json',
    'native-usage.json','relations.json',IDENTITY_FILE}
DATA_DIRS={'attachments','chats','descriptions','focus','reasoner-cache','reasoner-inputs',
    'reasoner-runs','reasoner-pilot','native-migration','migration'}
MEDIA_SUFFIXES={'.mp4','.mov','.mkv','.webm','.avi','.png','.jpg','.jpeg','.webp'}
NATIVE_SUFFIXES={'.json','.mp4','.png','.jpg','.jpeg','.webp'}
PRESERVED_TARGET='billing/openai-api'


def _now():return datetime.now(timezone.utc).isoformat()


def _hash(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def _json(path):return json.loads(path.read_text())


def _write(path,data):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with path.open('w') as stream:json.dump(data,stream,indent=2,allow_nan=False);stream.write('\n')
    path.chmod(0o600)


def _stamp(path):
    info=path.stat()
    return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns


def _regular(path):
    if path.is_symlink() or path.resolve()!=path.absolute() or not path.is_file():
        raise ValueError('Use regular source files inside the authorized state tree.')


def _sqlite_files(source):
    """SHM is a reader/writer coordination cache, never database content."""
    files={}
    for suffix in ('','-wal','-journal'):
        path=Path(str(source)+suffix)
        if not suffix or path.exists() or path.is_symlink():
            _regular(path);files[path]=_stamp(path)
    return files


@contextmanager
def _private_database(source):
    """Let SQLite recover only a private, identity-checked database/WAL copy.

    Even mode=ro can create WAL/SHM files at the original database. immutable=1
    instead ignores a committed WAL. Copying the quiesced database and journals
    preserves committed rows and keeps all SQLite side effects in this directory.
    """
    source=Path(source);before=_sqlite_files(source)
    with TemporaryDirectory(prefix='rocoto-migration-sqlite-') as temporary:
        private=Path(temporary)/source.name
        for path in before:
            copied=Path(temporary)/path.name
            shutil.copy2(path,copied);copied.chmod(0o600)
        if before!=_sqlite_files(source):
            raise ValueError('SQLite source changed during copy; repeat while quiesced.')
        try:
            with closing(sqlite3.connect(private)) as db:
                db.execute('PRAGMA query_only=ON')
                yield db
        finally:
            if before!=_sqlite_files(source):
                raise ValueError('SQLite source changed during inspection; repeat while quiesced.')


def _workspace(relative):
    parts=relative.parts
    if len(parts)>2 and parts[0] in {'environments','principals'}:
        return Path(*parts[:2]),Path(*parts[2:])
    return Path(),relative


def _include(relative,indexed):
    _,local=_workspace(relative);parts=local.parts
    if not parts:return False
    if relative in indexed:return True
    if local.as_posix().startswith(PRESERVED_TARGET+'/'):return False
    if any(part in {'.codex','.cache','.ssh','.aws','node_modules','__pycache__'} for part in parts):return False
    if local.name in {'auth.json','credentials.json','config.json','device-auth.log'} or local.suffix in {'.env','.pem','.key'}:return False
    if local.name.endswith(('.lock','-wal','-shm','.tmp')):return False
    if local.name in DATABASES:return True
    if len(parts)==1:return local.name in ROOT_JSON
    if parts[0]=='cosmos':
        if local.name in {*GENERATION_FILES,GENERATION_FILE}:return False  # Selected committed paths entered above.
        return (local.name!= 'qdrant-index.json' and (local.name in
            {'clips.json','embeddings.npy','index-report.json','index-generation.json','text-encoder.pt',
             'text-encoder.json',IDENTITY_FILE} or parts[1] in {'thumbnails','text-tokenizer'}))
    if parts[0] in DATA_DIRS:return local.suffix.lower() in NATIVE_SUFFIXES
    if parts[0]=='compute':return local.suffix=='.json'
    if parts[0]=='media':return local.suffix.lower() in {'.json','.md','.csv'} or local.name.lower().startswith(('license','readme'))
    if parts[0].startswith(('super-','nano-')):return local.suffix.lower() in NATIVE_SUFFIXES
    if parts[0].startswith('modal-'):return local.suffix=='.json'
    return False


def _check_idle(source):
    for path in source.rglob('chats/*.json'):
        if _json(path).get('running'):raise ValueError('Finish active chats before a quiesced snapshot.')
    for path in source.rglob('background-agents.sqlite'):
        with _private_database(path) as db:
            for table,field,active in [('agents','status','active'),('runs','status','running')]:
                for (body,) in db.execute(f'SELECT body FROM {table}'):
                    if _json_value(body).get(field)==active:raise ValueError('Pause active background agents before a quiesced snapshot.')
    for path in source.rglob('billing/openai-api/company-usage.sqlite'):
        with _private_database(path) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone() and db.execute('SELECT 1 FROM events LIMIT 1').fetchone():
                raise ValueError('Reconcile source API money events with authoritative staging funding before migration.')


def _json_value(value):return json.loads(value)


def _cosmos_generations(source):
    """Verified committed indexes; retained/uncommitted directories stay at source."""
    for cosmos in sorted(source.rglob('cosmos')):
        if not cosmos.is_dir() or not any((cosmos/name).exists() for name in ('clips.json',GENERATION_FILE)):
            continue
        if cosmos.is_symlink() or not (cosmos/IDENTITY_FILE).is_file():
            raise ValueError('Adopt the original vector-index workspace identity before migration.')
        marker=cosmos/GENERATION_FILE
        if marker.exists():_regular(marker)
        records,_,_,committed,identity=load_generation(cosmos)
        selected=list(generation_paths(cosmos,committed if committed["schema_version"] else None).values())
        if marker.exists():selected.append(marker)
        for path in selected:_regular(path)
        if (_stamp(marker) if marker.exists() else None)!=identity:
            raise ValueError("Cosmos generation changed while selecting migration files; repeat while quiesced.")
        yield cosmos,records,selected,committed["generation_id"]


def inventory(source):
    """Read-only selection audit; credentials and unindexed experiment media stay on dev."""
    source=Path(source).resolve(strict=True)
    indexed=set();sources=[];scopes={Path()};generations={}
    for cosmos,records,selected,generation in _cosmos_generations(source):
        workspace=cosmos.parent.relative_to(source);scopes.add(workspace)
        generations[workspace.as_posix()]=generation
        indexed.update(path.relative_to(source) for path in selected)
        for clip in records:
            original=Path(clip['path'])
            if not original.is_absolute() or not original.is_relative_to(source):
                raise ValueError('An indexed source is outside the selected state; import it into its company first.')
            _regular(original);relative=original.relative_to(source)
            if _workspace(relative)[0]!=workspace:
                raise ValueError('An indexed source belongs to another company workspace.')
            if original.suffix.lower() not in MEDIA_SUFFIXES:raise ValueError('Verify the indexed source media format.')
            indexed.add(relative)
            sources.append({'workspace':workspace.as_posix(),'asset_id':clip['video_id'],'clip_id':clip['id'],
                'relative_path':relative.as_posix(),'start':clip['start'],'end':clip['end']})
    files=[];excluded=[]
    for path in sorted(source.rglob('*')):
        relative=path.relative_to(source)
        if path.is_symlink():
            if _include(relative,indexed):raise ValueError('Selected migration input is a symbolic link.')
            excluded.append(relative.as_posix());continue
        if not path.is_file():continue
        if _include(relative,indexed):
            _regular(path);files.append(relative);scopes.add(_workspace(relative)[0])
        else:excluded.append(relative.as_posix())
    for scope in scopes:
        if not (source/scope/IDENTITY_FILE).is_file():
            raise ValueError('Adopt each original workspace identity before copying its state.')
    return {'source':source,'files':files,'sources':sources,'excluded':excluded,'workspaces':sorted(scopes),'cosmos_generations':generations}


def _relocate(value,source,target,fields):
    if isinstance(value,list):return [_relocate(item,source,target,fields) for item in value]
    if not isinstance(value,dict):return value
    output={}
    for key,item in value.items():
        if key in fields and isinstance(item,str) and item.startswith(str(source)+'/'):
            output[key]=str(target/Path(item).relative_to(source))
        else:output[key]=_relocate(item,source,target,fields)
    return output


def _backup(source,destination):
    destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with _private_database(source) as original:
        with closing(sqlite3.connect(destination)) as copied:
            original.backup(copied)
            copied.execute('PRAGMA journal_mode=DELETE')
            if copied.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise ValueError('Verify the SQLite backup before migration.')
    destination.chmod(0o600);shutil.copystat(source,destination);destination.chmod(0o600)


def _relocate_database(path,source,target):
    """Only mutable operational links move; immutable event bodies remain verbatim."""
    if path.name!='company-usage.sqlite':return 0
    with closing(sqlite3.connect(path)) as db,db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_links'").fetchone():return 0
        changed=0
        for key,rid,approval,provider_id in db.execute('SELECT * FROM provider_links').fetchall():
            if not approval.startswith(str(source)+'/'):
                raise ValueError('Reconcile an external provider authority before moving its ledger.')
            relocated=str(target/Path(approval).relative_to(source))
            db.execute('UPDATE provider_links SET provider_key=?,approval_path=? WHERE reservation_id=?',
                (relocated+'\0'+provider_id,relocated,rid));changed+=1
        return changed


def build_snapshot(source,bundle,target,*,quiesced=False,native_exports=None):
    if not quiesced:raise ValueError('Confirm a quiesced source before snapshotting related state.')
    source=Path(source).resolve(strict=True);bundle=Path(bundle).absolute();target=Path(target).absolute()
    if (bundle.exists() or bundle.resolve()!=bundle or target.resolve()!=target
            or bundle.is_relative_to(source) or target.is_relative_to(source) or source.is_relative_to(target)
            or bundle.is_relative_to(target) or target.is_relative_to(bundle)):
        raise ValueError('Choose a new private bundle outside the source and a separate target.')
    _check_idle(source);plan=inventory(source)
    export_root=Path(native_exports).resolve(strict=True) if native_exports else None
    # Every current host-owned binding needs an explicit public continuation export.
    from deploy.migration.history import history_export_path
    exports=[]
    for workspace in plan['workspaces']:
        registry=source/workspace/'agent-chats.json'
        for chat_id,thread_id in (_json(registry) if registry.exists() else {}).items():
            chat=source/workspace/'chats'/(chat_id+'.json')
            if not chat.is_file() or not _json(chat).get('owner_principal_id'):continue
            relative=history_export_path(Path(),chat_id)
            exported=(export_root/workspace/relative) if export_root else source/workspace/relative
            if not exported.is_file():raise ValueError('Export all owned native chat histories before migration.')
            # Validate content and original scope, using the source identity rather than an output-root scope.
            data=_json(exported);record=_json(chat)
            body={key:value for key,value in data.items() if key!='payload_sha256'}
            if (data.get('source_thread_id')!=thread_id or data.get('chat_id')!=chat_id
                    or data.get('owner_principal_id')!=record['owner_principal_id']
                    or data.get('company_scope')!=_json(source/workspace/IDENTITY_FILE)['company_scope']
                    or data.get('payload_sha256')!=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()):
                raise ValueError('Verify the scoped native history export before migration.')
            exports.append((exported,workspace/relative))
    stamps={relative:_stamp(source/relative) for relative in plan['files']}
    # WAL changes count even when a SQLite database file itself has not changed.
    watched={p:_stamp(p) for p in source.rglob('*') if p.is_file() and p.name.endswith(('-wal','-shm','-journal'))}
    bundle.mkdir(mode=0o700,parents=True);payload=bundle/'state';payload.mkdir(mode=0o700)
    transformations=[];entries=[]
    for relative in plan['files']:
        original=source/relative;copied=payload/relative
        copied.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        if original.name in DATABASES:_backup(original,copied)
        else:
            shutil.copy2(original,copied);copied.chmod(0o600)
            if _hash(original)!=_hash(copied):raise ValueError('Source copy checksum differs; repeat while quiesced.')
        _,local=_workspace(relative)
        fields=set()
        if 'manifest' in local.name and local.suffix=='.json':fields={'path','source','clip'}
        elif local.parts[0]=='focus' and local.suffix=='.json':fields={'sidecar_path'}
        before=_hash(copied)
        if fields:
            document=_json(copied);relocated=_relocate(document,source,target,fields)
            if document!=relocated:
                archive=bundle/'original-metadata'/relative;archive.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                shutil.copy2(copied,archive);_write(copied,relocated)
        if copied.name=='company-usage.sqlite':
            archive=bundle/'original-metadata'/relative;archive.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            shutil.copy2(copied,archive)
            if not _relocate_database(copied,source,target):archive.unlink()
        after=_hash(copied)
        if before!=after:transformations.append({'path':relative.as_posix(),'before_sha256':before,'after_sha256':after,
            'reason':'operational_path_relocation'})
        os.utime(copied,ns=(original.stat().st_atime_ns,stamps[relative][3]));copied.chmod(0o600)
    for cosmos,_,selected,generation in list(_cosmos_generations(payload)):
        records,vectors,report,original,_=load_generation(cosmos)
        expected=plan["cosmos_generations"][cosmos.parent.relative_to(payload).as_posix()]
        if generation!=expected or original["generation_id"]!=expected:
            raise ValueError("Cosmos generation changed after source inventory; repeat while quiesced.")
        # Relocation publishes its own valid generation, preserving source vectors
        # and the old catalogue/marker in the audit area. Writers remain stopped.
        paths=generation_paths(cosmos,original if original["schema_version"] else None)
        for path in (paths['clips.json'],cosmos/GENERATION_FILE):
            if path.exists():
                archive=bundle/'original-metadata'/path.relative_to(payload)
                archive.parent.mkdir(mode=0o700,parents=True,exist_ok=True);shutil.copy2(path,archive)
        relocated=_relocate(records,source,target,{'path','thumbnail_path'})
        before=_hash(cosmos/GENERATION_FILE) if (cosmos/GENERATION_FILE).exists() else None
        _publish_generation(cosmos,relocated,vectors,report)
        (cosmos/'.index-publish.lock').unlink()  # Private quiesced bundle, never a live publisher.
        for path in paths.values():path.unlink()
        if paths['clips.json'].parent!=cosmos:paths['clips.json'].parent.rmdir()
        marker=cosmos/GENERATION_FILE
        transformations.append({'path':marker.relative_to(payload).as_posix(),'before_sha256':before,
            'after_sha256':_hash(marker),'reason':'new_catalogue_generation_after_path_relocation',
            'original_generation_id':original['generation_id']})
    for original,relative in exports:
        _regular(original);copied=payload/relative;copied.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        shutil.copy2(original,copied);copied.chmod(0o600)
        if _hash(original)!=_hash(copied):raise ValueError('Native history export changed during snapshot.')
    if any(_stamp(source/relative)!=stamp for relative,stamp in stamps.items()):
        raise ValueError('Source changed during snapshot; retain the incomplete bundle for review and repeat while quiesced.')
    if watched!={p:_stamp(p) for p in source.rglob('*') if p.is_file() and p.name.endswith(('-wal','-shm','-journal'))}:
        raise ValueError('SQLite journals changed during snapshot; repeat while quiesced.')
    final_inventory=inventory(source)
    if final_inventory['files']!=plan['files'] or final_inventory['cosmos_generations']!=plan['cosmos_generations']:
        raise ValueError('Source file selection changed during snapshot; repeat while quiesced.')
    _check_idle(source)
    for path in sorted(bundle.rglob('*')):
        if path.is_dir():path.chmod(0o700)
        if path.is_file():entries.append({'path':path.relative_to(bundle).as_posix(),'sha256':_hash(path),
            'bytes':path.stat().st_size,'mtime_ns':path.stat().st_mtime_ns})
    assets={}
    for record in plan['sources']:
        key=(record['workspace'],record['asset_id']);relative=record['relative_path']
        assets.setdefault(key,{'workspace':record['workspace'],'asset_id':record['asset_id'],'path':relative,
            'sha256':_hash(payload/relative),'bytes':(payload/relative).stat().st_size,'intervals':[]})['intervals'].append(
                {'clip_id':record['clip_id'],'start':record['start'],'end':record['end']})
    manifest={'schema_version':1,'created_at':_now(),'source_root':str(source),'target_root':str(target),
        'quiesced_by_operator':True,'files':entries,'sources':list(assets.values()),'transformations':transformations,
        'relocation_map':{str(source):str(target)},'excluded':plan['excluded'],
        'preserve_target_subtrees':[PRESERVED_TARGET],
        'native_continuation':'bounded_public_history_export','qdrant':'rebuild_from_verified_cosmos_vectors',
        'activation':'application remains stopped; review and explicitly resume saved agents'}
    _write(bundle/'manifest.json',manifest)
    verify_snapshot(bundle)
    return manifest


def verify_snapshot(bundle,*,installed_root=None):
    bundle=Path(bundle).resolve(strict=True);manifest=_json(bundle/'manifest.json')
    if manifest.get('schema_version')!=1:raise ValueError('Use a supported migration manifest.')
    destination=Path(installed_root).resolve(strict=True) if installed_root else None
    if destination and str(destination)!=manifest['target_root']:raise ValueError('Verify the declared target root.')
    seen=set()
    for entry in manifest['files']:
        relative=Path(entry['path'])
        if relative.is_absolute() or '..' in relative.parts or relative.as_posix() in seen:
            raise ValueError('Use distinct bounded migration paths.')
        seen.add(relative.as_posix())
        if destination and relative.parts[0]!='state':continue
        path=destination/Path(*relative.parts[1:]) if destination else bundle/relative
        _regular(path)
        if path.stat().st_size!=entry['bytes'] or _hash(path)!=entry['sha256']:
            raise ValueError('Migration checksum differs; keep the current runtime stopped.')
    if not destination:
        actual={p.relative_to(bundle).as_posix() for p in bundle.rglob('*') if p.is_file() and p!=bundle/'manifest.json'}
        if actual!=seen:raise ValueError('Verify the complete migration file inventory.')
    return {'verified':True,'file_count':len(seen),'source_count':len(manifest['sources'])}


def install_snapshot(bundle,target,*,quiesced=False):
    if not quiesced:raise ValueError('Confirm a quiesced target before installation.')
    verify_snapshot(bundle);bundle=Path(bundle).resolve();target=Path(target).absolute();manifest=_json(bundle/'manifest.json')
    if str(target)!=manifest['target_root'] or target.resolve()!=target:raise ValueError('Install only at the declared target root.')
    # The staging API pool may already hold the newly commissioned allocation.
    # Preserve it, but require its immutable binding to match the incoming company.
    for identity in (bundle/'state').rglob(IDENTITY_FILE):
        workspace=identity.parent.relative_to(bundle/'state')
        database=target/workspace/PRESERVED_TARGET/'company-usage.sqlite'
        if database.exists():
            _regular(database)
            with _private_database(database) as db:
                binding=db.execute('SELECT provider,parent_company_scope FROM pool_binding').fetchone()
            if not binding or binding!=('openai',_json(identity)['company_scope']):
                raise ValueError('Preserve API funding inside its bound company workspace.')
    selected={Path(item['path']) for item in manifest['sources']}
    for _,_,paths,_ in _cosmos_generations(bundle/'state'):
        selected.update(path.relative_to(bundle/'state') for path in paths)
    planned=[]
    for entry in manifest['files']:
        relative=Path(entry['path'])
        if relative.parts[0]!='state':continue
        local=Path(*relative.parts[1:]);_,scoped=_workspace(local)
        if not _include(local,selected):
            raise ValueError('Install only declared application state and scoped source media.')
        if scoped.as_posix().startswith(PRESERVED_TARGET+'/'):raise ValueError('Preserve commissioned target API funding.')
        destination=target/local
        if destination.resolve()!=destination:raise ValueError('Install into a regular private target tree.')
        if destination.exists():
            _regular(destination)
            if _hash(destination)!=entry['sha256']:raise ValueError('Reconcile existing target data before installation.')
        if local.name in DATABASES and any(Path(str(destination)+tail).exists() for tail in ('-wal','-shm')):
            raise ValueError('Close target database writers before installation.')
        planned.append((bundle/relative,destination))
    target.mkdir(mode=0o700,parents=True,exist_ok=True)
    for source,destination in planned:
        if destination.exists():continue
        destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        shutil.copy2(source,destination);destination.chmod(0o400 if source.name in GENERATION_FILES and 'generations' in source.parts else 0o600)
    result=verify_snapshot(bundle,installed_root=target)
    _write(target/'migration'/'state-migration.json',manifest)
    return result


async def export_native(source,output,*,codex_bin='codex'):
    """Local thread/read only. No thread/start, turn, provider inference or auth export."""
    from deploy.migration.app_server import AppServerConnection
    from deploy.migration.config import cli_args, EXPORT_CONFIG
    from deploy.migration.history import history_export,history_export_path
    source=Path(source).resolve(strict=True);output=Path(output).absolute()
    if output.exists() or output.is_relative_to(source):raise ValueError('Choose a new private export directory outside the source.')
    _check_idle(source);plan=inventory(source)
    process=await asyncio.create_subprocess_exec(codex_bin,'app-server','--listen','stdio://',
        *cli_args(EXPORT_CONFIG),cwd=str(source),
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,limit=16*1024*1024)
    connection=AppServerConnection(process);output.mkdir(mode=0o700,parents=True);count=0
    try:
        await asyncio.wait_for(connection.rpc('initialize',{'clientInfo':{'name':'rocoto_state_migration','version':'1'},
            'capabilities':{'experimentalApi':True}}),timeout=20)
        await connection.write({'method':'initialized'})
        for workspace in plan['workspaces']:
            registry=source/workspace/'agent-chats.json'
            for chat_id,thread_id in (_json(registry) if registry.exists() else {}).items():
                chat=source/workspace/'chats'/(chat_id+'.json')
                if not chat.is_file() or not _json(chat).get('owner_principal_id'):continue
                response=await asyncio.wait_for(connection.rpc('thread/read',{'threadId':thread_id,'includeTurns':True}),timeout=30)
                thread=response.get('thread',{})
                if thread.get('id')!=thread_id:raise ValueError('Read the exact registered native thread.')
                document=history_export(thread,state=source/workspace,chat_id=chat_id,
                    owner_principal_id=_json(chat)['owner_principal_id'])
                _write(history_export_path(output/workspace,chat_id),document);count+=1
    finally:await connection.close()
    return {'exported_threads':count,'source_root':str(source),'model_calls':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    audit=sub.add_parser('audit');audit.add_argument('--source',type=Path,required=True)
    export=sub.add_parser('export-native');export.add_argument('--source',type=Path,required=True)
    export.add_argument('--output',type=Path,required=True);export.add_argument('--codex-bin',default='codex')
    build=sub.add_parser('snapshot');build.add_argument('--source',type=Path,required=True)
    build.add_argument('--bundle',type=Path,required=True);build.add_argument('--target',type=Path,required=True)
    build.add_argument('--native-exports',type=Path);build.add_argument('--quiesced',action='store_true')
    verify=sub.add_parser('verify');verify.add_argument('--bundle',type=Path,required=True)
    verify.add_argument('--installed-root',type=Path)
    install=sub.add_parser('install');install.add_argument('--bundle',type=Path,required=True)
    install.add_argument('--target',type=Path,required=True);install.add_argument('--quiesced',action='store_true')
    args=parser.parse_args();options=vars(args).copy();command=options.pop('command')
    if command=='audit':
        plan=inventory(args.source);result={'selected_files':len(plan['files']),'excluded_files':len(plan['excluded']),
            'indexed_sources':len({(r['workspace'],r['asset_id']) for r in plan['sources']}),
            'bytes':sum((plan['source']/p).stat().st_size for p in plan['files']),
            'workspaces':[p.as_posix() for p in plan['workspaces']]}
    elif command=='export-native':result=asyncio.run(export_native(**options))
    elif command=='snapshot':
        manifest=build_snapshot(**options);result={'files':len(manifest['files']),'sources':len(manifest['sources'])}
    elif command=='verify':result=verify_snapshot(**options)
    else:result=install_snapshot(**options)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
