import hashlib,json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]))
import native_install_worker as worker
import native_package_mount as mount
from native_packages import _scan_tree

def test_prefix_sibling_inventory_roundtrips_into_broker_and_host(tmp_path):
    for n in ('.codex-plugin/plugin.json','skills/review/SKILL.md','skills/review-resolver/SKILL.md'):
        p=tmp_path/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{}')
    inventory=worker.tree_inventory(tmp_path)
    package={'id':'test','version':'1','tree_sha256':mount.digest(inventory),**inventory}
    owner=hashlib.sha256(b'owner').hexdigest()
    value={'company':'company','owner':owner,'generation':1,'packages':[package]}
    value['selection_sha256']=mount.digest({'company':'company','owner':owner,'generation':1,'packages':[('test','1',package['tree_sha256'])]})
    assert mount.validate_metadata(value,'company','owner')==value
    assert _scan_tree(tmp_path)[1]==package['tree_sha256']

def package(tmp_path,root_manifest):
    tree=tmp_path/mount.MARKETPLACE/'test/1';(tree/'.codex-plugin').mkdir(parents=True)
    (tree/'.codex-plugin/plugin.json').write_text(json.dumps({'name':'test','version':'1'}))
    (tree/'plugin.json').write_text(json.dumps(root_manifest))
    return tree,{'packages':[{'id':'test','version':'1','files':[{'path':'.codex-plugin/plugin.json'},{'path':'plugin.json'}]}]}

def test_original_metadata_only_agent_manifest_preserved_and_enabled(tmp_path):
    original={'$schema':'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json','name':'test','version':'1','author':{'name':'Original publisher'}}
    tree,metadata=package(tmp_path,original);before=(tree/'plugin.json').read_bytes()
    assert mount.native_config(tmp_path,metadata)=={'test@rocoto-pinned-api':{'enabled':True,'mcp_servers':{}}}
    assert (tree/'plugin.json').read_bytes()==before

def test_unrelated_root_json_does_not_replace_codex_manifest(tmp_path):
    _,metadata=package(tmp_path,{'name':'unrelated-project','config':{'x':1}})
    assert mount.native_config(tmp_path,metadata)['test@rocoto-pinned-api']['enabled'] is True

@pytest.mark.parametrize('change',[{'name':'other'},{'version':'2'},{'$schema':'https://agent-plugins.org/schemas/2.0.0/plugin.schema.json'},{'extensions':{'com.openai':{'mcpServers':{'unexpected':{'command':'unexpected'}}}}}])
def test_agent_manifest_identity_or_unreviewed_extensions_stay_closed(tmp_path,change):
    original={'$schema':'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json','name':'test','version':'1'}|change
    _,metadata=package(tmp_path,original)
    with pytest.raises(mount.PackageMountError):mount.native_config(tmp_path,metadata)

@pytest.mark.parametrize('name',['mcp.json','.agents/plugins'])
def test_new_discovery_paths_require_matching_host_policy(tmp_path,name):
    tree,metadata=package(tmp_path,{'$schema':'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json','name':'test','version':'1'})
    path=tree/name;path.parent.mkdir(exist_ok=True);path.write_text('{}')
    with pytest.raises(mount.PackageMountError):mount.native_config(tmp_path,metadata)

def test_mount_target_explicit_traversal_modes_under_service_umask(tmp_path):
    import os,stat
    (tmp_path/'codex-state').mkdir()
    previous=os.umask(0o077)
    try:mount.ensure_mount_target(tmp_path)
    finally:os.umask(previous)
    for path in ('codex-state/plugins','codex-state/plugins/cache'):
        assert stat.S_IMODE((tmp_path/path).stat().st_mode)==0o755

def test_source_descriptor_reader_preserves_original_directory_modes(tmp_path):
    import stat
    parent=tmp_path/'private/skills';parent.mkdir(mode=0o700,parents=True);(parent/'SKILL.md').write_text('original')
    before={p:p.stat().st_mode for p in [parent.parent,parent]}
    with mount.open_source(tmp_path,'private/skills/SKILL.md') as stream:assert stream.read()==b'original'
    assert {p:p.stat().st_mode for p in before}==before
