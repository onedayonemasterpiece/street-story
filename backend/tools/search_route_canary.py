"""One real product search call; report model/grounding, never credentials or publication."""
from __future__ import annotations
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from devcoveer_story_diag import load_installer
from street_story.config import Settings
from street_story.providers import GeminiClient


async def main():
    installer = load_installer()
    for path in (installer.PROVIDERS_ENV, installer.SERVICE_ENV):
        installer.require_mode(path, 0o600)
        os.environ.update(installer.parse_dotenv(path))
    output = Path('/home/dev/artifacts/street-story/20261002T091632Z-owner-corpus-20261002')
    if not (output / '.artifact.json').is_file():
        raise RuntimeError('Retained private evidence directory is required')
    settings = replace(Settings.from_env(), data_dir=output / 'search-canary-data')
    client = GeminiClient(settings)
    original = client._generate
    attempts = []
    async def observed(key, timeout, contents, config=None, **kwargs):
        item = {'model': kwargs.get('model', settings.gemini_model)}
        attempts.append(item)
        try:
            response = await original(key, timeout, contents, config, **kwargs)
            item['grounding_chunks'] = sum(len(getattr(getattr(candidate, 'grounding_metadata', None), 'grounding_chunks', None) or []) for candidate in response.candidates or [])
            item['status'] = 'ok'
            return response
        except Exception as exc:
            item['status'] = 'error'
            item['error_type'] = type(exc).__name__
            item['error'] = settings.redact(str(exc))[:300]
            raise
    client._generate = observed
    try:
        result = await asyncio.wait_for(client.search_web(
            'Музей Мурариум водонапорная башня Зеленоградск официальный сайт история здания',
            {'purpose': 'one authorized search-route canary; no publication'}), timeout=90)
        receipt = {'attempts': attempts, 'grounding_sources': result.grounding_sources,
                   'facts': result.payload.get('facts', []), 'success': True}
    except Exception as exc:
        receipt = {'attempts': attempts, 'success': False, 'error_type': type(exc).__name__}
    path = output / 'search-route-canary.json'
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
