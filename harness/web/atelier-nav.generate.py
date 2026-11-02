#!/usr/bin/env python3
"""Regenerate this vendored navigation from its adjacent editable public snapshot."""
import argparse,hashlib,json,re
from pathlib import Path
from urllib.parse import urlsplit
HERE=Path(__file__).resolve().parent
INPUTS=('atelier-nav.css','atelier-asterisk.svg','atelier-nav-fonts/archivo-variable.woff2','atelier-nav-fonts/Archivo-OFL.txt','atelier-nav.LICENSE','atelier-nav.LUCIDE-LICENSE','atelier-nav.README.md','atelier-nav.PROVENANCE.json','atelier-nav.template.js','atelier-nav.icons.json','atelier-nav.catalog.json','atelier-nav.generate.py')
def serial(value):return (json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode()
def digest(value):return hashlib.sha256(value).hexdigest()
def build(here=HERE,overrides=None):
 files={name:(here/name).read_bytes() for name in INPUTS}
 if overrides:
  assert set(overrides).issubset({'atelier-nav.catalog.json','atelier-nav.icons.json'})
  files.update(overrides)
 items=json.loads(files['atelier-nav.catalog.json']);icons=json.loads(files['atelier-nav.icons.json'])
 assert items and len({p['id'] for p in items})==len(items),'Project IDs must be unique'
 for item in items:
  assert re.fullmatch('[a-z0-9-]+',item['id']) and item['name']
  for key in ('entry','docs','site','repo','thumbnail'):
   u=urlsplit(item[key]);assert u.scheme in ('https','http') and u.netloc and not u.username and not u.password,(item['id'],key)
 assert set(icons)=={'grid','book','close'}
 version='v2-'+digest(serial({'schema':2,'files':{p:digest(v) for p,v in files.items()}}))[:16]
 source=files['atelier-nav.template.js'].decode().replace('__CATALOG__',json.dumps(items,ensure_ascii=True,separators=(',',':'))).replace('__ICONS__',json.dumps(icons,ensure_ascii=True)).replace('__VERSION__',version).replace('__MARK__',json.dumps(files['atelier-asterisk.svg'].decode(),ensure_ascii=True))
 assert not any(v in source for v in ('__CATALOG__','__ICONS__','__VERSION__','__MARK__'))
 files['atelier-nav.js']=source.encode();files['atelier-nav.manifest.json']=serial({'schema_version':2,'version':version,'self_excluded':True,'files':[{'path':p,'bytes':len(v),'sha256':digest(v)} for p,v in sorted(files.items())]})
 return version,files

def write(destination,files,check=False):
 stale=[]
 for rel,data in files.items():
  path=destination/rel
  if path.is_file() and path.read_bytes()==data:continue
  stale.append(rel)
  if not check:
   path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_name(path.name+'.tmp');temporary.write_bytes(data);temporary.replace(path)
 return stale

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--check',action='store_true');parser.add_argument('--out',type=Path);args=parser.parse_args()
 version,files=build();destination=args.out or HERE;stale=write(destination,files,args.check)
 print(json.dumps({'version':version,'files':len(files),'stale':stale,'checked':args.check}))
 return 1 if args.check and stale else 0
if __name__=='__main__':raise SystemExit(main())
