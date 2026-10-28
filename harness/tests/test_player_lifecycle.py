"""Run deterministic browser lifecycle/worker scheduling regressions with Node."""
from pathlib import Path
import subprocess
import pytest

@pytest.mark.parametrize('script', ['player_lifecycle.cjs', 'person_outline.cjs'])
def test_browser_lifecycle(script):
    subprocess.run(['node', str(Path(__file__).with_name(script))], check=True, capture_output=True, text=True)
