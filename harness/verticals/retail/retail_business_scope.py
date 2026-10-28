"""Retail locations, rooms, cameras and retained source-view authority.

Construct actors and callbacks from the host's authenticated company membership.
Reads of absent state are inert. Location assignments restrict every derived
camera/source grant; retained view revisions preserve earlier recording provenance.
"""
from __future__ import annotations
from business_scope import WorkspaceAuthority, ScopeActor, ScopeError, need, identifier, label, ids
from contextlib import closing, contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid

KINDS = ('locations', 'functions', 'rooms', 'cameras', 'members')
LIMITS = {'locations':100, 'functions':100, 'rooms':500, 'cameras':500, 'members':1000}
ROLES = {'location_admin','function_admin','colleague','alert_recipient'}

class RetailBusinessScope(WorkspaceAuthority):
    def __init__(self,state,*,company_id,member,members=lambda:[],source_authorized=lambda *args:False,clock=time.time):
        super().__init__(state,company_id=company_id,member=member,members=members)
        self.path=self.state/'business-scope.sqlite'
        self.source_authorized=source_authorized; self.clock=clock

    @contextmanager
    def _db(self,write=False):
        if not self.path.exists() and not write:
            yield None; return
        if write:
            self.state.mkdir(parents=True,exist_ok=True,mode=0o700)
            try:
                fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600); os.close(fd)
            except FileExistsError: pass
        info=self.path.lstat()
        need(stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid() and not info.st_mode&0o077,'scope_store_unavailable')
        db=sqlite3.connect(str(self.path) if write else self.path.as_uri()+'?mode=ro',uri=not write,timeout=5)
        db.row_factory=sqlite3.Row
        try:
            if write:
                db.execute('PRAGMA synchronous=FULL'); db.execute('PRAGMA max_page_count=16384'); db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE IF NOT EXISTS identity(company TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS graphs(collection TEXT PRIMARY KEY,revision INTEGER NOT NULL,body TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS history(collection TEXT NOT NULL,revision INTEGER NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL,body TEXT NOT NULL,PRIMARY KEY(collection,revision))')
                db.execute('CREATE TABLE IF NOT EXISTS chats(chat TEXT PRIMARY KEY,collection TEXT NOT NULL,locations TEXT,revision INTEGER NOT NULL,updated REAL NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,collection TEXT NOT NULL,revision INTEGER NOT NULL,actor TEXT NOT NULL,kind TEXT NOT NULL,record_id TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS chat_history(chat TEXT PRIMARY KEY,body TEXT NOT NULL,native_binding TEXT NOT NULL)')
                if not db.execute('SELECT 1 FROM identity').fetchone():db.execute('INSERT INTO identity VALUES(?)',(self.company_id,))
            identity=db.execute('SELECT company FROM identity').fetchall()
            need(len(identity)==1 and identity[0][0]==self.company_id,'scope_store_unavailable')
            yield db
            if write:db.commit()
        finally:db.close()

    @staticmethod
    def _graph(db,collection):
        row=db.execute('SELECT revision,body FROM graphs WHERE collection=?',(collection,)).fetchone() if db else None
        return (row['revision'],json.loads(row['body'])) if row else (0,{kind:[] for kind in KINDS})

    def _effective(self,actor,member,graph,revision,collection,locations=None):
        owner=member['role']=='owner'
        grant=next((r for r in graph['members'] if r['member_id']==actor.principal_id),None)
        role='organization_admin' if owner else grant['role'] if grant else 'unassigned'
        allowed_locations={r['id'] for r in graph['locations']} if owner else set(grant['location_ids']) if grant else set()
        if locations is not None:
            selected=set(ids(locations)); need(selected<=allowed_locations,'scope_selection_denied'); allowed_locations=selected
        functions=set(grant['function_ids']) if grant and not owner else set()
        rooms={r['id'] for r in graph['rooms'] if r['location_id'] in allowed_locations
               and (not functions or functions.intersection(r['function_ids']))}
        cameras=[]; sources=set()
        for camera in graph['cameras']:
            if camera['location_id'] in allowed_locations and camera['room_ids'] and set(camera['room_ids'])<=rooms:
                cameras.append(camera['id'])
            for source,view in camera.get('source_views',{}).items():
                if view['location_id'] in allowed_locations and view['room_ids'] and set(view['room_ids'])<=rooms:
                    sources.add(source)
        query=member['role']!='browse' and role not in {'unassigned','alert_recipient'}
        return {'company_id':self.company_id,'principal_id':actor.principal_id,'collection':collection,
            'revision':revision,'configured':revision>0,'role':role,'location_ids':sorted(allowed_locations),
            'room_ids':sorted(rooms),'function_ids':sorted({f for r in graph['rooms'] if r['id'] in rooms for f in r['function_ids'] if not functions or f in functions}),
            'camera_ids':sorted(cameras) if query else [],'source_ids':sorted(sources) if query else [],
            'includes_unassigned':owner and locations is None,
            'can_query':query,'account_role':member['role'],
            'can_manage':member['role']!='browse' and not actor.observer and role in {'organization_admin','location_admin','function_admin'}}

    def effective(self,actor,collection,location_ids=None):
        identifier(collection); member=self.authority(actor)
        with self._db() as db:revision,graph=self._graph(db,collection)
        need(self.authority(actor)['role']==member['role'],'scope_authority_changed')
        return self._effective(actor,member,graph,revision,collection,location_ids)

    def describe(self,actor,collection):
        identifier(collection); member=self.authority(actor)
        with self._db() as db:revision,graph=self._graph(db,collection)
        effective=self._effective(actor,member,graph,revision,collection)
        owner=member['role']=='owner'; manage=effective['can_manage']
        kinds=list(KINDS) if owner and manage else ['rooms','cameras','members'] if manage and effective['role']=='location_admin' else ['cameras','members'] if manage else []
        result={kind:copy.deepcopy(rows) for kind,rows in graph.items()}
        if not owner:
            result['locations']=[r for r in result['locations'] if r['id'] in effective['location_ids']]
            result['rooms']=[r for r in result['rooms'] if r['id'] in effective['room_ids']]
            result['functions']=[r for r in result['functions'] if r['id'] in effective['function_ids']]
            result['cameras']=[r for r in result['cameras'] if r['id'] in effective['camera_ids']]
            result['members']=[r for r in result['members'] if r['member_id']==actor.principal_id or manage and set(r['location_ids'])<=set(effective['location_ids']) and (not next((g['function_ids'] for g in graph['members'] if g['member_id']==actor.principal_id),[]) or set(r['function_ids']) and set(r['function_ids'])<=set(effective['function_ids']))]
        for camera in result['cameras']:
            camera.pop('views',None);camera.pop('source_views',None)
            if not owner:camera['source_ids']=[source for source in camera['source_ids'] if source in effective['source_ids']]
        visible_members={r['member_id']:r for r in result['members']}
        candidates=self.members() if owner else [{'id':k} for k in visible_members]
        live_members={}
        for candidate in candidates:
            key=candidate.get('id') or candidate.get('principal_id')
            if not isinstance(key,str):continue
            live=self.member(key)
            if not live:continue
            if key not in visible_members:visible_members[key]={'member_id':key,'role':'colleague','location_ids':[],'function_ids':[]}
            visible_members[key].update(display_name=live.get('display_name','Member'),account_role=live['role'])
            live_members[key]=visible_members[key]
        result['members']=list(live_members.values())
        need(self.authority(actor)['role']==member['role'],'scope_authority_changed')
        return {'schema_version':1,'collection':collection,'revision':revision,'configured':revision>0,
            'can_manage':manage,'can_manage_kinds':kinds,**result,'effective':effective}

    def camera(self,actor,collection,camera_id,*,manage=False):
        view=self.describe(actor,collection)
        need(not manage or 'cameras' in view['can_manage_kinds'],'scope_management_denied')
        record=next((r for r in view['cameras'] if r['id']==camera_id),None)
        need(record is not None,'scope_camera_unavailable')
        return record,view['revision']

    def mutate(self,actor,collection,kind,value,expected_revision):
        identifier(collection); need(kind in KINDS and isinstance(value,dict) and type(expected_revision)is int)
        member=self.authority(actor,write=True)
        with self._db(True) as db:
            revision,graph=self._graph(db,collection); need(revision==expected_revision,'scope_revision_conflict')
            effective=self._effective(actor,member,graph,revision,collection)
            owner=member['role']=='owner'; role=effective['role']; need(effective['can_manage'],'scope_management_denied')
            need(owner or kind in ({'rooms','cameras','members'} if role=='location_admin' else {'cameras','members'}),'scope_management_denied')
            rows=graph[kind]; key='member_id' if kind=='members' else 'id'
            allowed={'locations':{'id','name','address'},'functions':{'id','name'},'rooms':{'id','name','location_id','function_ids'},
                'cameras':{'id','name','location_id','room_ids','source_ids'},'members':{'member_id','role','location_ids','function_ids'}}[kind]
            need(set(value)<=allowed)
            identity=identifier(value.get(key)) if value.get(key) else kind[:-1]+'-'+uuid.uuid4().hex
            old=next((r for r in rows if r[key]==identity),None)
            if value.get(key) and kind!='members':need(old is not None,'scope_record_unavailable')
            if kind=='members':
                need(value.get('member_id') and self.member(identity),'scope_member_unavailable')
                need(self.member(identity).get('role')!='owner','scope_owner_managed_by_account')
                entry={'member_id':identity,'role':value.get('role'),'location_ids':ids(value.get('location_ids')),'function_ids':ids(value.get('function_ids'))}
                need(entry['role'] in ROLES and (entry['role']!='function_admin' or entry['function_ids']))
                need(entry['role']!='location_admin' or not entry['function_ids'])
                need(set(entry['location_ids'])<={r['id'] for r in graph['locations']} and set(entry['function_ids'])<={r['id'] for r in graph['functions']})
                if not owner:
                    need(identity!=actor.principal_id and entry['role'] in ({'function_admin','colleague','alert_recipient'} if role=='location_admin' else {'colleague','alert_recipient'}),'scope_management_denied')
                    need(set(entry['location_ids'])<=set(effective['location_ids']),'scope_management_denied')
                    if role=='function_admin':need(entry['function_ids'] and set(entry['function_ids'])<=set(effective['function_ids']),'scope_management_denied')
                    if old:need(set(old['location_ids'])<=set(effective['location_ids']) and (role!='function_admin' or old['function_ids'] and set(old['function_ids'])<=set(effective['function_ids'])),'scope_management_denied')
            else:
                entry={'id':identity,'name':label(value.get('name'))}
                if kind=='locations':entry['address']=label(value.get('address',''),500,True)
                if kind in {'rooms','cameras'}:
                    location=identifier(value.get('location_id')); need(location in {r['id'] for r in graph['locations']})
                    need(owner or location in effective['location_ids'] and (old is None or old['location_id'] in effective['location_ids']),'scope_management_denied')
                    entry['location_id']=location
                    if kind=='rooms':
                        entry['function_ids']=ids(value.get('function_ids'));need(set(entry['function_ids'])<={r['id'] for r in graph['functions']})
                        if old and location!=old['location_id']:
                            need(not any(identity in c['room_ids'] for c in graph['cameras']),'scope_room_in_use')
                    else:
                        entry['room_ids']=ids(value.get('room_ids'));entry['source_ids']=ids(value.get('source_ids',[]))
                        protected=set(old['source_ids'])-set(effective['source_ids']) if old and not owner else set()
                        entry['source_ids']=sorted(set(entry['source_ids'])|protected)
                        need(entry['room_ids'] and set(entry['room_ids'])<={r['id'] for r in graph['rooms'] if r['location_id']==location})
                        need(owner or set(entry['room_ids'])<=set(effective['room_ids']) and (old is None or old['id'] in effective['camera_ids']),'scope_management_denied')
                        for source in entry['source_ids']:
                            need(source in protected or self.source_authorized(actor,collection,source) is True,'scope_source_denied')
                            need(not any(source in c['source_ids'] for c in rows if c['id']!=identity),'scope_source_already_bound')
                        changed=old is not None and (old['location_id']!=location or old['room_ids']!=entry['room_ids'])
                        entry['view_revision']=old['view_revision']+int(changed) if old else 1
                        entry['views']=copy.deepcopy(old.get('views',[])) if old else []
                        if changed:entry['views'].append({k:copy.deepcopy(old[k]) for k in ('location_id','room_ids','view_revision')})
                        need(len(entry['views'])<=100,'scope_view_limit')
                        # source_ids identify exact original video_id/asset IDs,
                        # never a feed containing unrelated future recordings.
                        # A retained recording keeps its confirmed view; explicit
                        # removal withdraws current access while history remains.
                        prior_views=old.get('source_views',{}) if old else {}
                        entry['source_views']={source:copy.deepcopy(prior_views.get(source,
                            {k:entry[k] for k in ('location_id','room_ids','view_revision')})) for source in entry['source_ids']}
            if old:rows[rows.index(old)]=entry
            else:need(len(rows)<LIMITS[kind],'scope_capacity');rows.append(entry)
            body=json.dumps(graph,separators=(',',':'));need(len(body.encode())<=1024*1024,'scope_capacity')
            need(self.authority(actor,write=True)['role']==member['role'],'scope_authority_changed')
            if not old and revision==0:need(db.execute('SELECT count(*) FROM graphs').fetchone()[0]<64,'scope_capacity')
            db.execute('INSERT OR REPLACE INTO graphs VALUES(?,?,?)',(collection,revision+1,body))
            db.execute('INSERT INTO history VALUES(?,?,?,?,?)',(collection,revision+1,actor.principal_id,self.clock(),body))
            db.execute('DELETE FROM history WHERE collection=? AND revision<?',(collection,max(0,revision-100)))
            self._event(db,collection,revision+1,actor.principal_id,kind,identity,entry)
            need(self.authority(actor,write=True)['role']==member['role'],'scope_authority_changed')
            if kind=='members':
                target=self.member(identity)
                need(isinstance(target,dict) and target.get('role') in {'operator','browse'},'scope_member_unavailable')
        return self.describe(actor,collection)

    def _event(self,db,collection,revision,actor,kind,record_id,value):
        need(db.execute('SELECT count(*) FROM events').fetchone()[0]<20000,'scope_event_capacity')
        db.execute('INSERT INTO events(collection,revision,actor,kind,record_id,body,created) VALUES(?,?,?,?,?,?,?)',
            (collection,revision,actor,kind,record_id,json.dumps(value,separators=(',',':')),self.clock()))

    def events(self,actor,collection,*,after=0,limit=100):
        member=self.authority(actor);identifier(collection)
        need(member['role']=='owner','scope_management_denied')
        need(type(after)is int and after>=0 and type(limit)is int and 1<=limit<=100)
        with self._db() as db:
            rows=db.execute('SELECT * FROM events WHERE collection=? AND id>? ORDER BY id LIMIT ?',
                (collection,after,limit)).fetchall() if db else []
        need(self.authority(actor)['role']=='owner','scope_management_denied')
        return [{**dict(row),'body':json.loads(row['body'])} for row in rows]

    def bind_chat(self,actor,chat_id,collection,location_ids,expected_revision,*,expected_selection_revision=0):
        identifier(chat_id);identifier(collection);member=self.authority(actor,write=True)
        need(type(expected_selection_revision)is int and expected_selection_revision>=0)
        with self._db(True) as db:
            revision,graph=self._graph(db,collection);need(revision==expected_revision,'scope_revision_conflict')
            effective=self._effective(actor,self.authority(actor),graph,revision,collection,location_ids)
            need(effective['can_query'],'scope_query_denied')
            prior=db.execute('SELECT collection,revision FROM chats WHERE chat=?',(chat_id,)).fetchone()
            need(prior is None or prior['collection']==collection,'scope_chat_business_fixed')
            actual=prior['revision'] if prior else 0
            need(actual==expected_selection_revision,'scope_selection_revision_conflict')
            value=json.dumps(location_ids) if location_ids is not None else None
            db.execute('INSERT OR REPLACE INTO chats VALUES(?,?,?,?,?)',(chat_id,collection,value,actual+1,self.clock()))
            self._event(db,collection,revision,actor.principal_id,'chat_scope',chat_id,
                {'location_ids':location_ids,'selection_revision':actual+1})
            need(self.authority(actor,write=True)['role']==member['role'],'scope_authority_changed')
        return {**effective,'selection_revision':actual+1,'selected_location_ids':location_ids}

    def chat_scope(self,actor,chat_id,collection):
        identifier(chat_id);identifier(collection);self.authority(actor)
        with self._db() as db:
            row=db.execute('SELECT * FROM chats WHERE chat=?',(chat_id,)).fetchone() if db else None
            need(row is None or row['collection']==collection,'scope_chat_business_fixed')
            locations=json.loads(row['locations']) if row and row['locations'] else None
        # Every read re-intersects a saved selector with CURRENT membership. A
        # revoked selected location removes access instead of falling back to all.
        if locations is not None:
            available=set(self.effective(actor,collection)['location_ids']); locations=[x for x in locations if x in available]
        return {**self.effective(actor,collection,locations),'selection_revision':row['revision'] if row else 0,
            'selected_location_ids':json.loads(row['locations']) if row and row['locations'] else None}

    def history(self,chat_id):
        identifier(chat_id)
        with self._db() as db:
            if db is None or not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_history'").fetchone():return None
            row=db.execute('SELECT body,native_binding FROM chat_history WHERE chat=?',(chat_id,)).fetchone()
        return {**json.loads(row['body']),'native_binding':row['native_binding']} if row else None

    def admit_history(self,actor,chat_id,scopes,source_ids,*,legacy=False):
        """Bind all admitted context before any model/tool can expose it.

        The receipt is an upper bound, including available sources rather than
        guessing which sources influenced free-form text. It only accumulates.
        """
        identifier(chat_id);member=self.authority(actor,write=True)
        need(isinstance(scopes,list) and 1<=len(scopes)<=64)
        need(isinstance(source_ids,dict) and set(source_ids)=={s['collection'] for s in scopes})
        source_ids={key:ids(value,250000) for key,value in source_ids.items()}
        need(sum(map(len,source_ids.values()))<=250000,'scope_history_capacity')
        binding_scopes=[{key:value for key,value in scope.items() if key not in {'revision','selection_revision'}} for scope in scopes]
        binding=hashlib.sha256(json.dumps({'principal':actor.principal_id,'scopes':binding_scopes,
            'source_ids':source_ids},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with self._db(True) as db:
            for scope in scopes:
                revision,_=self._graph(db,scope['collection'])
                need(revision==scope['revision'],'scope_authority_changed')
                selected=db.execute('SELECT revision FROM chats WHERE chat=?',(chat_id,)).fetchone()
                need((selected[0] if selected else 0)==scope.get('selection_revision',0),'scope_authority_changed')
            prior=db.execute('SELECT body,native_binding FROM chat_history WHERE chat=?',(chat_id,)).fetchone()
            previous=json.loads(prior['body']) if prior else {'sources':{},'scopes':{},'legacy':legacy}
            for key,values in source_ids.items():previous['sources'][key]=sorted(set(previous['sources'].get(key,[]))|set(values))
            need(sum(map(len,previous['sources'].values()))<=250000,'scope_history_capacity')
            for scope in scopes:
                saved=previous['scopes'].setdefault(scope['collection'],{'location_ids':[],'room_ids':[],'function_ids':[]})
                saved.setdefault('camera_ids', [])
                for key in saved:saved[key]=sorted(set(saved[key])|set(scope.get(key,[])))
            body=json.dumps(previous,separators=(',',':'));need(len(body.encode())<=8*1024*1024,'scope_history_capacity')
            db.execute('INSERT OR REPLACE INTO chat_history VALUES(?,?,?)',(chat_id,body,binding))
            self._event(db,scopes[0]['collection'],scopes[0]['revision'],actor.principal_id,'history_admission',chat_id,
                {'binding':binding,'source_count':sum(map(len,source_ids.values())),'legacy':previous['legacy']})
            need(self.authority(actor,write=True)['role']==member['role'],'scope_authority_changed')
        return {'binding':binding,'reset':prior is None or prior['native_binding']!=binding,
            'sources':source_ids,'scopes':scopes}

    def current(self,actor,collection,revision,*,account_role=None,chat_id=None,selection_revision=None):
        """Cheap fail-closed fence for a previously admitted tool/media result.

        No schema creation, held transaction or cached membership authority.
        Rebuild the view after a false result; never widen an old view in place.
        """
        try:
            role=self.authority(actor)['role']
            if account_role is not None and role!=account_role:return False
            if not self.path.exists():return revision==0 and selection_revision in (None,0)
            info=self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:return False
            with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=0)) as db:
                if db.execute('SELECT company FROM identity').fetchall()!=[(self.company_id,)]:return False
                row=db.execute('SELECT revision FROM graphs WHERE collection=?',(collection,)).fetchone()
                if (row[0] if row else 0)!=revision:return False
                if chat_id is not None:
                    row=db.execute('SELECT collection,revision FROM chats WHERE chat=?',(chat_id,)).fetchone()
                    if row and row[0]!=collection or (row[1] if row else 0)!=selection_revision:return False
            return self.authority(actor)['role']==role
        except (ScopeError,OSError,sqlite3.Error):return False






def legacy_scope_current(state, collection=None):
    """Legacy local MCP has no live member resolver: configured scope uses HTTP.

    This probe opens no store, holds no transaction and caches no authority. The
    authenticated HTTP/native hosts use RetailBusinessScope with their AccessStore.
    """
    path=Path(state).absolute()/'business-scope.sqlite'
    try:
        if not path.exists():return True
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:return False
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0)) as db:
            if collection is None:return db.execute('SELECT 1 FROM graphs WHERE revision>0 LIMIT 1').fetchone() is None
            return db.execute('SELECT 1 FROM graphs WHERE collection=? AND revision>0',(collection,)).fetchone() is None
    except (OSError,sqlite3.Error):return False
