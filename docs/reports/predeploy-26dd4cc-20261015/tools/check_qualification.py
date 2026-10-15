"""Read-only qualification compatibility check against a source tree.

Mirrors devcoveer_install.install_fact_agent_runtime, but uses a throwaway
data_dir and never writes the production qualification cache.
"""
import asyncio, json, os, sys, tempfile
from dataclasses import replace
from pathlib import Path
from devcoveer_story_diag import load_installer
from owner_place_acceptance import load_agent_qualification
from street_story.config import Settings
from street_story.runtime import RuntimeStreetStoryService

proof_path = Path(sys.argv[1]); installer = load_installer()
for path in (Path(sys.argv[2]), Path(sys.argv[3])):
    if path.exists():
        installer.require_mode(path, 0o600); os.environ.update(installer.parse_dotenv(path))
proof = json.loads(proof_path.read_text())
config = [{k: r[k] for k in ('provider_id', 'model_id', 'endpoint', 'directory') if r.get(k) is not None}
          for r in proof['routes']]
with tempfile.TemporaryDirectory(prefix='qualcheck-', dir=sys.argv[4]) as data:
    settings = replace(Settings.from_env(), data_dir=Path(data), fact_agent_routes=tuple(config),
                       vibepublish_base_url='', vibepublish_bearer_token='')
    service = RuntimeStreetStoryService(settings)
    routes = service.providers.research.fact_agent_routes(include_unqualified=True)
    current = {r.route_id: r.profile_fingerprint for r in routes}
    out = {'proof_profiles': {r['route_id']: r['profile_sha256'] for r in proof['routes']},
           'current_profiles': current}
    try:
        _, provenance = load_agent_qualification(proof_path, routes=routes)
        out.update(compatible=True, qualification_sha256=provenance['agent_qualification_sha256'])
    except ValueError as exc:
        out.update(compatible=False, error=str(exc))
    async def close():
        await service.providers.research.close(); await service.providers.gemini.close()
    asyncio.run(close())
print(json.dumps(out, sort_keys=True))
