"""External packages retain original metadata and require host immutable bindings."""
import copy
import io
import json
import hashlib
import tarfile
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))
import plugin_catalogue as catalogue
from native_install_source import source_spec, InstallSourceError
from native_packages import StablePluginCLI

REV='a'*40
PIN='b'*40

def fixture(subtree=''):
    source={'source':'git-subdir' if subtree else 'url','url':'https://github.com/publisher/example.git'}
    if subtree:source['path']=subtree
    row={'id':'example','name':'Original catalogue title','supported_api':True,'metadata_status':'external_source',
         'catalogue_entry':{'name':'example','source':source},'category':'Developer Tools','skills':[], 'apps':[], 'mcp_servers':[]}
    manifest={'name':'example','version':'1.0','license':'MIT','skills':'./skills',
      'interface':{'displayName':'Exact name','shortDescription':'Exact short.','longDescription':'Exact long.',
       'defaultPrompt':['Original prompt.'],'logo':'./assets/logo.png'}}
    prefix=subtree+'/' if subtree else ''
    files={prefix+'.codex-plugin/plugin.json':json.dumps(manifest).encode(),
           prefix+'skills/one/SKILL.md':b'---\nname: one\ndescription: Exact skill.\n---\nA workflow body.',
           prefix+'assets/logo.png':b'logo',prefix+'README.md':b'Setup: https://docs.publisher.com/setup',
           'LICENSE':b'MIT License\nOriginal copyright notice\n'}
    content=io.BytesIO()
    with tarfile.open(fileobj=content,mode='w:gz') as bundle:
        for path,data in files.items():
            member=tarfile.TarInfo('example-'+PIN+'/'+path);member.size=len(data)
            bundle.addfile(member,io.BytesIO(data))
    archive=content.getvalue()
    pin={'id':'example','repository':'publisher/example','revision':PIN,'subtree':subtree,
      'archive_sha256':hashlib.sha256(archive).hexdigest(),'archive_bytes':len(archive),'license_path':'LICENSE'}
    return {'revision':REV,'plugins':[row]},pin,archive,manifest

@pytest.mark.parametrize('subtree',['','codex-packages/example'])
def test_materializes_exact_original_metadata_and_preserves_catalogue_entry(subtree):
    snapshot,pin,archive,manifest=fixture(subtree)
    original=copy.deepcopy(snapshot)
    result=catalogue.materialize_external(snapshot,[pin],lambda value:archive)
    row=result['plugins'][0]
    assert snapshot==original
    assert row['catalogue_entry']==original['plugins'][0]['catalogue_entry']
    assert row['metadata_status']=='available' and row['manifest']==manifest
    assert row['description']=='Exact short.' and row['long_description']=='Exact long.'
    assert row['prompts']==['Original prompt.'] and row['skills'][0]['description']=='Exact skill.'
    assert row['skills'][0]['source_url'].startswith('https://github.com/publisher/example/blob/'+PIN+'/')
    assert row['logo_url'].startswith('https://raw.githubusercontent.com/publisher/example/'+PIN+'/')
    assert row['connection_setup']['instructions'][0]['source_url'].startswith('https://github.com/publisher/example/blob/'+PIN+'/')
    assert row['source_provenance']['license_notice']['text']=='MIT License\nOriginal copyright notice\n'
    assert 'A workflow body.' not in json.dumps(row)
    spec=source_spec(row,result)
    assert spec.repository=='publisher/example' and spec.revision==PIN and spec.subtree==subtree
    cli=StablePluginCLI._source(None,row,result,None)
    assert cli['sha']==PIN and cli['url']==row['catalogue_entry']['source']['url']

@pytest.mark.parametrize('field,value',[('revision','main'),('repository','different/repository'),('subtree','../escape'),('archive_sha256','c'*64),('archive_bytes',0)])
def test_external_pin_and_archive_must_match_host_catalogue(field,value):
    snapshot,pin,archive,_=fixture()
    pin[field]=value
    with pytest.raises(ValueError):catalogue.materialize_external(snapshot,[pin],lambda value:archive)

@pytest.mark.parametrize('change',[{'url':'https://github.com/other/repo.git'},{'path':'other'},{'sha':'main'},{'source':'local','path':'elsewhere'}])
def test_resolved_source_cannot_change_published_origin_or_choose_mutable_revision(change):
    snapshot,pin,archive,_=fixture('codex-packages/example')
    row=snapshot['plugins'][0]
    row['resolved_source']={**row['catalogue_entry']['source'],'sha':PIN,**change}
    with pytest.raises(InstallSourceError):source_spec(row,snapshot)


def test_published_external_packages_have_complete_original_metadata_and_pins():
    snapshot=catalogue.catalogue()
    rows={row['id']:row for row in snapshot['plugins']}
    counts={'crowdstrike-falcon-foundry':12,'crowdstrike-falcon-fusion':7,'qodo':4}
    for identifier,count in counts.items():
        row=rows[identifier]
        assert row['metadata_status']=='available'
        assert len(row['skills'])==count and row['license']=='MIT'
        assert 'sha' not in row['catalogue_entry']['source']
        assert source_spec(row,snapshot).revision==row['source_provenance']['revision']
        assert row['source_provenance']['license_notice']['text'].startswith('MIT License')

@pytest.mark.parametrize('kind',['symlink','duplicate','traversal','wrong-root','manifest-name'])
def test_archive_ambiguity_and_manifest_mismatch_fail_before_metadata_publication(kind):
    snapshot,pin,archive,_=fixture()
    members=[]
    with tarfile.open(fileobj=io.BytesIO(archive),mode='r:gz') as source:
        for member in source:
            members.append((member,source.extractfile(member).read()))
    if kind=='manifest-name':
        member,data=members[0];value=json.loads(data);value['name']='other';data=json.dumps(value).encode();member.size=len(data);members[0]=(member,data)
    else:
        name='example-'+PIN+'/bad'
        if kind=='traversal':name='example-'+PIN+'/../escape'
        if kind=='wrong-root':name='other-'+PIN+'/bad'
        member=tarfile.TarInfo(name);data=b''
        if kind=='symlink':member.type=tarfile.SYMTYPE;member.linkname='/private'
        if kind=='duplicate':member,data=members[0]
        members.append((member,data))
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as target:
        for member,data in members:target.addfile(member,io.BytesIO(data))
    archive=stream.getvalue();pin.update(archive_sha256=hashlib.sha256(archive).hexdigest(),archive_bytes=len(archive))
    original=copy.deepcopy(snapshot)
    with pytest.raises(ValueError):catalogue.materialize_external(snapshot,[pin],lambda value:archive)
    assert snapshot==original


def test_external_refresh_preserves_other_sixty_two_packages_verbatim():
    current=catalogue.catalogue()
    external={'crowdstrike-falcon-foundry','crowdstrike-falcon-fusion','qodo'}
    untouched=[row for row in current['plugins'] if row['id'] not in external]
    digest=hashlib.sha256(json.dumps(untouched,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert len(untouched)==62 and digest=='ca73ec50e378bc5382d366c6e5dec1d57b799764bd505798e1790e95b185a801'
