"""Retail setting names retain installed configuration and explicit withdrawal semantics."""
import pytest


def test_realtime_authority_uses_new_name_before_installed_alias():
    from realtime_read_routes import read_authority
    assert read_authority({'ROCOTO_RETAIL_REALTIME_READS':'maintained',
        'ROCOTO_VIDEO_REALTIME_READS':'legacy'})=='maintained'
    with pytest.raises(ValueError):
        read_authority({'ROCOTO_RETAIL_REALTIME_READS':'', 'ROCOTO_VIDEO_REALTIME_READS':'maintained'})


def test_location_kill_switch_uses_primary_value():
    from location_context import context_enabled
    assert context_enabled({'ROCOTO_RETAIL_LOCATION_CONTEXT':'off',
        'ROCOTO_VIDEO_LOCATION_CONTEXT':'on'}) is False


def test_claiming_map_credentials_removes_primary_and_legacy_names():
    from location_context import claim_provider
    token='pk.'+'a'*30
    env={'ROCOTO_RETAIL_MAPBOX_TOKEN':token,'ROCOTO_VIDEO_MAPBOX_TOKEN':'pk.'+'b'*30,
        'ROCOTO_RETAIL_GOOGLE_MAPS_API_KEY':'c'*30,'ROCOTO_VIDEO_GOOGLE_MAPS_API_KEY':'d'*30,
        'PATH':'/usr/bin'}
    assert claim_provider(env)==('mapbox',token)
    assert env=={'PATH':'/usr/bin'}
    env={'ROCOTO_RETAIL_MAPBOX_TOKEN':'','ROCOTO_VIDEO_MAPBOX_TOKEN':token}
    assert claim_provider(env) is None and env=={}


def test_script_event_switch_can_withdraw_installed_authority():
    from tracker_scripts import switches
    assert switches({'ROCOTO_TILE_SCRIPT_EVENT_RUNS':'off','ROCOTO_VIDEO_TRACKER_SCRIPT_EVENT_RUNS':'on',
        'ROCOTO_CUSTOM_EXECUTOR_ENABLED':'1'})=={'sandbox':True,'event_runs':False}


def test_tile_runner_switch_can_stop_installed_runner(tmp_path):
    from tracker_runner import runner_state
    assert runner_state(tmp_path,tmp_path,{'ROCOTO_TILE_RUNNER':'off',
        'ROCOTO_VIDEO_TRACKER_RUNNER':'on'})['enabled'] is False


from retail_settings import ALIASES


@pytest.mark.parametrize('primary,legacy', ALIASES.items())
def test_every_installed_setting_retains_fallback_and_explicit_empty_precedence(primary,legacy):
    from retail_settings import value
    assert value(primary,'fallback',environ={})=='fallback'
    assert value(primary,environ={legacy:'installed'})=='installed'
    assert value(primary,environ={primary:'selected',legacy:'installed'})=='selected'
    assert value(primary,environ={primary:'',legacy:'installed'})==''


def test_invalid_map_credential_is_removed_before_child_environment_reuse():
    from location_context import claim_provider
    env={'ROCOTO_RETAIL_MAPBOX_TOKEN':'invalid','ROCOTO_VIDEO_MAPBOX_TOKEN':'pk.'+'a'*30}
    with pytest.raises(ValueError):claim_provider(env)
    assert env=={}


def test_cosmos_worker_import_keeps_its_single_file_image_contract():
    import subprocess
    import sys
    import layout
    result=subprocess.run([sys.executable,'-I','-c',
        'import importlib.util,sys; '
        'spec=importlib.util.spec_from_file_location("cosmos_worker",sys.argv[1]); '
        'module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); '
        'assert callable(module.inspect_batch)',str(layout.source('cosmos_inspect'))],
        capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
