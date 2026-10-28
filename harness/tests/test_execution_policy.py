import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from cosmos_inspect import MODEL, MODEL_REVISIONS, LIVE_SUPER_PROFILE, _cache_key, inspect_live
from execution_policy import FRESH_INSPECTION_ADMISSION, admit_fresh_inspection


class InspectionAdmissionTests(unittest.TestCase):
    def run_case(self, *, cached, compare=False, cache_profile=LIVE_SUPER_PROFILE, request_profile=None,
                 missing_runtime=False):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            config = state/'modal.toml'
            if not missing_runtime:
                config.write_text('[fixture]\nactive = true\n')
                config.chmod(0o600)
            ids = ['a', 'b'] if compare else ['a']
            records = {name: {'id':name, 'video_id':'source-' + name, 'path':'unused',
                             'start':0, 'end':8, 'camera':name, 'dataset':'test'} for name in ids}
            clips = [{**record, 'source_video_id':record['video_id'], 'sha256':'actual-input-hash'}
                     for record in records.values()]
            question = 'Describe visible actions.'
            requests = [{'clip_ids':ids, 'question':question}]
            key = _cache_key(clips, requests, MODEL, quality_profile=cache_profile)
            if cached:
                (state/'reasoner-cache').mkdir()
                (state/'reasoner-cache'/f'{key}.json').write_text(json.dumps({
                    'cache_key':key, 'model':MODEL, 'model_revision':MODEL_REVISIONS[MODEL]}))
            attempted = []
            def deny(maximum):
                attempted.append(maximum)
                raise ValueError('Scheduled run has used its fresh inspection allowance')
            token = FRESH_INSPECTION_ADMISSION.set(deny)
            try:
                with patch.dict('os.environ', {'ROCOTO_MODAL_CONFIG_PATH': str(config)}), \
                     patch('cosmos_inspect._reuse_prepared_source', return_value=True), \
                     patch('camera_context.get_context_for_clip', return_value=None), \
                     patch('cosmos_inspect.load_manifest', return_value=(clips, requests)), \
                     patch('cosmos_inspect.asyncio.create_subprocess_exec') as provider:
                    call = inspect_live(ids, question, index=SimpleNamespace(get_clip=records.get),
                                        state=state, modal_python=str(state/'missing-python') if missing_runtime else sys.executable, compare_views=compare,
                                        quality_profile=request_profile)
                    if cached and cache_profile == (request_profile or LIVE_SUPER_PROFILE):
                        result = asyncio.run(call)
                        self.assertTrue(result['cached'])
                        self.assertEqual(attempted, [])
                    else:
                        with self.assertRaisesRegex(ValueError, 'allowance'):
                            asyncio.run(call)
                        self.assertEqual(attempted, ['2.50'])
                    provider.assert_not_called()
            finally:
                FRESH_INSPECTION_ADMISSION.reset(token)

    def test_cache_reuse_skips_scheduled_fresh_admission(self):
        self.run_case(cached=True)

    def test_cached_observation_needs_neither_modal_python_nor_account_config(self):
        self.run_case(cached=True, missing_runtime=True)

    def test_new_detail_profile_rejects_old_results_and_preserves_explicit_legacy_replay(self):
        self.run_case(cached=True, cache_profile="standard")
        self.run_case(cached=True, cache_profile="standard", request_profile="standard")

    def test_denied_single_and_comparison_launch_no_provider_process(self):
        self.run_case(cached=False)
        self.run_case(cached=False, compare=True)

    def test_child_async_tasks_inherit_the_host_policy(self):
        seen = []
        token = FRESH_INSPECTION_ADMISSION.set(seen.append)
        async def child():
            FRESH_INSPECTION_ADMISSION.get()('2.50')
        async def parent():
            await asyncio.create_task(child())
        try:
            asyncio.run(parent())
            self.assertEqual(seen, ['2.50'])
        finally:
            FRESH_INSPECTION_ADMISSION.reset(token)

    def test_async_policy_misconfiguration_fails_closed(self):
        async def invalid_policy(maximum):
            pass
        token = FRESH_INSPECTION_ADMISSION.set(invalid_policy)
        try:
            with self.assertRaisesRegex(TypeError, 'synchronously'):
                admit_fresh_inspection('2.50')
        finally:
            FRESH_INSPECTION_ADMISSION.reset(token)


if __name__ == '__main__':
    unittest.main()
