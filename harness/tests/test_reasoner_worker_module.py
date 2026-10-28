"""The exact uploaded reasoner module imports in its remote image layout."""
from pathlib import Path
import shutil
import subprocess
import sys


def test_reasoner_module_keeps_host_intake_dependencies_out_of_remote_import(tmp_path):
    source=Path(__file__).parents[1]/'verticals/retail/cosmos_inspect.py'
    target=tmp_path/source.name
    shutil.copyfile(source,target)
    result=subprocess.run([sys.executable,'-I','-c',
        'import runpy,sys; m=runpy.run_path(sys.argv[1]); '
        'assert callable(m["inspect_batch"]); '
        'assert m["execution_profile"](m["MODEL"])["gpu"] == "B200"',str(target)],
        cwd=tmp_path,capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
