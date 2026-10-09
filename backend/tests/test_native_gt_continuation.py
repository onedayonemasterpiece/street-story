"""Closed G hypotheses use existing joint2, normal identity/facts and POI gates."""
import base64
import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from street_story import identity_discovery
from street_story.gemini import GeminiUnavailable
from street_story.headless_facts import HeadlessFacts
from street_story.identity_plan_diagnostics import joint_followup_marker
from street_story.identity_proof import accepted_identity
from street_story.providers import GeminiClient, PermanentProviderError, RetryableProviderError
from street_story.research_runs import begin_research_run
from test_architectural_text_identity import TEXT, text_inputs
from test_geometry_identity_plan import geometry_decision, geometry_setup, payload
from test_headless_facts import CLAIM, Researcher, controlled_public_dns, review_candidates  # noqa: F401


@pytest.mark.asyncio
async def test_sufficient_original_native_readback_needs_no_google_executor_or_t(tmp_path):
    svc, story, active = geometry_setup(tmp_path)
    closed = {'phase': 'completed', 'turn_id': 'already-addressed-original'}
    async def readback(snapshot, prompt, schema, images, host_context):
        return {'result': payload(geometry_decision()), 'receipt': closed, 'host_context': host_context}
    svc.providers.gemini = SimpleNamespace()
    svc.providers.research = SimpleNamespace(source_map_receipt=lambda snapshot: closed, plan_source_map=readback)
    await identity_discovery.prepare_search_plan(svc, story, '', active)
    assert story['_identity_geometry_result']['proof_kind'] == 'geometry'
    assert story['_identity_geometry_result']['candidate_id'] == 'osm:way:2'
    assert joint_followup_marker(svc, story) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['geometry', 'text', 'uncertain', 'unknown'])
async def test_native_closed_insufficient_proof_preserves_hypothesis_and_uses_one_joint2(tmp_path, outcome):
    svc, snapshot, active = geometry_setup(tmp_path)
    sid = snapshot['id']
    url = 'https://archive.example/gate-history'
    raw_body = ('<main><p>' + TEXT + '</p><p>' + CLAIM + '</p></main>').encode()
    svc.store.cache_put('public-article-acquisition-v1:' + hashlib.sha256(url.encode()).hexdigest(),
        {'final_url': url, 'mime': 'text/html', 'body': base64.b64encode(raw_body).decode(),
         'sha256': hashlib.sha256(raw_body).hexdigest(), 'acquired_at': svc.store.now()}, 86400)
    bad = geometry_decision()
    bad['spatial_correspondence']['pattern_kind'] = 'frontage_sequence'
    initial = payload(bad)
    if outcome != 'geometry':
        initial.update(selected_wikipedia_page_ids=['13'], subject_article_bindings=[{
            'article_id': 'wiki:13', 'candidate_id': 'osm:way:2', 'scope': 'Main physical footprint',
            'binding_basis': 'Received page explicitly describes this footprint, excluding its neighbor.',
            'physical_binding_resolved': True}])
    _fixture, _candidates, decision, _receipt = text_inputs(candidate_id='osm:way:2', url=url)
    for row in [*decision['article_bindings'], *decision['correspondences']]:
        row['article_id'] = 'wiki:13'
    decision['material_alternatives'] = [{'candidate_id': 'osm:way:3',
        'reason': 'SOURCE/text places the return behind the bay; this neighboring body has the reverse order.'}]
    if outcome == 'uncertain':
        decision.update(decision='uncertain', material_alternatives_resolved=False,
            unresolved_contradictions=['The visible return does not resolve the physical wing.'])
    calls, native_receipts, addressed_images = [], [], []

    class Executor:
        leases = 0
        async def execute(self, role, call):
            if not calls:
                calls.append('google_not_sent')
                raise GeminiUnavailable(svc.store.now() + 120, 'fixture_unsent')
            assert not self.leases
            self.leases += 1
            try:
                return await call('fixture', 60)
            finally:
                self.leases -= 1

    executor = Executor()
    reader = GeminiClient(svc.settings, svc.store)
    async def forbidden_http(request):
        pytest.fail('Acquired article bytes must be reused in both T and facts')
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(forbidden_http))

    async def native(story, prompt, schema, images, host_context):
        assert calls == ['google_not_sent'] and executor.leases == 0
        assert [label for label, _mime, _bytes in images] == ['SOURCE', 'MAP']
        addressed_images.extend(images)
        calls.append('native_original')
        receipt = {'phase': 'completed', 'turn_id': 'closed-original', 'result': copy.deepcopy(initial)}
        native_receipts.append(receipt)
        return {'result': copy.deepcopy(initial), 'receipt': receipt, 'host_context': host_context}

    async def generate(key, timeout, contents, config, **kwargs):
        assert calls == ['google_not_sent', 'native_original']
        assert contents[0].inline_data.data == addressed_images[0][2]
        assert contents[1].inline_data.data == addressed_images[1][2]
        with svc.store.connection() as db:
            retained = json.loads(svc._story_row(db, sid)['research_json'])['identity_physical_hypothesis']
        assert retained['state'] == 'unconfirmed_physical_hypothesis' and not retained['identity_accepted']
        assert retained['initial_decision']['candidate_ids'] == ['osm:way:2', 'osm:way:3']
        assert retained['source_map_receipt']['model_id'] == 'gpt-6-luna'
        assert 'different physical bodies' in retained['reason']['reason']
        assert json.loads(retained['raw_json']) == initial
        calls.append('joint2')
        if outcome == 'unknown':
            raise TimeoutError('Original joint2 outcome is unknown')
        if outcome == 'geometry':
            assert 'never just rename the pattern' in contents[-1]
            return SimpleNamespace(text=json.dumps(payload(geometry_decision())))
        assert TEXT in contents[-1] and CLAIM in contents[-1]
        assert 'previous_model_hypotheses_not_evidence' in contents[-1]
        assert 'first_wave_hypotheses' not in config.system_instruction
        return SimpleNamespace(text=json.dumps(decision))

    async def lookup(*args):
        return snapshot['_identity_map_snapshot']
    async def wikipedia(*args):
        return [] if outcome == 'geometry' else [{'pageid': 13, 'title': 'Observed physical building', 'url': url}]
    async def forbidden(*args, **kwargs):
        pytest.fail('No REF, third judge or replacement planner is allowed')
    svc.providers.osm.lookup = lookup
    svc.providers.wikipedia.nearby = wikipedia
    svc.providers.gemini = SimpleNamespace(executor=executor, _generate=generate,
        web_search_routes=[('fixture-model', object(), object(), executor)],
        _fetch_page_documents=reader._fetch_page_documents)
    svc.providers.research = SimpleNamespace(source_map_available=True, vision_available=False, plan_source_map=native,
        source_map_receipt=lambda story: native_receipts[-1] if native_receipts else None,
        plan_identity_search=forbidden)
    svc._identify_photo = forbidden
    try:
        svc.ensure_identity(sid)
        await svc.run_once()
        current = svc.story(sid)
        assert calls == ['google_not_sent', 'native_original', 'joint2'] and executor.leases == 0
        if outcome in {'uncertain', 'unknown'}:
            assert not accepted_identity(current['visual_identity']) and current['facts'] == []
            fresh, _research = svc._identity_snapshot(sid)
            fresh.update(_identity_map_snapshot=snapshot['_identity_map_snapshot'],
                _identity_observed_candidates=snapshot['_identity_observed_candidates'])
            expected = PermanentProviderError if outcome == 'uncertain' else RetryableProviderError
            with pytest.raises(expected, match='identity_architectural_text_uncertain|identity_joint_followup_outcome_unknown'):
                await identity_discovery.prepare_search_plan(svc, fresh, '', active)
            assert len(calls) == 3
            return
        identity = current['visual_identity']
        assert current['state'] == 'identity_ready' and accepted_identity(identity)
        assert identity['candidate_id'] == 'osm:way:2'
        assert identity['proof_kind'] == ('geometry' if outcome == 'geometry' else 'architectural_text')
        fresh, _research = svc._identity_snapshot(sid)
        assert joint_followup_marker(svc, fresh)['phase'] == 'response_closed'
        if outcome == 'geometry':
            return
        # Continue the accepted T result through the unchanged fact worker and
        # own-evidence review, then read the canonical POI ledger independently.
        researcher = Researcher()
        researcher.expected_identity = 'osm:way:2'
        svc.providers.research = researcher
        with svc.store.tx() as db:
            row = svc._story_row(db, sid)
            jid = svc._enqueue_job(db, sid, 'research', 'offline-gt-facts',
                {'identity_generation': 0, 'photo_sha256': row['photo_sha256']})
            db.execute("UPDATE jobs SET state='running',attempts=1 WHERE id=?", (jid,))
            job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
            begin_research_run(db, story_id=sid, poi_key='osm:way:2', goal='Find historical facts', scope='history',
                expected_story_revision=row['revision'], identity_generation=0, run_id='offline-gt', now=svc.store.now())
        await HeadlessFacts(svc).run(job, 'offline-gt', 'Find historical facts', 'history')
        assert researcher.searches == 0
        assert svc.story(sid)['facts'][0]['eligibility'] == 'unreviewed'
        await review_candidates(svc, sid, 'offline-gt')
        independent = type(svc)(svc.settings, providers=svc.providers)
        assert independent.story(sid)['facts'][0]['eligibility'] == 'eligible'
        with independent.store.connection() as db:
            assert db.execute("SELECT eligibility FROM poi_research_assertions WHERE poi_key='osm:way:2'").fetchone()[0] == 'eligible'
    finally:
        await reader.search_http.aclose()
