"""Distinct tracker IDs keep distinct activity timers and interaction highlights."""
from pathlib import Path
import shutil
import subprocess

import pytest

MODEL = Path(__file__).parents[1] / 'verticals/retail/web/control-room/journey-model.js'


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is required for player logic')
def test_activity_and_flash_keep_full_tracker_identity():
    script = r'''
const assert = require('node:assert/strict');
const model = require(process.argv[1]);
model.configure({event_types:{examine_object:{timer_label:'Examining item'},pick_up:{emphasis:'major'}}});
const examine = [{event:'examine_object',person_id:'Person P0',playback_start_s:1,playback_end_s:5}];
assert.equal(model.activityAt(examine,'0',2),null,'P0 timer must not attach to visitor0');
assert.equal(model.activityAt(examine,'P0',2).elapsed_s,1);
const pickup = [{event:'pick_up',person_id:'Person P0',playback_start_s:1,playback_end_s:2}];
assert.equal(model.majorInteractionAt(pickup,'0',1.2),null,'P0 action must not flash for visitor0');
assert.ok(model.majorInteractionAt(pickup,'P0',1.2).opacity>0);
const zero = [{event:'examine_object',person_id:'Person 0',playback_start_s:1,playback_end_s:5}];
assert.equal(model.activityAt(zero,'P0',2),null);
assert.equal(model.activityAt(zero,'0',2).elapsed_s,1);
'''
    result = subprocess.run(['node', '-e', script, str(MODEL)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
