"""Narrow host integration for profile artwork and its parent-chat tools."""
import copy
import sqlite3
from pathlib import Path

from fastapi import HTTPException
from custom_coordinator import offload
from profile_covers import ProfileCovers, CoverError, SPEC
from profile_cover_generation import CoverGenerator, CoverScope, GENERATE_SPEC
from profile_cover_routes import register_profile_cover_routes, PRIVATE

TOOL_NAMES=frozenset({'publish_profile_cover','generate_profile_cover'})


def tool_specs():return copy.deepcopy([SPEC,GENERATE_SPEC])


class ProfileCoverHost:
    def __init__(self,selected,access,*,transport=None):
        self.selected=selected;self.access_path=Path(access.path);self.environment=selected['environment_id']
        self.transport=transport
        self.store=ProfileCovers(selected['state'],authorize=self.authorize)

    def member(self,actor_id):
        try:
            if self.access_path.is_symlink():return None
            with sqlite3.connect(self.access_path.absolute().as_uri()+'?mode=ro',uri=True,timeout=1)as db:
                row=db.execute('''SELECT p.id,p.display_name,m.role FROM principals p
                    JOIN principal_environments m ON m.principal_id=p.id JOIN environments e ON e.id=m.environment
                    WHERE p.id=? AND m.environment=? AND m.role IN ('owner','operator')''',(actor_id,self.environment)).fetchone()
            return {'id':row[0],'display_name':row[1],'role':row[2]}if row else None
        except sqlite3.Error:return None

    def authorize(self,actor_id,*,target,target_id,collection,write):
        actor=self.member(actor_id)
        if not actor or (collection!='company'and collection not in self.selected['index'].status().get('datasets',[])):
            return False
        if target=='business':return target_id==collection and (not write or actor['role']=='owner')
        if target=='member':return bool(self.member(target_id))and(not write or target_id==actor_id)
        return False

    def business(self,actor_id,collection,data):
        result={**data,'cover':self.store.get(actor_id=actor_id,target='business',target_id=collection,collection=collection)}
        result['cover_generation']={'tool':'generate_profile_cover','target':'business',
            'expected_profile_revision':data['profile_revision'],
            'instruction':'After saving the researched business profile, generate its light, dreamlike Monet-inspired cover. The host uses the saved business context and publishes it to the profile.'}
        return result

    def members(self,actor_id,collection,rows):
        result=[]
        for row in rows:
            member_id=row.get('id')or row.get('principal_id')or row.get('member_id')
            cover=self.store.get(actor_id=actor_id,target='member',target_id=member_id,collection=collection)
            result.append({**row,'cover':cover})
        return result

    async def tool(self,name,args,*,owner,actor_id,chat_id,collection,turn_id,current,emit=None):
        if name not in TOOL_NAMES or not isinstance(args,dict):raise CoverError('Choose a profile artwork tool.')
        allowed={'file_id','target','expected_profile_revision'}if name=='publish_profile_cover'else{'target','expected_profile_revision','direction'}
        if set(args)-allowed or 'target'not in args or(name=='publish_profile_cover'and'file_id'not in args):
            raise CoverError('Use the profile artwork tool’s declared fields.')
        target=args['target'];target_id=actor_id if target=='member'else collection
        def guard():
            record=self.selected['agent'].children.get(chat_id,{})
            organization=self.selected.get('organization_chats')
            return bool(current()and record.get('owner_principal_id')==owner and record.get('dataset')==collection
                and not record.get('parent_id')and(owner==actor_id or organization and organization.owns(record))
                and self.authorize(actor_id,target=target,target_id=target_id,collection=collection,write=True))
        if not guard():raise CoverError('Use your current owning parent chat to update this profile.')
        files=self.selected['agent']._files();business=self.selected['business']
        if name=='generate_profile_cover':
            auth=self.selected.get('auth')or self.selected['agent'].auth
            if auth is None or auth.mode!='openai_api':raise CoverError('Choose the company’s funded OpenAI route for profile artwork.')
            generator=await offload(CoverGenerator,self.selected['state'],
                api_key=auth.environment.get('ROCOTO_OPENAI_API_KEY'),transport=self.transport)
            scope=CoverScope(owner,actor_id,chat_id,collection,turn_id)
            return await generator.generate(scope,store=self.store,files=files,business=business,
                member=self.member(actor_id),current=guard,emit=emit,**args)
        created=[]
        try:
            cover=await offload(self.store.publish,files,business,owner=owner,actor_id=actor_id,chat_id=chat_id,
                collection=collection,current=guard,created=created,**args)
            if not guard():raise CoverError('The profile artwork publication has finished.')
            if emit:
                from inspect import isawaitable
                value=emit({'type':'profile_cover','cover':cover})
                if isawaitable(value):await value
            return {'cover':cover}
        except BaseException:
            if created:await offload(self.store.discard,created,owner=owner,chat_id=chat_id,collection=collection)
            raise


def install_profile_covers(selected,access,*,transport=None):
    host=ProfileCoverHost(selected,access,transport=transport)
    selected['profile_covers']=host
    selected['agent'].profile_covers=host
    return host


def register_routes(app):
    def context(request,cover_id):
        selected=request.state.workspace;session=request.state.session
        host=selected['profile_covers'];principal=selected['chats'].principal(session)
        if not host.member(principal)or not host.member((session.get('principal')or{}).get('id')):
            raise HTTPException(403,'profile_access_required',headers=PRIVATE)
        return host.store,principal,None
    register_profile_cover_routes(app,context_for_request=context)
