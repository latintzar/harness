#!/usr/bin/env python3
"""Run the native bare workspace with included source files and owned local state."""
from pathlib import Path
from urllib.parse import urlsplit
import argparse,asyncio,json,os,signal,stat,sys
ROOT=Path(__file__).resolve().parents[1]
def private_json(path):
 s=path.lstat()
 if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_nlink!=1 or s.st_mode&0o077:raise ValueError('Use an owner-only workspace marker.')
 return json.loads(path.read_text())
def environment(state,guest,origin):
 home=state/'home';home.mkdir(parents=True,exist_ok=True,mode=0o700);home.chmod(0o700)
 env={k:v for k,v in os.environ.items() if k in {'PATH','LANG','LC_ALL','TMPDIR','SSL_CERT_FILE','SSL_CERT_DIR'}}
 env.update(HOME=str(home),XDG_CONFIG_HOME=str(home/'.config'),XDG_CACHE_HOME=str(home/'.cache'),XDG_DATA_HOME=str(home/'.local/share'),PYTHONUNBUFFERED='1',PYTHONDONTWRITEBYTECODE='1',ROCOTO_VERTICAL='bare',ROCOTO_BROWSE_SESSIONS='false',ROCOTO_READ_ONLY='true' if guest else 'false')
 if guest:env['ROCOTO_SESSION_COOKIE']='agent_workspace_session'
 if origin:env['ROCOTO_PUBLIC_ORIGIN']=origin
 return env
async def prepare(app,state,access):
 from access import COOKIE
 import httpx
 code=access.issue('demo',owner=True,principal_id='owner',display_name='Owner')
 async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver') as client:
  response=await client.post('/api/session',json={'code':code},headers={'X-Rocoto-Page':'1'})
  if response.status_code!=200:raise RuntimeError('Initialize the native local owner session.')
  token=response.cookies.get(COOKIE)
  if token:access.logout(token)
 selected=app.state.workspaces['demo'];record=selected['organization_chats'].ensure(selected['agent'],'company','organization');files=selected['files'];owner=selected['organization_chats'].owner
 saved={x['name']:x for x in files.list(owner_principal_id=owner,chat_id=record['id'],dataset='company')}
 templates=[('bare-vertical.json','Vertical definition',ROOT/'harness/verticals/bare/vertical.json'),('reader-roles.json','Reader roles',ROOT/'harness/verticals/bare/sub_agents.json')]
 for index,path in enumerate(sorted((ROOT/'harness/verticals/bare/prompts').glob('*.md')),1):templates.append((f'instructions-{index}.md',path.stem.replace('-',' ').capitalize(),path))
 for name,title,path in templates:
  if name not in saved:files.create({'name':name,'title':title,'content':path.read_text()},owner_principal_id=owner,chat_id=record['id'],dataset='company')
 code=access.issue('demo',owner=True,principal_id='owner',display_name='Owner');target=state/'owner-code.txt';target.write_text(code+'\n');target.chmod(0o600)
 return {'organization_chat':record['id'],'source_files':len(templates)}
def main():
 os.umask(0o077)
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state',type=Path,default=Path.home()/'.local/state/agent-workspace-local');parser.add_argument('--port',type=int,default=8780);parser.add_argument('--guest',action='store_true',help='Serve public state through the native read-only observer');parser.add_argument('--base-path',default='');parser.add_argument('--public-origin',default='');args=parser.parse_args()
 if not 1024<=args.port<=65535:parser.error('Choose an unprivileged local port.')
 if args.public_origin:
  u=urlsplit(args.public_origin)
  if u.scheme!='https' or not u.hostname or u.username or u.password or u.path not in {'','/'} or u.query or u.fragment:parser.error('Use the exact recipient HTTPS origin.')
  args.public_origin=args.public_origin.rstrip('/')
 state=args.state.expanduser().absolute()
 if state.is_symlink() or state.resolve().is_relative_to(ROOT.resolve()):raise ValueError('Choose an owned private state directory outside the checkout.')
 state.mkdir(parents=True,exist_ok=True,mode=0o700);state.chmod(0o700);marker=state/'.agent-workspace-local.json';audience='guest' if args.guest else 'owner'
 if marker.exists():
  declared=private_json(marker)
  if declared.get('schema')!='agent-workspace-local-v1' or declared.get('audience')!=audience:raise ValueError('Use a separate state directory for this workspace mode.')
 else:
  if any(state.iterdir()):raise ValueError('Choose fresh state for the local starter.')
  marker.write_text(json.dumps({'schema':'agent-workspace-local-v1','audience':audience})+'\n');marker.chmod(0o600)
 env=environment(state,args.guest,args.public_origin);os.environ.clear();os.environ.update(env)
 import offline;offline.install()
 sys.path.insert(0,str(ROOT/'harness'));import layout;layout.install('bare')
 from web_mount import MountedApp,base_path
 args.base_path=base_path(args.base_path)
 import server,uvicorn
 app=server.create_app(state);receipt=asyncio.run(prepare(app,state,app.state.access));(state/'starter.json').write_text(json.dumps(receipt,indent=2)+'\n')
 if args.guest:
  from native_visitor import NativeVisitor
  app=NativeVisitor(app,secure=bool(args.public_origin))
 app=MountedApp(app,prefix=args.base_path,read_only=args.guest,public_origin=args.public_origin)
 print('Workspace: '+(args.public_origin or f'http://127.0.0.1:{args.port}')+args.base_path+'/',flush=True);print('Owner code file: '+str(state/'owner-code.txt'),flush=True)
 def stop(*_):raise KeyboardInterrupt
 signal.signal(signal.SIGTERM,stop)
 try:uvicorn.run(app,host='127.0.0.1',port=args.port,access_log=False)
 except KeyboardInterrupt:pass
 finally:(state/'network.json').write_text(json.dumps(offline.counts(),indent=2)+'\n')
if __name__=='__main__':main()
