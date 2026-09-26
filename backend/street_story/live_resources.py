"""Shared resource boundary. No ordinary DB or direct Live-key fallback."""
from __future__ import annotations
import os
import re

_FIELDS=('GOOGLE_AI_LIMITER_SUPABASE_URL','GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY','AI_RESOURCE_LEDGER_ID')

def resource_environment(settings, environment=None):
    env=os.environ if environment is None else environment
    names=env.get('AI_RESOURCE_KEY_ENVS') or env.get('GOOGLE_AI_NORMAL_KEY_ENVS') or ','.join(settings.gemini_key_refs)
    refs=tuple(dict.fromkeys(name.strip() for name in names.split(',') if name.strip()))
    if any(not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}',name) for name in refs):
        raise ValueError('RESOURCE_CONFIGURATION_INVALID')
    values={name:env[name] for name in _FIELDS if env.get(name)}
    values['AI_RESOURCE_KEY_ENVS']=','.join(refs)
    for name in refs:
        if env.get(name):values[name]=env[name]
    return values

def managed_provider(settings):
    async def run(*,reader,on_event,load_key=None):
        # load_key is an old host-contract parameter, intentionally NEVER called.
        try:
            from ai_resource_control import run_guarded
        except ImportError:
            on_event({'type':'error','code':'RESOURCE_PACKAGE_MISSING','message':'RESOURCE_PACKAGE_MISSING'})
            return
        try:
            environment=resource_environment(settings)
        except ValueError:
            on_event({'type':'error','code':'RESOURCE_CONFIGURATION_INVALID','message':'RESOURCE_CONFIGURATION_INVALID'})
            return
        try:
            await run_guarded(consumer='street-story',environment=environment,reader=reader,
                              on_event=on_event,binding='street-story-service')
        finally:
            environment.clear()
    return run
