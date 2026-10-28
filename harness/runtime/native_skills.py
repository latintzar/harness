"""Pinned original skill references resolved into actual native skill inputs.

Public IDs bind the current owner/package generation; neither clients nor models
choose a file path. Native skills/list must independently confirm the sealed path
and owning plugin before the host sends Codex's explicit SkillUserInput.
"""
from __future__ import annotations
import asyncio
from collections import Counter
import hashlib,json,re
from pathlib import PurePosixPath

from native_packages import NativePackageStore,RuntimePackages,MARKETPLACE
from native_provider_relay import _read

PREFIX='native-skill:'
MAX_SKILLS=512

class NativeSkillError(ValueError):pass

def _error():return NativeSkillError('Refresh installed skills in this workspace.')
def is_native_skill(identifier):return isinstance(identifier,str) and identifier.startswith(PREFIX)
def _hash(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

class NativePluginSkills:
    def __init__(self,store):
        if not isinstance(store,NativePackageStore):raise _error()
        self.store=store

    def _descriptors(self,principal_id):
        if not isinstance(principal_id,str) or not principal_id:raise _error()
        from plugin_catalogue import _child, _strings, _skill
        import yaml
        selected=self.store.snapshot(principal_id=principal_id)
        current={'generation':selected.generation};result=[]
        for package in selected.packages:
            files={item.path:item for item in package.files}
            def original(path):
                item=files.get(path)
                if item is None or item.size_bytes>256*1024:raise _error()
                raw=(package.root/path).read_bytes()
                if len(raw)!=item.size_bytes or hashlib.sha256(raw).hexdigest()!=item.sha256:raise _error()
                return raw
            manifest=json.loads(original('.codex-plugin/plugin.json'))
            roots=[_child('',value) for value in _strings(manifest.get('skills','./skills'))]
            candidates=[]
            for path in sorted(files):
                if not path.endswith('/SKILL.md') or not any(root and
                        (path==root or path.startswith(root.rstrip('/')+'/')) for root in roots):continue
                try:skill=_skill(path,package.upstream_revision,original)
                except yaml.YAMLError:continue
                name=skill['name']
                if (skill['metadata_status']!='available' or not isinstance(name,str)
                        or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',name)):continue
                candidates.append((name,path,skill))
            names=Counter(row[0] for row in candidates)
            for name,path,skill in candidates:
                if names[name]!=1:continue
                binding={'company':self.store.company_scope,'owner':selected.owner_binding,
                    'generation':selected.generation,'plugin':package.id,'version':package.version,
                    'tree':package.tree_sha256,'revision':package.upstream_revision,'path':path,'name':name}
                public={'id':PREFIX+_hash(binding),'name':name,'command':package.id+':'+name,
                    'description':skill.get('description',''),'publisher':manifest.get('name',package.id),
                    'plugin_id':package.id,'skill_path':path,'native_plugin':True,'installed':True,
                    'revision':selected.generation,'installed_revision':selected.generation,
                    'latest_revision':selected.generation,'package_version':package.version,
                    'source_url':package.source_url.rstrip('/')+'/'+path}
                result.append((public,binding))
                if len(result)>MAX_SKILLS:raise _error()
        if selected.packages and not self.current(selected,principal_id):raise _error()
        return current,result

    def list(self,principal_id):
        _,items=self._descriptors(principal_id)
        return [public for public,_ in items]

    def resolve(self,identifiers,principal_id):
        if (not isinstance(identifiers,(list,tuple)) or len(identifiers)>8 or
                any(not is_native_skill(i) for i in identifiers) or len(set(identifiers))!=len(identifiers)):raise _error()
        _,items=self._descriptors(principal_id);indexed={p['id']:p for p,_ in items}
        if any(i not in indexed for i in identifiers):raise _error()
        return [indexed[i] for i in identifiers]

    def current(self,selection,principal_id):
        if not isinstance(selection,RuntimePackages):return False
        owner=hashlib.sha256(principal_id.encode()).hexdigest()
        if selection.owner_binding!=owner or selection.company_scope!=self.store.company_scope:return False
        # A single local generation read is the final synchronous native-write
        # fence; no package hashing or public metadata parsing occurs here.
        with self.store._db() as db:
            row=db.execute('SELECT generation FROM owners WHERE owner=?',(owner,)).fetchone()
        return row is not None and row['generation']==selection.generation

    async def inputs(self,identifiers,*,principal_id,selection,connection,cancelled):
        if not identifiers:return []
        if (not isinstance(selection,RuntimePackages) or selection.company_scope!=self.store.company_scope
                or selection.owner_binding!=hashlib.sha256(principal_id.encode()).hexdigest() or cancelled.is_set()):raise _error()
        current,rows=await _read(self._descriptors,principal_id)
        if current['generation']!=selection.generation:raise _error()
        indexed={p['id']:(p,b) for p,b in rows};expected=[]
        if len(identifiers)>8 or len(set(identifiers))!=len(identifiers):raise _error()
        for identifier in identifiers:
            if identifier not in indexed:raise _error()
            public,binding=indexed[identifier]
            package=next((p for p in selection.packages if p.id==binding['plugin']),None)
            if (not package or package.tree_sha256!=binding['tree'] or package.version!=binding['version']
                    or package.upstream_revision!=binding['revision'] or binding['path'] not in {f.path for f in package.files}):raise _error()
            path=PurePosixPath('/codex-state/plugins/cache')/MARKETPLACE/package.id/package.version/binding['path']
            expected.append({'name':public['command'],'path':str(path),'pluginId':package.id+'@'+MARKETPLACE})
        if cancelled.is_set():raise _error()
        response=await connection.rpc('skills/list',{'cwds':['/workspace'],'forceReload':True})
        discovered=[skill for group in response.get('data',[]) if group.get('cwd')=='/workspace' for skill in group.get('skills',[])]
        for item in expected:
            matches=[s for s in discovered if all(s.get(k)==v for k,v in item.items()) and s.get('enabled') is True]
            if len(matches)!=1:raise _error()
        latest=await _read(self.store.list,principal_id=principal_id)
        if cancelled.is_set() or latest['generation']!=selection.generation:raise _error()
        return [{'type':'skill','name':item['name'],'path':item['path']} for item in expected]

async def native_skill_inputs(agent,resolved,run):
    requested=(resolved or {}).get('native_skills',[])
    if not requested:return []
    session=run.get('isolated_session');store=agent._native_skills()
    if (store is None or session is None or getattr(session,'_closing',True)
            or getattr(session,'stopped',True) or run['cancel'].is_set()):raise _error()
    values=await store.inputs([item['id'] for item in requested],principal_id=session.scope.owner_id,
        selection=session.runtime_packages,connection=run['connection'],cancelled=run['cancel'])
    if session._closing or session.stopped or run['cancel'].is_set():raise _error()
    def delivery_guard():
        return (agent.active.get(session.scope.root_chat_id) is run and not session._closing and not session.stopped
                and not run['cancel'].is_set() and store.current(session.runtime_packages,session.scope.owner_id))
    run['native_skill_guard']=delivery_guard
    return values
