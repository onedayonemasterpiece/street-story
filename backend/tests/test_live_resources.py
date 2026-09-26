import asyncio
import sys
import types
import unittest
from unittest.mock import patch
from street_story.live_resources import managed_provider,resource_environment

class Settings:
    gemini_key_refs=('GOOGLE_API_KEY','GOOGLE_API_KEY2')

class LiveResources(unittest.IsolatedAsyncioTestCase):
    def test_only_explicit_quota_configuration_and_registered_aliases(self):
        env={'SUPABASE_URL':'wrong','SUPABASE_KEY':'wrong','LIVE_API_KEY':'wrong','GOOGLE_API_KEY':'fixture1','GOOGLE_API_KEY2':'fixture2','UNRELATED_TOKEN':'hidden'}
        result=resource_environment(Settings(),env)
        self.assertNotIn('GOOGLE_AI_LIMITER_SUPABASE_URL',result)
        self.assertNotIn('SUPABASE_KEY',result);self.assertNotIn('LIVE_API_KEY',result)
        self.assertNotIn('UNRELATED_TOKEN',result)
        self.assertEqual(result['AI_RESOURCE_KEY_ENVS'],'GOOGLE_API_KEY,GOOGLE_API_KEY2')
    def test_invalid_key_alias(self):
        with self.assertRaises(ValueError):resource_environment(Settings(),{'AI_RESOURCE_KEY_ENVS':'../private'})
    async def test_host_direct_key_callback_never_called(self):
        calls=[]
        async def guard(**kwargs):calls.append({**kwargs,'environment':dict(kwargs['environment'])})
        def forbidden():raise AssertionError('uncontrolled key resolver invoked')
        with patch.dict(sys.modules,{'ai_resource_control':types.SimpleNamespace(run_guarded=guard)}),patch.dict('os.environ',{'GOOGLE_API_KEY':'fixture1'},clear=True):
            await managed_provider(Settings())(reader=object(),on_event=lambda e:None,load_key=forbidden)
        self.assertEqual(calls[0]['consumer'],'street-story')
        self.assertEqual(calls[0]['environment']['GOOGLE_API_KEY'],'fixture1')
    async def test_missing_package_bounded_failure(self):
        events=[]
        with patch.dict(sys.modules,{'ai_resource_control':None}):
            await managed_provider(Settings())(reader=object(),on_event=events.append,load_key=lambda:'fixture')
        self.assertEqual(events[0]['code'],'RESOURCE_PACKAGE_MISSING')

if __name__=='__main__':unittest.main()
