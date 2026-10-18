"""Prior model excerpts locate own bytes without supplying semantic verdicts."""
import hashlib
import json

import pytest

from test_review_topic_binding import review as review
from street_story.research_budget import PAGE_UNITS, response_units
from street_story.review_packets import prior_excerpt_navigation


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [None, 'version', 'hash', 'truncated', 'absent', 'foreign_story'])
async def test_navigation_requires_unchanged_own_version_and_literal(review, invalid):
    svc, _, session, _, fact_id = review
    fragment = 'Exact own prior excerpt.'
    evidence = {'text': 'menu ' * 200 + fragment, 'source_version_id': 'own-version'}
    source = {'source_version_id': 'own-version', 'confirmed_fragment': fragment,
              'confirmed_fragment_original_sha256': hashlib.sha256(fragment.encode()).hexdigest(),
              'confirmed_fragment_truncated': False}
    if invalid == 'version':
        source['source_version_id'] = 'another-version'
    elif invalid == 'hash':
        source['confirmed_fragment_original_sha256'] = '0' * 64
    elif invalid == 'truncated':
        source['confirmed_fragment_truncated'] = True
    elif invalid == 'absent':
        evidence['text'] = 'menu without the excerpt'
    with svc.store.tx() as db:
        db.execute('UPDATE facts SET sources_json=? WHERE fact_id=?', (json.dumps([source]), fact_id))
        hint = prior_excerpt_navigation(db, 'another-story' if invalid == 'foreign_story' else
                                        session.resource_id, fact_id, evidence)
    if invalid:
        assert hint is None
    else:
        assert hint == {'offset': 1000, 'length': len(fragment), 'origin_fact_id': fact_id,
                        'fragment_sha256': source['confirmed_fragment_original_sha256'], 'navigation_only': True}


@pytest.mark.asyncio
async def test_repaired_child_reuses_only_own_parent_navigation(review):
    svc, _, session, _, fact_id = review
    fragment = 'Original retained excerpt.'
    source = {'source_version_id': 'own-version', 'confirmed_fragment': fragment,
              'confirmed_fragment_original_sha256': hashlib.sha256(fragment.encode()).hexdigest()}
    with svc.store.tx() as db:
        db.execute('UPDATE facts SET sources_json=? WHERE fact_id=?', (json.dumps([source]), fact_id))
        db.execute('INSERT INTO live_fact_repairs VALUES(?,?,?,?,?,?)',
                   (session.resource_id, fact_id, 'child', 'command', 'packet', 'Controlled ancestry.'))
        db.execute('INSERT INTO live_fact_repairs VALUES(?,?,?,?,?,?)',
                   (session.resource_id, 'child', 'grandchild', 'command', 'packet', 'Controlled ancestry.'))
        # UNION must also terminate on malformed/cyclic historical ancestry.
        db.execute('INSERT INTO live_fact_repairs VALUES(?,?,?,?,?,?)',
                   (session.resource_id, 'grandchild', fact_id, 'command', 'packet', 'Controlled cycle.'))
        evidence = {'source_version_id': 'own-version', 'text': fragment}
        assert prior_excerpt_navigation(db, session.resource_id, 'grandchild', evidence)['origin_fact_id'] == fact_id
        assert prior_excerpt_navigation(db, 'another-story', 'grandchild', evidence) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('fragment', ['Relevant retained caption with an ambiguous date.', 'caption ' * 200])
async def test_live_relevant_slice_first_preserves_all_bytes_and_api_order(review, fragment):
    svc, adapter, session, run_id, fact_id = review
    packet = await adapter.execute_tool(session, {'name': 'get_review_packet',
        'args': {'run_id': run_id, 'fact_ids': [fact_id]}})
    ref = packet['packet_ref']
    text = 'menu ' * 200 + fragment + ' remaining source ' * 150
    with svc.store.tx() as db:
        payload = json.loads(db.execute('SELECT payload_json FROM live_review_packets WHERE packet_ref=?',
                                       (ref,)).fetchone()[0])
        evidence = payload['items'][0]['evidence'][0]
        evidence.update(text=text, prior_excerpt_navigation={'offset': 1000, 'length': len(fragment),
                        'fragment_sha256': hashlib.sha256(fragment.encode()).hexdigest(),
                        'origin_fact_id': fact_id, 'navigation_only': True})
        db.execute('UPDATE live_review_packets SET payload_json=? WHERE packet_ref=?', (json.dumps(payload), ref))
    pages, args = [], {'packet_ref': ref}
    while True:
        page = await adapter.execute_tool(session, {'name': 'get_review_packet', 'args': args})
        assert response_units('get_review_packet', page) <= PAGE_UNITS
        pages.extend(page['items'])
        if not page['has_more']:
            break
        args = page['next_args']
    assert pages[0]['offset'] == 900
    relevant = [p for p in pages if p['offset'] < 1000 + len(fragment) and
                1000 < p['offset'] + len(p['passage'])]
    assert pages[:len(relevant)] == relevant
    assert fragment in ''.join(p['passage'] for p in relevant)
    assert ''.join(p['passage'] for p in sorted(pages, key=lambda p: p['offset'])) == text
    assert len({p['quote_ref'] for p in pages}) == len(pages)
    assert all(p['saved_verdict'] is None for p in pages)
    from street_story import review_packets
    api_page = review_packets.read(adapter, session, {'packet_ref': ref})
    assert api_page['items'][0]['offset'] == 0
    with svc.store.connection() as db:
        assert db.execute('SELECT decisions_json FROM live_review_packets WHERE packet_ref=?',
                          (ref,)).fetchone()[0] == '{}'
