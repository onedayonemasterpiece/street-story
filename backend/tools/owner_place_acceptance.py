"""Real REST manual photo -> explicit place -> admitted fact acceptance.

One case per invocation keeps provider work sequential. The bounded observation
never sets a product deadline or cancels the research state. A resumed invocation
reuses intake/selection request keys and the same durable database and receipts.
Registry source URLs, historical facts and model experiment answers are never
passed to the backend; only the explicitly chosen object ID/name/address are.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import fields
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from devcoveer_story_diag import load_installer
from identity_corpus import NoPublication
from product_recovery_acceptance import (
    digest, instrument_control_boundary, managed, read_case,
    require_frozen_checkout, runtime_qualification_caches, save, summarize_receipts,
)

BACKEND = Path(__file__).resolve().parents[1]


def load_research_qualification(installer, *, research_directory=None, qualification_path=None):
    """Apply local overrides after environment loading; never relabel old proof."""
    if research_directory is not None:
        directory = managed(research_directory)
        os.environ['STREET_STORY_RESEARCH_DIRECTORY'] = str(directory)
    else:
        directory = Path(os.environ.get('STREET_STORY_RESEARCH_DIRECTORY')
                         or installer.RESEARCH_DIRECTORY).resolve()
    if not directory.is_dir():
        raise ValueError('Research capsule directory unavailable')
    # The installer validator binds qualified routes to this exact directory.
    installer.RESEARCH_DIRECTORY = directory
    original_path = qualification_path or installer.RESEARCH_QUALIFICATION
    if original_path.is_symlink():
        raise ValueError('Provider qualification symlink is not accepted')
    path = managed(original_path) if qualification_path is not None else original_path.resolve()
    installer.require_mode(path, 0o600)
    qualification = json.loads(path.read_text())
    for evidence in qualification['evidence']:
        evidence_path = Path(evidence['path'])
        if evidence_path.is_symlink() or hashlib.sha256(evidence_path.read_bytes()).hexdigest() != evidence['sha256']:
            raise ValueError('Provider qualification provenance mismatch')
    caches = runtime_qualification_caches(qualification, installer)
    profile = directory/'opencode.json'
    if profile.is_symlink() or not profile.is_file():
        raise ValueError('Research capsule profile unavailable')
    profile_sha = hashlib.sha256(profile.read_bytes()).hexdigest()
    if research_directory is not None:
        text = caches.get('research-text-verification-v1') or {}
        scoped = [route for route in text.get('extractors', [])
                  if route.get('provider_id') == 'opencode']
        if not scoped:
            raise ValueError('Overridden research capsule requires scoped text qualification')
        proofs = {item['path']: item['sha256'] for item in qualification['evidence']}
        for route in scoped:
            flags = ('semantic_contract_verified', 'source_subject_negative_verified',
                     'planned_modality_verified', 'known_claim_reuse_verified')
            if (route.get('directory') != str(directory)
                    or not all(route.get(flag) is True for flag in flags)
                    or route.get('profile_sha256') != profile_sha
                    or not route.get('qualification_sha256')
                    or proofs.get(route.get('qualification_receipt')) != route['qualification_sha256']):
                raise ValueError('Scoped text qualification provenance mismatch')
            report = json.loads(Path(route['qualification_receipt']).read_text())
            measured = (report.get('model_routes') or {}).get(route['model_id']) or {}
            controls = measured.get('controls') or {}
            if (report.get('directory') != str(directory)
                    or measured.get('qualified') is not True
                    or measured.get('provider_id') != route['provider_id']
                    or measured.get('model_id') != route['model_id']
                    or set(controls) != {'negative_foreign_subject', 'positive_planned_modality_known_reuse'}):
                raise ValueError('Scoped text qualification controls incomplete')
            for control in controls.values():
                receipt = control.get('receipt') or {}
                if (control.get('passed') is not True or control.get('phase') != 'completed'
                        or not control.get('checks') or not all(value is True for value in control['checks'].values())
                        or receipt.get('phase') != 'completed'
                        or receipt.get('provider_id') != route['provider_id']
                        or receipt.get('model_id') != route['model_id']
                        or (receipt.get('isolation') or {}).get('directory') != str(directory)):
                    raise ValueError('Provider qualification receipt belongs to a different capsule or failed control')
        # An optional background review pool must have independent actual proof.
        for route in (caches.get('fact-semantic-verification-v1') or {}).get('routes', []):
            if route.get('provider_id') == 'opencode':
                report = json.loads(Path(route['qualification_receipt']).read_text())
                if ((report.get('receipt') or {}).get('isolation') or {}).get('directory') != str(directory):
                    raise ValueError('Semantic qualification receipt belongs to a different capsule')
    provenance = {'research_directory': str(directory), 'qualification_path': str(path),
        'research_profile_path': str(profile), 'research_profile_sha256': profile_sha,
        'qualification_file_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'directory_override': research_directory is not None,
        'qualification_override': qualification_path is not None}
    return qualification, caches, provenance


def load_agent_qualification(path, *, routes=None):
    """New agent-role proof only: immutable actual tool receipts plus current profile."""
    from street_story.fact_agents import QUALIFICATION_KEY
    from street_story.fact_agent_providers import INSTRUCTIONS, RESULT_SCHEMA, TOOLS, validate_result
    from jsonschema import Draft202012Validator
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Agent qualification symlink is not accepted')
    path = managed(path)
    proof = json.loads(path.read_text())
    if proof.get('contract') != QUALIFICATION_KEY or not isinstance(proof.get('routes'), list) or not proof['routes']:
        raise ValueError('New agent-role qualification required')
    current = {route.route_id: route for route in routes} if routes is not None else None
    seen, providers = set(), set()
    for route in proof['routes']:
        if (not isinstance(route, dict) or route.get('contract') != QUALIFICATION_KEY
                or not route.get('route_id') or not route.get('profile_sha256')
                or route.get('tool_access_verified') is not True or route.get('tool_safety_verified') is not True
                or not isinstance(route.get('qualification_receipts'), list) or not route['qualification_receipts']
                or route['route_id'] in seen):
            raise ValueError('Agent role/tool qualification incomplete')
        seen.add(route['route_id'])
        providers.add(route.get('provider_id'))
        if current is not None:
            configured = current.get(route['route_id'])
            if (configured is None or configured.profile_fingerprint != route['profile_sha256']
                    or configured.provider_id != route.get('provider_id') or configured.model_id != route.get('model_id')
                    or configured.endpoint.rstrip('/') != str(route.get('endpoint') or '').rstrip('/')
                    or configured.directory != route.get('directory')):
                raise ValueError('Agent qualification route/profile changed')
        for evidence in route['qualification_receipts']:
            evidence_path = Path(evidence['path'])
            if evidence_path.is_symlink():
                raise ValueError('Agent receipt symlink is not accepted')
            evidence_path = managed(evidence_path)
            raw = evidence_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != evidence.get('sha256'):
                raise ValueError('Agent qualification receipt integrity mismatch')
            report = json.loads(raw)
            receipt = report.get('receipt', report)
            binding = receipt.get('binding') or {}
            tools = receipt.get('tool_receipts') or []
            if (receipt.get('phase') != 'completed' or receipt.get('provider_id') != route.get('provider_id')
                    or receipt.get('model_id') != route.get('model_id') or not binding.get('attempt_id')
                    or not binding.get('story_id') or binding.get('purpose') != 'facts'
                    or receipt.get('provider_send_state') != 'response_closed'
                    or receipt.get('frozen_schema') != RESULT_SCHEMA
                    or not str(receipt.get('frozen_prompt') or '').startswith(INSTRUCTIONS + '\nINPUT=')
                    or not receipt.get('model_attempts') or not tools
                    or {item.get('name') for item in tools} != {'websearch', 'webfetch'}):
                raise ValueError('Actual agent search/read receipt required; legacy reader flags cannot qualify')
            for tool in tools:
                declaration = next((item for item in TOOLS if item['name'] == tool.get('name')), None)
                if (not declaration or not Draft202012Validator(declaration['parameters']).is_valid(tool.get('arguments'))
                        or not tool.get('completed_at') or not tool.get('result_sha256')):
                    raise ValueError('Agent observed tool receipt outside current read-only scope')
            facts = validate_result(receipt.get('result'))['facts']
            read_urls = {source.get(key) for source in receipt.get('sources') or []
                         if source.get('source_version_id') and source.get('content_sha256')
                         for key in ('requested_url', 'final_url')}
            if not facts or any(fact['url'] not in read_urls for fact in facts):
                raise ValueError('Agent qualification has no actually read candidate source')
    if len(providers - {None}) < 2:
        raise ValueError('Agent qualification needs two independent providers')
    return proof, {'agent_qualification_path': str(path),
        'agent_qualification_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def registry_item(path, message):
    registry = json.loads(managed(path).read_text())
    matches = [row for row in registry['items'] if any(
        source.get('telegram_message_id') == message for source in row['sources'])]
    if len(matches) != 1:
        raise ValueError('Registry original must be unambiguous')
    row = matches[0]
    source = next((entry for entry in row['storage'] if entry.get('canonical')
                   and entry.get('availability') == 'verified_bytes'), None)
    if not source:
        raise ValueError('Registry original bytes unavailable')
    original = managed(Path(source['path']))
    data = original.read_bytes()
    if hashlib.sha256(data).hexdigest() != row['file']['sha256']:
        raise ValueError('Registry original SHA mismatch')
    expected = row['expected']
    physical_ids = [value for value in expected.get('physical_ids', []) if value.startswith('osm:')]
    reference = expected.get('research_osm_ref')
    if reference:
        physical_ids.append('osm:' + reference.replace('/', ':'))
    label = expected.get('object_or_address')
    if not label or not physical_ids:
        raise ValueError('Explicit author object ID and name/address required')
    item = {'message_id': message, 'path': str(original), 'sha256': row['file']['sha256'],
        'mime': row['file']['mime'], 'min_useful_facts': 3,
        'expected_physical_ids': list(dict.fromkeys(physical_ids)), 'report_label': label,
        'camera_gps': row['camera_gps']}
    return item, data


def author_context(item):
    """Only the chosen registry label; no historical article/observations."""
    label = item['report_label']
    city = next((name for name in ('Калининград', 'Светлогорск', 'Советск') if name in label), 'Калининград')
    address = label.removeprefix(city + ', ').split(';')[0].strip()
    return {'city': city, 'address': address, 'name': label.split(';')[-1].strip()}


async def fallback_object_point(service, item):
    """Retrieve the explicit object's observed center, never camera-as-building."""
    import httpx
    cid = item['expected_physical_ids'][0]
    _, kind, identifier = cid.split(':')
    async with httpx.AsyncClient(timeout=25, headers={'User-Agent': service.settings.osm_user_agent}) as client:
        raw, endpoint = await service.providers.osm._overpass_query(client,
            f'[out:json][timeout:20];{kind}({int(identifier)});out center;',
            'owner_selected_point', None, set())
    element = next((row for row in raw.get('elements', [])
                    if row.get('type') == kind and row.get('id') == int(identifier)), None)
    center = (element or {}).get('center') or {}
    if not center.get('lat') or not center.get('lon'):
        raise ValueError('No observed object point; camera coordinates cannot stand in for the selected house')
    return {'latitude': center['lat'], 'longitude': center['lon']}, {
        'candidate_id': cid, 'center': center, 'source': 'existing_osm_overpass_adapter', 'endpoint': endpoint}


async def stable_post(client, url, key, *, json_body=None, form=None, files=None):
    import httpx
    for retry in range(3):
        try:
            response = await client.post(url, headers={'Idempotency-Key': key},
                json=json_body, data=form, files=files)
            if response.is_error:
                failure = response.json().get('error', {})
                raise RuntimeError(f'REST {response.status_code} {failure.get("code", "endpoint_error")}')
            response.raise_for_status()
            return response.json()
        except httpx.TransportError:
            if retry == 2:
                raise
            await asyncio.sleep(1)  # Same key reconciles an unknown prior effect.


def enrich_evidence(service, case, visible):
    """Read own immutable evidence, while eligibility stays the product's choice."""
    facts = {fact['fact_id']: fact for fact in visible.get('facts', [])}
    usable = [fact for fact in case['eligible_facts'] if fact['assertion_id'] in facts
              and facts[fact['assertion_id']].get('eligibility') == 'eligible']
    with service.store.connection() as db:
        for fact in usable:
            fact['read_spans'] = [dict(row) for row in db.execute(
                'SELECT e.evidence_id,e.source_url,e.source_version_id,e.support_kind,e.span_text,'
                'e.span_sha256,e.chunk_id,e.span_start,e.span_end,e.relation FROM fact_evidence_spans e '
                'JOIN fact_observations o ON o.observation_id=e.observation_id '
                'WHERE o.story_id=? AND o.assertion_id=? AND e.relation=?',
                (case['story_id'], fact['assertion_id'], 'supports'))]
    case['rest_eligible_proved_facts'] = usable
    case['rest_eligible_proved_count'] = len(usable)
    case['own_read_source_count'] = len({span['source_url'] for fact in usable for span in fact['read_spans']})
    from street_story.agent_evidence import CONTRACT
    from street_story.fact_ledger import accepted_basis_valid
    with service.store.connection() as db:
        def model_confirmed(fact, span):
            if span['support_kind'] != 'model_confirmed_source_chunk':
                return False
            assertion = db.execute('SELECT * FROM fact_assertions WHERE story_id=? AND assertion_id=?',
                (case['story_id'], fact['assertion_id'])).fetchone()
            if not assertion or not accepted_basis_valid(db, case['story_id'], assertion):
                return False
            basis = json.loads(assertion['accepted_basis_json'] or '{}')
            operation = basis.get('operation') or {}
            metadata = facts[fact['assertion_id']].get('confirmation') or {}
            version = db.execute('SELECT * FROM source_versions WHERE source_version_id=?', (span['source_version_id'],)).fetchone()
            chunk = db.execute('SELECT * FROM source_chunks WHERE source_version_id=? AND chunk_id=?',
                (span['source_version_id'], span['chunk_id'])).fetchone()
            return bool(operation.get('contract') == CONTRACT and operation.get('confirmation_attempt_id')
                and operation.get('confirmation_model') and operation.get('confirmed_fragment') and operation.get('reason')
                and (operation.get('agent') or {}).get('route_id')
                and metadata.get('confirmation_attempt_id') == operation['confirmation_attempt_id']
                and metadata.get('confirmation_model') == operation['confirmation_model']
                and span['evidence_id'] in {ev['evidence_id'] for ev in basis.get('evidence', [])}
                and version and chunk and version['access_scope'] == 'public'
                and hashlib.sha256(version['normalized_text'].encode()).hexdigest() == version['content_sha256']
                and operation.get('source_content_sha256') == version['content_sha256'])
        case['own_read_supported_fact_count'] = sum(any(span['support_kind'] == 'verified_page_span' or model_confirmed(fact, span)
            for span in fact['read_spans']) for fact in usable)
    case['rest_projection'] = visible
    case['confirmed_identity_preserved'] = (
        visible.get('visual_identity', {}).get('status') == 'owner_confirmed'
        and visible.get('visual_identity', {}).get('candidate_id') == case.get('selected_candidate_id'))


async def run(args):
    sys.path.insert(0, str(BACKEND))
    if args.allow_wip:
        sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=BACKEND.parent, text=True).strip()
        if sha != args.expected_sha:
            raise ValueError('WIP baseline HEAD differs from expected SHA')
    else:
        sha = require_frozen_checkout(args.expected_sha)
    item, data = registry_item(args.registry, args.message)
    output = managed(args.output)
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    installer = load_installer()
    env_hashes = {}
    for config in (installer.PROVIDERS_ENV, installer.SERVICE_ENV):
        installer.require_mode(config, 0o600)
        env_hashes[config.name] = hashlib.sha256(config.read_bytes()).hexdigest()
        os.environ.update(installer.parse_dotenv(config))
    os.environ.update(DATA_DIR=str(output/'data'), STREET_STORY_DEPLOY_SHA=sha,
                      VIBEPUBLISH_BASE_URL='', VIBEPUBLISH_BEARER_TOKEN='')
    qualification, caches, research_provenance = load_research_qualification(installer,
        research_directory=args.research_directory, qualification_path=args.qualification)
    agent_qualification = None
    if args.agent_qualification is not None:
        agent_qualification, agent_provenance = load_agent_qualification(args.agent_qualification)
        research_provenance.update(agent_provenance)
    from street_story.config import Settings, reveal
    settings = Settings.from_env()
    from street_story.shared_devcoveer_research import SharedDevCoveerResearch
    models = {settings.research_model, *(
        route['model_id'] for route in (caches.get('fact-semantic-verification-v1') or {}).get('routes', [])
        if route.get('provider_id') == 'opencode')}
    research_provenance['runtime_profile_fingerprints'] = {model:
        SharedDevCoveerResearch(research_provenance['research_directory'], model_id=model).profile_fingerprint
        for model in sorted(models)}
    public = {field.name: str(getattr(settings, field.name))
              for field in fields(settings) if field.repr}
    frozen = {'source_sha': sha, 'registry_sha256': hashlib.sha256(args.registry.read_bytes()).hexdigest(),
        'original_sha256': item['sha256'], 'message_id': args.message,
        'environment_sha256': env_hashes, 'qualification_sha256': digest(qualification),
        'runtime_qualification_caches_sha256': digest(caches), 'public_settings': public,
        'research_provenance': research_provenance,
        'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'observation_seconds': args.observe_seconds, 'product_deadline': None,
        'provider_reference_or_expected_facts_supplied': False,
        'author_selection_input': author_context(item), 'physical_ids': item['expected_physical_ids']}
    frozen['revision14_voice_gps'] = args.revision14
    frozen['qualification_scope'] = 'prerelease_WIP' if args.allow_wip else 'frozen_candidate'
    frozen['source_hashes'] = {str(path.relative_to(BACKEND.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((BACKEND/'street_story').glob('*.py'))}
    run_path = output/'run.json'
    if run_path.exists() and json.loads(run_path.read_text()) != frozen:
        raise ValueError('Frozen acceptance source/config changed; retain prior case and choose another output')
    save(run_path, frozen)
    report_path = output/'case.json'
    case = json.loads(report_path.read_text()) if report_path.exists() else {
        'message_id': args.message, 'label': item['report_label'], 'status': 'NOT_RUN',
        'source_sha': sha, 'original_sha256': item['sha256'], 'min_useful_facts': 3}
    logging.basicConfig(filename=output/'workers.log', level=logging.INFO)
    from street_story.app import app
    service = app.state.service
    service.providers.vibepublish = NoPublication()
    for key, value in caches.items():
        service.store.cache_put(key, value, 3600)
    if agent_qualification is not None:
        from street_story.fact_agents import QUALIFICATION_KEY
        checked, _ = load_agent_qualification(args.agent_qualification,
            routes=service.providers.research.fact_agent_routes(include_unqualified=True))
        service.store.cache_put(QUALIFICATION_KEY, checked, 3600)
    restore_control = instrument_control_boundary()
    import httpx
    import uvicorn
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    base = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(app, lifespan='off', access_log=False, log_config=None))
    server_task = None
    try:
        async with app.router.lifespan_context(app):
            server_task = asyncio.create_task(server.serve(sockets=[sock]))
            while not server.started:
                if server_task.done():
                    await server_task
                    raise RuntimeError('REST server failed to start')
                await asyncio.sleep(.05)
            headers = {'Authorization': 'Bearer ' + reveal(settings.device_token), 'Connection': 'close'}
            async with httpx.AsyncClient(base_url=base, headers=headers, trust_env=False, timeout=45) as client:
                key = f'owner-place-acceptance:{digest(frozen)}:upload'
                if not case.get('story_id'):
                    form = {'client_story_id': key, 'photo_sha256': item['sha256'],
                            'voice_protocol': 'voice-chunks-v2', 'identity_mode': 'manual'}
                    gps = item['camera_gps']
                    if args.revision14 and gps.get('status') != 'verified_exif':
                        raise ValueError('Revision14 requires original verified EXIF GPS')
                    if gps.get('status') == 'verified_exif':
                        # Standard intake lat/lon fields represent photo GPS;
                        # camera_coordinate_source is reserved for owner hints.
                        form.update(lat=str(gps['latitude']), lon=str(gps['longitude']))
                    created = await stable_post(client, '/v1/stories', key, form=form,
                        files={'photo': (f'photo-{args.message}.jpg', data, item['mime'])})
                    case.update(story_id=created['id'], upload_response=created)
                    save(report_path, case)
                story_id = case['story_id']
                if not case.get('confirmation_completed'):
                    current = await client.get('/v1/stories/' + story_id)
                    current.raise_for_status()
                    current = current.json()
                    # Do not repeat a completed map read after a disconnect.
                    if 'map_response' not in case:
                        map_response = await client.get(f'/v1/stories/{story_id}/place-map')
                        case['map_http_status'] = map_response.status_code
                        case['map_response'] = map_response.json()
                        save(report_path, case)
                    snapshot = case['map_response']
                    features = snapshot.get('buildings', {}).get('features', [])
                    selected = next((feature['id'] for feature in features
                                     if feature['id'] in item['expected_physical_ids']), None)
                    if not case.get('confirmation_body'):
                        command = f'owner-place-acceptance:{digest(frozen)}:confirm'
                        body = {'mode': 'exact', 'command_id': command,
                            'expected_photo_sha256': item['sha256'],
                            'expected_identity_generation': current.get('identity_generation', 0),
                            'expected_candidate_id': (current.get('visual_identity') or {}).get('candidate_id')}
                        context = author_context(item)
                        if selected:
                            body.update(snapshot_id=snapshot['snapshot_id'], candidate_id=selected)
                            if not args.revision14:
                                body['owner_context'] = context
                            case['selection_basis'] = 'shown_osm_contour'
                        elif args.revision14:
                            raise ValueError('Revision14 requires a shown contour; no manual point or author text fallback')
                        else:
                            coordinates, provenance = await fallback_object_point(service, item)
                            body['fallback'] = {**context, **coordinates}
                            case['fallback_coordinate_provenance'] = provenance
                            case['selection_basis'] = 'owner_explicit_address_point_at_observed_object_center'
                        case.update(confirmation_body=body, confirmed_at_utc=time.time())
                        save(report_path, case)
                    confirmed = await stable_post(client, f'/v1/stories/{story_id}/place',
                        case['confirmation_body']['command_id'], json_body=case['confirmation_body'])
                    case.update(confirmation_completed=True, confirmation_response=confirmed,
                        selected_candidate_id=(confirmed.get('visual_identity') or {}).get('candidate_id'))
                    # Replay identical command: current result, no new generation/job.
                    replay = await stable_post(client, f'/v1/stories/{story_id}/place',
                        case['confirmation_body']['command_id'], json_body=case['confirmation_body'])
                    case['idempotent_replay_same_generation'] = replay.get('identity_generation') == confirmed.get('identity_generation')
                    save(report_path, case)
            # Close intake client. Research proceeds without connected clients.
            started = time.monotonic()
            case['client_closed_at_utc'] = time.time()
            while True:
                # A newly created TCP client observes persisted product state.
                async with httpx.AsyncClient(base_url=base, headers=headers, trust_env=False, timeout=45) as client:
                    response = await client.get('/v1/stories/' + story_id)
                    response.raise_for_status()
                    visible = response.json()
                read_case(service, case, item)
                enrich_evidence(service, case, visible)
                elapsed = time.time() - case['confirmed_at_utc']
                count = case['rest_eligible_proved_count']
                if count and 'first_eligible_from_confirmation_s' not in case:
                    case['first_eligible_from_confirmation_s'] = round(elapsed, 3)
                    save(output/'first-rest-facts.json', visible)
                if count >= 3 and 'three_eligible_from_confirmation_s' not in case:
                    case['three_eligible_from_confirmation_s'] = round(elapsed, 3)
                case['observed_from_confirmation_s'] = round(elapsed, 3)
                case['threshold_pass'] = (count >= 3 and case.get('three_eligible_from_confirmation_s', float('inf')) <= 300
                    and case['own_read_supported_fact_count'] >= 3 and case['confirmed_identity_preserved'])
                case['status'] = 'PASS' if case['threshold_pass'] else 'OBSERVING'
                save(report_path, case)
                if count >= 3 or time.monotonic() - started >= args.observe_seconds:
                    if not case['threshold_pass']:
                        case['status'] = 'THRESHOLD_NOT_REACHED'
                    case['observation_ended_at_utc'] = time.time()
                    case['backend_shutdown_is_experiment_end'] = True
                    save(report_path, case)
                    print(json.dumps({key: case.get(key) for key in ('message_id', 'status', 'story_id',
                        'selection_basis', 'selected_candidate_id', 'first_eligible_from_confirmation_s',
                        'rest_eligible_proved_count', 'own_read_source_count', 'three_eligible_from_confirmation_s',
                        'confirmed_identity_preserved', 'product_outcome', 'error_code')}, ensure_ascii=False), flush=True)
                    break
                await asyncio.sleep(2)
            server.should_exit = True
            await server_task
    finally:
        restore_control()
        if server_task and not server_task.done():
            server.should_exit = True
            await server_task
        sock.close()
        if case.get('story_id'):
            # Preserve the observation result separately from worker shutdown
            # effects and any provider responses whose delivery remains unknown.
            with service.store.connection() as db:
                case['post_shutdown_jobs'] = [dict(row) for row in db.execute(
                    'SELECT id,kind,state,attempts,last_error FROM jobs WHERE story_id=?', (case['story_id'],))]
                attempts = [dict(row) for row in db.execute(
                    'SELECT * FROM research_provider_attempts WHERE story_id=? ORDER BY created_at', (case['story_id'],))]
            case['post_shutdown_provider_receipts'] = summarize_receipts(attempts)
            save(report_path, case)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--message', type=int, required=True)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--allow-wip', action='store_true', help='Prerelease canary only; retain full source hashes')
    parser.add_argument('--research-directory', type=Path,
        help='Managed task capsule override, applied after the dev environment')
    parser.add_argument('--qualification', type=Path,
        help='Managed task qualification override; immutable evidence is verified')
    parser.add_argument('--revision14', action='store_true', help='Verified EXIF GPS and one shown contour; no author text/point fallback')
    parser.add_argument('--agent-qualification', type=Path, help='Managed actual research-agent-verification-v1 tool proof JSON')
    parser.add_argument('--observe-seconds', type=float, default=300)
    args = parser.parse_args()
    if args.revision14 and args.agent_qualification is None:
        parser.error('Revision14 requires explicit actual --agent-qualification')
    if not 0 < args.observe_seconds <= 300:
        parser.error('Observation must be between 0 and 300 seconds; this is not a product deadline')
    asyncio.run(run(args))
