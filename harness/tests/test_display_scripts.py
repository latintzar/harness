"""The shared script runtime admits generic jobs with the same source and quota fences."""
import asyncio
from pathlib import Path
import subprocess
import sys

import pytest


def test_shared_script_runtime_imports_with_the_bare_vertical():
    code = '''import sys
import layout
layout.install('bare')
from display_scripts import ScriptContract, ScriptSandbox, validate_source
validate_source('def run(input):\\n    return {"value": len(input)}\\n')
assert all('/verticals/retail/' not in str(getattr(module, '__file__', '')) for module in sys.modules.values())
'''
    subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).parents[1], check=True)


def test_script_source_is_parsed_and_never_executed(tmp_path):
    from display_scripts import ScriptError, validate_source
    marker = tmp_path / 'executed'
    value = f'from pathlib import Path\nPath({str(marker)!r}).touch()\ndef run(input):\n    return {{}}\n'
    assert validate_source(value) == value and not marker.exists()
    with pytest.raises(ScriptError):
        validate_source('async def run(input):\n    return {}\n')
    with pytest.raises(ScriptError):
        validate_source(value, max_bytes=10)


def test_display_job_limits_survive_restart_and_separate_company_budgets(tmp_path):
    from display_scripts import JobCounts, JobLimits
    clock = lambda: 1000
    limits = lambda: JobLimits(company=1, host=2, history=3, company_retained=2, host_retained=4)
    path = tmp_path / 'jobs.sqlite'
    one = JobCounts(path, clock=clock, limits=limits)
    mark = one.count('manufacturer')
    two = JobCounts(path, clock=clock, limits=limits)
    assert two.refused('manufacturer', retained=False) == 'company_daily_limit'
    assert two.refused('other', retained=False) is None
    assert two.refused('manufacturer', retained=True) is None
    two.uncount(mark)
    assert one.numbers('manufacturer') == {'host': 0, 'company': 0, 'total': 0}


def test_disabled_shared_script_runtime_never_prepares_a_job():
    from display_scripts import ScriptContract, ScriptSandbox
    def unexpected(*args, **kwargs):
        raise AssertionError('disabled execution reached its input provider')
    contract = ScriptContract(unexpected, unexpected, unexpected, lambda code: {'code': code})
    result = asyncio.run(ScriptSandbox(contract).run(company_id='company', owner='owner', collection='orders',
        display_id='orders-ready', revision=1, source='def run(input): return {}', document={}))
    assert result['status'] == 'unavailable' and result['error']['code'] == 'sandbox_offline'
    assert result['started'] is False
