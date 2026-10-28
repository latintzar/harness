"""One-use environment codes and 24-hour private browser sessions."""
import argparse
import os
import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

COOKIE = os.environ.get('ROCOTO_SESSION_COOKIE', 'rocoto_session')
if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', COOKIE):
    raise ValueError('Use a short application session cookie name.')
LEGACY_COOKIE = 'rocoto_video_session'
DAY = 24 * 60 * 60

def page_request(headers):
    """Admit one browser page marker; the canonical header takes precedence."""
    if hasattr(headers, 'getlist'):
        values = headers.getlist('x-rocoto-page') or headers.getlist('x-video-demo')
        return values == ['1']
    current = headers.get('x-rocoto-page')
    return (current if current is not None else headers.get('x-video-demo')) == '1'

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

class AccessStore:
    def __init__(self, state):
        self.state = Path(state)
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.state/'access.sqlite'
        with self.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS environments (id TEXT PRIMARY KEY, name TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS codes (hash TEXT PRIMARY KEY, environment TEXT NOT NULL, expires REAL NOT NULL, used REAL);
              CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, environment TEXT NOT NULL, grants TEXT NOT NULL, expires REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS principals (id TEXT PRIMARY KEY, display_name TEXT NOT NULL, created_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS principal_environments (
                principal_id TEXT NOT NULL, environment TEXT NOT NULL, role TEXT NOT NULL,
                PRIMARY KEY(principal_id,environment));
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns = [r[1] for r in db.execute('PRAGMA table_info(sessions)')]
            if 'can_query' not in columns:
                db.execute('ALTER TABLE sessions ADD COLUMN can_query INTEGER NOT NULL DEFAULT 0')
            for name,definition in [('principal_id','TEXT'),('role',"TEXT NOT NULL DEFAULT 'operator'"),
                                    ('observer_id','TEXT'),('observer_return_env','TEXT')]:
                if name not in columns:db.execute(f'ALTER TABLE sessions ADD COLUMN {name} {definition}')
            code_columns=[r[1] for r in db.execute('PRAGMA table_info(codes)')]
            for name,definition in [('principal_id','TEXT'),('role',"TEXT NOT NULL DEFAULT 'operator'")]:
                if name not in code_columns:db.execute(f'ALTER TABLE codes ADD COLUMN {name} {definition}')
            db.execute('INSERT OR IGNORE INTO environments VALUES (?,?)',('demo','Demo company'))
            # A legacy token establishes a session, not an identity for old chats.
            # Never infer ownership from a wildcard or the number of grants.
            for row in db.execute('SELECT * FROM sessions WHERE principal_id IS NULL').fetchall():
                principal='legacy-'+digest(row['hash'])[:24]
                role='operator' if row['can_query'] else 'browse'
                db.execute('INSERT OR IGNORE INTO principals VALUES(?,?,?)',(principal,'Legacy session',time.time()))
                db.execute('UPDATE sessions SET principal_id=?,role=? WHERE hash=?',(principal,role,row['hash']))
                for environment in json.loads(row['grants']):
                    db.execute('INSERT OR IGNORE INTO principal_environments VALUES(?,?,?)',(principal,environment,role))
        self.path.chmod(0o600)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        return db

    def create_environment(self, key, name):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',key): raise ValueError('Use a short company identifier.')
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO environments VALUES (?,?)',(key,name))
        directory = self.state if key=='demo' else self.state/'environments'/key
        directory.mkdir(parents=True,mode=0o700,exist_ok=True)
        return directory

    def issue(self, environment, owner=False, now=None, *, principal_id=None, display_name=None):
        now=time.time() if now is None else now
        if type(owner) is not bool:raise ValueError('Choose the explicit owner or operator role.')
        principal_id=principal_id or 'person-'+uuid.uuid4().hex
        if not isinstance(principal_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',principal_id):
            raise ValueError('Use a stable principal identifier.')
        if display_name is not None and (not isinstance(display_name,str) or not display_name.strip() or len(display_name)>80 or any(ord(c)<32 for c in display_name)):
            raise ValueError('Use the person’s display name, up to 80 characters.')
        role='owner' if owner else 'operator'
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM environments WHERE id=?',(environment,)).fetchone():
                raise ValueError('Create the company environment first.')
            prior=db.execute('SELECT * FROM principals WHERE id=?',(principal_id,)).fetchone()
            if prior is None:
                db.execute('INSERT INTO principals VALUES(?,?,?)',(principal_id,display_name.strip() if display_name else ('Owner' if owner else 'Operator'),now))
            elif display_name is not None:
                db.execute('UPDATE principals SET display_name=? WHERE id=?',(display_name.strip(),principal_id))
            db.execute('INSERT OR REPLACE INTO principal_environments VALUES(?,?,?)',(principal_id,environment,role))
            code=secrets.token_urlsafe(24)
            db.execute('INSERT INTO codes(hash,environment,expires,used,principal_id,role) VALUES (?,?,?,NULL,?,?)',
                (digest(code),'*' if owner else environment,now+DAY,principal_id,role))
        return code

    def login(self, code, now=None, *, can_query=True):
        now=time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM codes WHERE hash=?',(digest(code.strip()),)).fetchone()
            if not row or row['used'] is not None or row['expires']<=now: raise ValueError('Enter a fresh access code.')
            envs=db.execute('SELECT id FROM environments ORDER BY id').fetchall() if row['environment']=='*' else [{'id':row['environment']}]
            grants=[r['id'] for r in envs]
            selected='demo' if 'demo' in grants else grants[0]
            token=secrets.token_urlsafe(32)
            principal=row['principal_id'] or 'legacy-code-'+digest(row['hash'])[:24]
            role=row['role'] if row['role']=='owner' or can_query else 'browse'
            if role not in {'owner','operator','browse'}:raise ValueError('Use a code with an explicit workspace role.')
            db.execute('INSERT OR IGNORE INTO principals VALUES(?,?,?)',(principal,'Legacy code',now))
            for environment in grants:
                db.execute('INSERT OR REPLACE INTO principal_environments VALUES(?,?,?)',(principal,environment,role))
            db.execute('UPDATE codes SET used=? WHERE hash=?',(now,digest(code.strip())))
            db.execute('INSERT INTO sessions(hash,environment,grants,expires,can_query,principal_id,role) VALUES (?,?,?,?,?,?,?)',
                (digest(token),selected,json.dumps(grants),now+DAY,int(can_query),principal,role))
        return token, self.session(token,now)

    def session(self, token, now=None):
        now=time.time() if now is None else now
        if not token: return None
        with self.connect() as db:
            row=db.execute('SELECT * FROM sessions WHERE hash=? AND expires>?',(digest(token),now)).fetchone()
            if not row: return None
            grants=json.loads(row['grants'])
            environments=[dict(r) for r in db.execute('SELECT * FROM environments ORDER BY id') if r['id'] in grants]
            principal=db.execute('SELECT id,display_name FROM principals WHERE id=?',(row['principal_id'],)).fetchone()
            selected=next((e for e in environments if e['id']==row['environment']),None)
            if not principal or selected is None or row['role'] not in {'owner','operator','browse'}:return None
            viewing_as=None
            if row['observer_id'] is not None:
                target=db.execute('SELECT p.id,p.display_name FROM principals p JOIN principal_environments e ON e.principal_id=p.id WHERE p.id=? AND e.environment=?',
                    (row['observer_id'],row['environment'])).fetchone()
                if row['role']!='owner' or target is None:return None
                viewing_as={'principal':dict(target),'environment':selected,'read_only':True}
        can_query=bool(row['can_query']) and viewing_as is None and row['role']!='browse'
        return {'authenticated':True,'can_query':can_query,'read_only':not can_query,
                'role':row['role'],'principal':dict(principal),'viewing_as':viewing_as,
                'effective_principal_id':row['observer_id'] or row['principal_id'],'environment':selected,
                'environments':environments,'expires_at':row['expires']}

    def switch(self, token, environment):
        current=self.session(token)
        if not current or environment not in [e['id'] for e in current['environments']]:
            raise ValueError('Use a code issued for this company.')
        if current['viewing_as'] is not None:raise ValueError('Return to your workspace before switching companies.')
        with self.connect() as db:
            db.execute('UPDATE sessions SET environment=? WHERE hash=?',(environment,digest(token)))
        return self.session(token)

    def principals(self,token):
        current=self.session(token)
        if not current or current['role']!='owner':raise ValueError('Use an owner session to review workspace users.')
        allowed={e['id']:e for e in current['environments']};people={}
        with self.connect() as db:
            rows=db.execute('SELECT p.id,p.display_name,e.environment,e.role FROM principals p JOIN principal_environments e ON e.principal_id=p.id ORDER BY p.display_name,p.id').fetchall()
        for row in rows:
            if row['environment'] in allowed:
                person=people.setdefault(row['id'],{'id':row['id'],'display_name':row['display_name'],'environments':[]})
                person['environments'].append({**allowed[row['environment']],'role':row['role']})
        return list(people.values())

    def observe(self,token,environment_id,principal_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM sessions WHERE hash=? AND expires>?',(digest(token or ''),time.time())).fetchone()
            if (not row or row['role']!='owner' or environment_id not in json.loads(row['grants'])
                    or not db.execute('SELECT 1 FROM principal_environments WHERE principal_id=? AND environment=?',(principal_id,environment_id)).fetchone()):
                raise ValueError('Choose an actual user within your owner workspace grants.')
            db.execute('UPDATE sessions SET observer_return_env=COALESCE(observer_return_env,environment),environment=?,observer_id=? WHERE hash=?',
                (environment_id,principal_id,digest(token)))
        return self.session(token)

    def clear_observer(self,token):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM sessions WHERE hash=? AND expires>?',(digest(token or ''),time.time())).fetchone()
            if not row or row['role']!='owner':raise ValueError('Use your owner session to return to your workspace.')
            selected=row['observer_return_env'] or row['environment']
            if selected not in json.loads(row['grants']):raise ValueError('Refresh the current owner workspace grants.')
            db.execute('UPDATE sessions SET environment=?,observer_id=NULL,observer_return_env=NULL WHERE hash=?',(selected,digest(token)))
        return self.session(token)

    def logout(self, token):
        with self.connect() as db: db.execute('DELETE FROM sessions WHERE hash=?',(digest(token or ''),))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,default=Path('/home/peter/.local/state/rocoto/video-mvp'))
    parser.add_argument('--environment',default='demo')
    parser.add_argument('--name')
    parser.add_argument('--owner',action='store_true')
    parser.add_argument('--principal-id')
    parser.add_argument('--display-name')
    parser.add_argument('--output',type=Path,required=True,help='Private file receiving the one-use code')
    args=parser.parse_args()
    store=AccessStore(args.state)
    if args.name: store.create_environment(args.environment,args.name)
    code=store.issue(args.environment,args.owner,principal_id=args.principal_id,display_name=args.display_name)
    args.output.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=__import__('os').open(args.output,__import__('os').O_WRONLY|__import__('os').O_CREAT|__import__('os').O_EXCL,0o600)
    with __import__('os').fdopen(fd,'w') as f: f.write(code+'\n')
    print('One-use code saved to the requested private file; valid for 24 hours.')
