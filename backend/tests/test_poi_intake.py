"""Universal fixtures, not hard-coded answers for the owner's seven locations."""
import copy
import json

import httpx
import pytest

from street_story.db import Store
from street_story.poi_intake import execute, readback, ASPECTS, osm_candidate
from street_story.poi_memory import hydrate_story_facts

SOURCE = '''<html><title>House history</title><body>
The house at Test Street 7 was built in 1905.
The house at Test Street 7 appears in the film Example (1970).
Location research credited to Снято в Калининграде.
<img src="https://source.example/frame.jpg" alt="Frame from Example (1970)">
</body></html>'''
OSM = {'elements': [
    {'type': 'node', 'id': 1, 'lat': 54.0, 'lon': 20.0},
    {'type': 'node', 'id': 2, 'lat': 54.001, 'lon': 20.0},
    {'type': 'node', 'id': 3, 'lat': 54.001, 'lon': 20.001},
    {'type': 'node', 'id': 4, 'lat': 54.0, 'lon': 20.001},
    {'type': 'way', 'id': 77, 'version': 3, 'nodes': [1, 2, 3, 4, 1],
     'tags': {'building': 'yes', 'addr:street': 'Test Street', 'addr:housenumber': '7'}}]}


async def resolver(_host):
    return '93.184.216.34'


@pytest.fixture(scope='session', autouse=True)
def managed_test_artifact(tmp_path_factory):
    base = tmp_path_factory.getbasetemp()
    if str(base).startswith('/home/dev/artifacts/'):
        (base / '.artifact.json').write_text(json.dumps({'project': 'street-story',
            'purpose': 'poi-intake isolated test databases', 'retention_days': 7,
            'production_data': False}), encoding='utf-8')


def independent():
    return {'origin': 'independent', 'reason': 'Independent article evidence.'}


def credit(ref):
    return {'origin': 'partner_credited', 'partner_id': 'snyato-v-kaliningrade',
            'credit_read_ref': ref, 'credit_quote': 'Location research credited to Снято в Калининграде.',
            'reason': 'Explicit credit in the read article, not merely a hashtag.'}


def envelope(op, key, **kwargs):
    return {'op': op, 'campaign': 'cinema-test', 'pass_id': 'pass-1', 'request_key': key, **kwargs}


async def prepare(tmp_path, source=SOURCE):
    store = Store(tmp_path / 'canonical.sqlite3')
    def transport(request):
        if request.headers.get('host') == 'api.openstreetmap.org':
            return httpx.Response(200, json=OSM)
        return httpx.Response(200, text=source, headers={'content-type': 'text/html'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        read = await execute(store, envelope('read', 'read-1', url='https://source.example/article'), client=client, resolver=resolver)
        osm = await execute(store, envelope('osm', 'osm-1', way_id=77), client=client, resolver=resolver)
    lookup = await execute(store, envelope('lookup', 'lookup-1', osm_packet=osm['packet_id'], query='Test Street 7'))
    return store, read, osm, lookup


def assessment(text, key, quote, chunk):
    return {'checks': {'source_physical_subject': 'House at Test Street 7',
        'object_relation': 'selected_building',
        'relation_basis': {'fragment': 'The house at Test Street 7', 'chunk_ids': [chunk]},
        'material_kind': 'history', 'temporal_basis': 'historical', 'temporal_preserved': True},
        'verdict': 'supported', 'text': text, 'claim_key': key, 'subject_scope': 'selected_building',
        'confirmed_fragment': quote, 'source_date': None, 'chunk_ids': [chunk],
        'reason': 'The literal passage describes the selected physical building and this event.',
        'existing_fact_id': '', 'conflicts_with_existing': []}


def fact(read, cinema=False):
    text = 'Дом появился в фильме «Example».' if cinema else 'Дом построен в 1905 году.'
    key = 'film-example' if cinema else 'construction'
    quote = ('The house at Test Street 7 appears in the film Example (1970).' if cinema else
             'The house at Test Street 7 was built in 1905.')
    result = {'confidence': .9, 'text': text, 'claim_key': key, 'subject_scope': 'selected_building',
        'source_date': None, 'sources': [{'read_refs': [read['packet_id']],
            'source_version_id': read['source_version_id'],
            'assessment': assessment(text, key, quote, read['chunks'][0]['chunk_id']),
            'attribution': credit(read['packet_id']) if cinema else independent()}]}
    if cinema:
        result['cinema'] = {'film_key': 'fixture:example-1970', 'title': 'Example', 'release_year': 1970,
            'metadata_read_ref': read['packet_id'], 'metadata_quote': quote,
            'metadata_attribution': independent(), 'scene_key': 'exterior', 'relation': 'exterior',
            'description': 'The building exterior appears in the scene.',
            'frames': [{'url': 'https://source.example/frame.jpg', 'source_read_ref': read['packet_id'],
                       'caption': 'Frame from Example', 'attribution': credit(read['packet_id'])}]}
    return result


def command(read, osm, lookup):
    coverage = {k: {'status': 'pending', 'note': 'Still to be researched.'} for k in ASPECTS}
    coverage['construction'] = {'status': 'found', 'note': 'Source read and assessed.'}
    coverage['cinema'] = {'status': 'found', 'note': 'Film and scene identified.'}
    return envelope('commit', 'commit-1', osm_packet=osm['packet_id'], lookup_packet=lookup['packet_id'],
        poi_id=None, canonical_name='House at Test Street 7', identity_reason='The address and OSM building outline identify one physical house.',
        reviewed_candidate_ids=[], facts=[fact(read), fact(read, cinema=True)],
        completion={'coverage': coverage, 'remaining_gaps': ['Other building history.'],
                    'research_sufficient': False, 'next_action': 'Research original owners and changes of use.'})


@pytest.mark.asyncio
async def test_real_domain_intake_hydrates_new_story_without_job(tmp_path):
    store, read, osm, lookup = await prepare(tmp_path)
    body = command(read, osm, lookup)
    result = await execute(store, body)
    assert len(result['fact_ids']) == 2
    replay = await execute(store, body)
    assert replay['packet_id'] == result['packet_id'] and replay['replayed']
    assert readback(store, poi_id=result['poi_id'])['cinema']['partner_contributions'][0]['origin'] == 'partner_credited'
    with store.tx() as db:
        assert db.execute('SELECT count(*) FROM stories').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
        identity = {'candidate_id': 'osm:way:77', 'candidate_name': 'House at Test Street 7'}
        db.execute('INSERT INTO stories(id,client_story_id,photo_sha256,photo_mime_type,photo_path,voice_protocol,state,research_json,created_at,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)', ('story', 'story', 'fixture', 'image/jpeg', 'fixture.jpg', 'v1', 'ready',
            json.dumps({'visual_identity': identity}), 1, 1))
        assert hydrate_story_facts(db, identity, 'story') == 2
        assert hydrate_story_facts(db, identity, 'story') == 0
        rows = list(db.execute('SELECT * FROM fact_assertions WHERE story_id=\'story\''))
        assert {r['eligibility'] for r in rows} == {'eligible'}
        assert db.execute('SELECT count(*) FROM facts WHERE selected=1').fetchone()[0] == 0
        db.execute('UPDATE facts SET selected=1 WHERE fact_id=?', (result['fact_ids'][1],))
    context = readback(store, story_id='story')
    assert context['selected_fact_ids'] == [result['fact_ids'][1]]
    assert context['links'][0]['title'] == 'Example'
    assert context['links'][0]['frames'][0]['publication_status'] == 'not_cleared'
    assert context['partner_contributions'][0]['partner_id'] == 'snyato-v-kaliningrade'
    assert result['research_jobs_created'] == result['publications_created'] == 0
    from street_story.live import StreetStoryLiveAdapter
    from street_story.poi_intake import cinema_editor_context
    inventory = readback(store, poi_id=result['poi_id'])['facts']
    for item in inventory:
        item['selected'] = item['fact_id'] == result['fact_ids'][1]
    compact = StreetStoryLiveAdapter._compact_context({'story': {'facts': inventory}, 'editor': {}})
    assert compact['cinema_provenance']['films'][0]['title'] == 'Example'
    assert compact['cinema_provenance']['selected_partner_contributions'][0]['partner_id'] == 'snyato-v-kaliningrade'
    for item in inventory:
        item['selected'] = False
    assert cinema_editor_context(inventory)['selected_partner_contributions'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['quote', 'foreign_pass', 'false_credit', 'frame', 'scope', 'conflict'])
async def test_invalid_evidence_rolls_back_identity_and_facts(tmp_path, case):
    store, read, osm, lookup = await prepare(tmp_path)
    body = command(read, osm, lookup)
    if case == 'quote':
        body['facts'][0]['sources'][0]['assessment']['confirmed_fragment'] = 'This never appeared in the source.'
    if case == 'foreign_pass':
        body['pass_id'] = 'another-pass'
    if case == 'false_credit':
        body['facts'][1]['sources'][0]['attribution']['credit_quote'] = 'Made-up partner credit.'
    if case == 'frame':
        body['facts'][1]['cinema']['frames'][0]['url'] = 'https://source.example/not-read.jpg'
    if case == 'scope':
        body['facts'][0]['sources'][0]['assessment']['checks']['object_relation'] = 'unrelated'
    if case == 'conflict':
        body['facts'][0]['sources'][0]['assessment']['conflicts_with_existing'] = ['another-fact']
    with pytest.raises(ValueError):
        await execute(store, body)
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM pois').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM poi_research_assertions').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_snippet_is_not_complete_source_and_continuation_is_owned(tmp_path):
    store, read, osm, lookup = await prepare(tmp_path, SOURCE + (' Long supporting context.' * 500))
    body = command(read, osm, lookup)
    with pytest.raises(ValueError, match='full_frozen_source_not_read'):
        await execute(store, body)
    refs = [read['packet_id']]
    ordinal = read['next_ordinal']
    while ordinal is not None:
        part = await execute(store, envelope('read', f'read-{ordinal+1}', source_packet=read['packet_id'], ordinal=ordinal))
        refs.append(part['packet_id'])
        ordinal = part['next_ordinal']
    for f in body['facts']:
        f['sources'][0]['read_refs'] = refs
    assert len((await execute(store, body))['fact_ids']) == 2


@pytest.mark.asyncio
async def test_lookup_drift_and_changed_idempotency_fail_closed(tmp_path):
    store, read, osm, lookup = await prepare(tmp_path)
    body = command(read, osm, lookup)
    result = await execute(store, body)
    changed = copy.deepcopy(body)
    changed['canonical_name'] = 'Changed'
    with pytest.raises(ValueError, match='idempotency_conflict'):
        await execute(store, changed)
    changed['request_key'] = 'commit-2'
    with pytest.raises(ValueError, match='registry_changed'):
        await execute(store, changed)
    assert len(readback(store, poi_id=result['poi_id'])['facts']) == 2


def test_osm_missing_address_does_not_borrow_neighbour():
    data = copy.deepcopy(OSM)
    del data['elements'][-1]['tags']['addr:housenumber']
    data['elements'].append({'type': 'node', 'id': 999, 'lat': 54.0, 'lon': 20.0,
                            'tags': {'addr:street': 'Neighbour Street', 'addr:housenumber': '88', 'entrance': 'yes'}})
    with pytest.raises(ValueError, match='address_missing_or_ambiguous'):
        osm_candidate(json.dumps(data), 77)


def test_open_existing_never_creates_replacement_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        Store.open_existing(tmp_path / 'missing.sqlite3')
    assert not (tmp_path / 'missing.sqlite3').exists()


@pytest.mark.asyncio
async def test_checkpoint_replay_and_cursor_are_durable(tmp_path):
    store, _, _, _ = await prepare(tmp_path)
    body = envelope('checkpoint', 'cursor-1', last_source='https://source.example/article',
                    next_sources=['https://source.example/next'], unresolved=['Check another object'], note='Pass ended.')
    saved = await execute(store, body)
    reopened = Store.open_existing(store.path)
    assert (await execute(reopened, body))['packet_id'] == saved['packet_id']
    first = readback(reopened, campaign='cinema-test', limit=2)
    second = readback(reopened, campaign='cinema-test', after=first['next_cursor'], limit=2)
    assert second['items'][-1]['result']['checkpoint']['next_sources'] == ['https://source.example/next']


@pytest.mark.asyncio
async def test_private_network_source_is_rejected(tmp_path):
    store = Store(tmp_path / 'canonical.sqlite3')
    with pytest.raises(ValueError, match='unsafe_url'):
        await execute(store, envelope('read', 'private', url='https://127.0.0.1/secret'))
