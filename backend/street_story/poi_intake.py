"""Owner-led POI research over the existing deterministic MCP operator.

No inference, fake story, research job or publication is created here. Semantic
assessments are supplied by the authorized reader against its own frozen reads.
Canonical facts remain in poi_research_*; cinema is typed support metadata.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Annotated, Any, Literal
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .agent_evidence import NETWORK_SCHEMA, NETWORK_CONTRACT, confirmation_consistency_error
from .article_media import fetch_public, public_url, resolve_public, MAX_PAGE_BYTES
from .fact_ledger import candidate_assertion_id
from .poi_memory import (ensure_poi_identity, persist_research_memory,
                         _research_memory_facts, _canonical_poi_owner, _canonical_admission_valid)
from .research_runs import persist_source_version, canonical
from .source_assessment import CHECKS

CONTRACT = 'street-story.owner-poi-intake.v1'
ASPECTS = {'construction', 'architect', 'ownership', 'historic_use', 'current_use',
           'architecture', 'heritage', 'events', 'cinema'}
Text = Annotated[str, Field(min_length=1, max_length=1200)]
Ref = Annotated[str, Field(min_length=1, max_length=180)]


class Shape(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Attribution(Shape):
    origin: Literal['independent', 'partner_original', 'partner_credited', 'unknown']
    partner_id: str | None = None
    credit_read_ref: str | None = None
    credit_quote: str | None = None
    reason: Text

    @model_validator(mode='after')
    def credit_basis(self):
        partner = self.origin.startswith('partner_')
        if partner != bool(self.partner_id and self.credit_read_ref and self.credit_quote):
            raise ValueError('partner_credit_requires_own_read_basis')
        if not partner and any((self.partner_id, self.credit_read_ref, self.credit_quote)):
            raise ValueError('nonpartner_credit_fields_must_be_null')
        return self


class Frame(Shape):
    url: Text
    source_read_ref: Ref
    caption: Text
    visual_status: Literal['not_checked', 'checked_by_external_reader'] = 'not_checked'
    visual_evidence_ref: str | None = None
    visual_reason: str | None = None
    timecode: str | None = None
    edition: str | None = None
    attribution: Attribution
    # A researcher's association is not a licence or permission to publish.
    publication_status: Literal['not_cleared'] = 'not_cleared'


class Cinema(Shape):
    film_key: Ref
    title: Text
    release_year: Annotated[int, Field(ge=1888, le=2200)]
    metadata_read_ref: Ref
    metadata_quote: Text
    metadata_attribution: Attribution
    scene_key: Ref
    relation: Literal['exterior', 'interior', 'background', 'documented_location', 'miniature']
    description: Text
    screen_role: str | None = None
    filming_year: int | None = None
    frames: Annotated[list[Frame], Field(max_length=12)] = Field(default_factory=list)


class Evidence(Shape):
    read_refs: Annotated[list[Ref], Field(min_length=1, max_length=50)]
    source_version_id: Ref
    assessment: dict[str, Any]
    attribution: Attribution


class Fact(Shape):
    confidence: Annotated[float, Field(ge=0, le=1)]
    text: Text
    claim_key: Ref
    subject_scope: Literal['selected_building', 'complex_or_site']
    source_date: str | None
    sources: Annotated[list[Evidence], Field(min_length=1, max_length=12)]
    cinema: Cinema | None = None


class Aspect(Shape):
    status: Literal['found', 'not_found', 'not_applicable', 'pending']
    note: Text


class Completion(Shape):
    coverage: dict[str, Aspect]
    remaining_gaps: Annotated[list[Text], Field(max_length=40)]
    research_sufficient: bool
    next_action: Text

    @model_validator(mode='after')
    def full_building_scope(self):
        if set(self.coverage) != ASPECTS:
            raise ValueError('all_building_aspects_required')
        if self.research_sufficient and (self.remaining_gaps or any(
                x.status == 'pending' for x in self.coverage.values())):
            raise ValueError('completion_conflicts_with_remaining_work')
        return self


class Command(Shape):
    campaign: Ref
    pass_id: Ref
    request_key: Ref


class Read(Command):
    op: Literal['read']
    url: Text | None = None
    source_packet: Ref | None = None
    ordinal: Annotated[int, Field(ge=0)] = 0


class Osm(Command):
    op: Literal['osm']
    way_id: Annotated[int, Field(gt=0)]


class Lookup(Command):
    op: Literal['lookup']
    osm_packet: Ref
    query: Annotated[str, Field(min_length=2, max_length=300)]


class Commit(Command):
    op: Literal['commit']
    osm_packet: Ref
    lookup_packet: Ref
    poi_id: str | None
    canonical_name: Annotated[str, Field(min_length=1, max_length=300)]
    identity_reason: Text
    reviewed_candidate_ids: Annotated[list[Ref], Field(max_length=20)]
    facts: Annotated[list[Fact], Field(min_length=1, max_length=30)]
    completion: Completion


class Checkpoint(Command):
    op: Literal['checkpoint']
    last_source: str | None
    next_sources: Annotated[list[Text], Field(max_length=100)]
    unresolved: Annotated[list[Text], Field(max_length=100)]
    note: Text


COMMAND = TypeAdapter(Annotated[Read | Osm | Lookup | Commit | Checkpoint, Field(discriminator='op')])


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def packet(db, ref, campaign=None, pass_id=None, kind=None):
    row = db.execute('SELECT * FROM poi_intake_packets WHERE packet_id=?', (ref,)).fetchone()
    if (not row or campaign is not None and row['campaign'] != campaign
            or kind is not None and row['kind'] != kind):
        raise ValueError('intake_packet_not_owned')
    payload = json.loads(row['payload_json'])
    if pass_id is not None and payload['pass_id'] != pass_id:
        raise ValueError('intake_read_belongs_to_another_pass')
    return json.loads(row['result_json']), payload


def replay(db, command):
    row = db.execute('SELECT * FROM idempotency WHERE key=?', ('poi-intake:' + command.request_key,)).fetchone()
    if not row:
        return None
    if row['action'] != CONTRACT or row['request_digest'] != digest(command.model_dump()):
        raise ValueError('intake_idempotency_conflict')
    result, _ = packet(db, row['resource_id'], command.campaign, command.pass_id)
    return {**result, 'replayed': True}


def save_packet(db, command, result, now):
    saved = replay(db, command)
    if saved is not None:
        return saved
    ref = 'intake_' + uuid.uuid4().hex
    result = {**result, 'packet_id': ref, 'contract': CONTRACT, 'replayed': False}
    db.execute('INSERT INTO poi_intake_packets VALUES(?,?,?,?,?,?)',
               (ref, command.campaign, command.op, canonical(command.model_dump()), canonical(result), now))
    db.execute('INSERT INTO idempotency(key,action,request_digest,resource_type,resource_id,created_at) VALUES(?,?,?,?,?,?)',
               ('poi-intake:' + command.request_key, CONTRACT, digest(command.model_dump()), 'poi_intake_packet', ref, now))
    return result


def source_page(db, version, ordinal, media):
    row = db.execute('SELECT v.*,d.title FROM source_versions v JOIN source_documents d USING(document_id) '
                     'WHERE source_version_id=? AND access_scope=\'public\'', (version,)).fetchone()
    if not row or row['read_status'] != 'complete':
        raise ValueError('intake_source_not_complete_public')
    chunks = list(db.execute('SELECT * FROM source_chunks WHERE source_version_id=? ORDER BY ordinal', (version,)))
    if ordinal >= len(chunks):
        raise ValueError('intake_source_cursor_invalid')
    chunk = chunks[ordinal]
    return {'source_version_id': version, 'content_sha256': row['content_sha256'],
            'url': row['final_url'], 'title': row['title'], 'media': media,
            'chunks': [{'chunk_id': chunk['chunk_id'], 'text': chunk['chunk_text']}],
            'ordinal': ordinal, 'chunk_count': len(chunks),
            'next_ordinal': ordinal + 1 if ordinal + 1 < len(chunks) else None}


def parse_page(body, mime, final):
    media = []
    if mime in {'text/html', 'application/xhtml+xml'}:
        soup = BeautifulSoup(body, 'html.parser')
        title = soup.title.get_text(' ', strip=True) if soup.title else final
        for image in soup.find_all('img'):
            raw = image.get('data-src') or image.get('src')
            url = public_url(urljoin(final, raw or '')) if raw else None
            if url and url not in [x['url'] for x in media]:
                media.append({'url': url, 'caption': str(image.get('alt') or '')[:500]})
        for tag in soup(['script', 'style', 'noscript']):
            tag.decompose()
        text = soup.get_text('\n', strip=True)
    elif mime in {'text/plain', 'application/json'}:
        text, title = body.decode('utf-8'), final
    else:
        raise ValueError('intake_read_requires_html_text_or_json')
    if not text.strip():
        raise ValueError('intake_empty_source')
    return title, text, media[:100]


def osm_candidate(body, way_id):
    elements = json.loads(body)['elements']
    ways = [e for e in elements if e.get('type') == 'way' and e.get('id') == way_id]
    if len(ways) != 1:
        raise ValueError('intake_osm_way_missing')
    way = ways[0]
    nodes = {e['id']: e for e in elements if e.get('type') == 'node'}
    ids, tags = way.get('nodes', []), way.get('tags', {})
    if (not tags.get('building') or tags['building'] == 'no' or len(ids) < 4
            or ids[0] != ids[-1] or not set(ids).issubset(nodes)):
        raise ValueError('intake_requires_closed_osm_building')
    address = [(tags.get('addr:street'), tags.get('addr:housenumber'))]
    basis = 'osm_building_tags'
    if not all(address[0]):
        address = sorted({(nodes[n].get('tags', {}).get('addr:street'),
                           nodes[n].get('tags', {}).get('addr:housenumber')) for n in ids
                          if nodes[n].get('tags', {}).get('entrance') not in {None, '', 'no'}
                          and not any(nodes[n].get('tags', {}).get(k) for k in ('shop', 'office', 'amenity'))
                          and nodes[n].get('tags', {}).get('addr:street')
                          and nodes[n].get('tags', {}).get('addr:housenumber')})
        basis = 'osm_closed_way_entrance_membership'
    if len(address) != 1 or not all(address[0]):
        raise ValueError('intake_osm_address_missing_or_ambiguous')
    points = [{'lat': nodes[n]['lat'], 'lon': nodes[n]['lon']} for n in ids]
    key = f'osm:way:{way_id}'
    return {'candidate_id': key, 'osm_id': key, 'osm_version': way['version'],
            'url': f'https://www.openstreetmap.org/way/{way_id}', 'tags': tags,
            'address': address[0][0] + ' ' + address[0][1], 'address_basis': basis,
            'latitude': sum(p['lat'] for p in points[:-1]) / (len(points) - 1),
            'longitude': sum(p['lon'] for p in points[:-1]) / (len(points) - 1),
            'map_geometry': {'rings': [{'role': 'outer', 'closed': True, 'points': points}]}}


def lookup_rows(db, candidate, query):
    text = ' '.join(query.split()).casefold()
    key = candidate['candidate_id']
    rows = list(db.execute('SELECT DISTINCT p.* FROM pois p LEFT JOIN poi_aliases a ON a.poi_id=p.id '
        'WHERE p.status<>\'merged\' AND (a.normalized_value=? OR instr(a.normalized_value,?)>0 '
        'OR p.canonical_name=? OR (p.latitude BETWEEN ? AND ? AND p.longitude BETWEEN ? AND ?)) '
        'ORDER BY p.id LIMIT 21', (key, text, query, candidate['latitude'] - .0015,
        candidate['latitude'] + .0015, candidate['longitude'] - .0025, candidate['longitude'] + .0025)))
    result = []
    for row in rows:
        aliases = [dict(x) for x in db.execute('SELECT namespace,value,normalized_value FROM poi_aliases '
                    'WHERE poi_id=? ORDER BY namespace,normalized_value', (row['id'],))]
        result.append({**dict(row), 'aliases': aliases})
    return result


def own_source(db, refs, command, version=None, require_full=True):
    documents = []
    for ref in refs:
        result, _ = packet(db, ref, command.campaign, command.pass_id, 'read')
        documents.append(result)
    versions = {d['source_version_id'] for d in documents}
    if len(versions) != 1 or version is not None and versions != {version}:
        raise ValueError('intake_source_versions_mixed')
    version = next(iter(versions))
    frozen = db.execute('SELECT * FROM source_versions WHERE source_version_id=? AND access_scope=\'public\'',
                        (version,)).fetchone()
    chunks = {x['chunk_id']: x['chunk_text'] for x in db.execute(
        'SELECT * FROM source_chunks WHERE source_version_id=?', (version,))}
    received = {c['chunk_id']: c['text'] for d in documents for c in d['chunks']}
    if (not frozen or digest_text(frozen['normalized_text']) != frozen['content_sha256']
            or any(chunks.get(k) != v for k, v in received.items())
            or require_full and set(chunks) != set(received)):
        raise ValueError('intake_full_frozen_source_not_read')
    return dict(frozen), received, documents


def digest_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def literal_source(db, ref, quote, command):
    frozen, chunks, documents = own_source(db, [ref], command, require_full=False)
    if not quote or not any(quote in text for text in chunks.values()):
        raise ValueError('intake_literal_basis_missing')
    return documents[0]


def check_attribution(db, value, command):
    if value.origin.startswith('partner_'):
        literal_source(db, value.credit_read_ref, value.credit_quote, command)
    return value.model_dump()


def check_cinema(db, cinema, command):
    if cinema is None:
        return None
    literal_source(db, cinema.metadata_read_ref, cinema.metadata_quote, command)
    check_attribution(db, cinema.metadata_attribution, command)
    for frame in cinema.frames:
        result, _ = packet(db, frame.source_read_ref, command.campaign, command.pass_id, 'read')
        if frame.url not in {m['url'] for m in result.get('media', [])}:
            raise ValueError('intake_frame_not_in_read_source')
        if frame.visual_status == 'checked_by_external_reader' and not (
                frame.visual_evidence_ref and frame.visual_reason):
            raise ValueError('intake_frame_visual_basis_missing')
        if frame.timecode and not frame.edition:
            raise ValueError('intake_timecode_requires_edition')
        check_attribution(db, frame.attribution, command)
    return cinema.model_dump()


def checked_fact(db, fact, command, identity):
    sources, existing_ids = [], set()
    cinema = check_cinema(db, fact.cinema, command)
    for index, source in enumerate(fact.sources):
        frozen, chunks, documents = own_source(db, source.read_refs, command, source.source_version_id)
        row = source.assessment
        if not Draft202012Validator(NETWORK_SCHEMA).is_valid(row):
            raise ValueError('intake_complete_assessment_required')
        if any(row[k] != getattr(fact, k) for k in ('text', 'claim_key', 'subject_scope', 'source_date')):
            raise ValueError('intake_assessed_claim_changed')
        error = confirmation_consistency_error(row, {'contract': NETWORK_CONTRACT}, set(chunks))
        if error:
            raise ValueError(error)
        if row['verdict'] != 'supported' or row['conflicts_with_existing']:
            raise ValueError('intake_nonpositive_or_conflicting_assessment')
        for quote, ids in ((row['confirmed_fragment'], row['chunk_ids']),
                (row['checks']['relation_basis']['fragment'], row['checks']['relation_basis']['chunk_ids'])):
            if not any(quote in chunks.get(k, '') for k in ids):
                raise ValueError('intake_quote_not_in_own_chunk')
        existing_ids.add(row['existing_fact_id'])
        support = {'kind': 'external_reader_confirmed', 'text': row['confirmed_fragment'],
            'source_version_id': frozen['source_version_id'],
            'chunk_id': next(k for k in row['chunk_ids'] if row['confirmed_fragment'] in chunks[k]),
            'subject_scope': fact.subject_scope, 'source_date': fact.source_date,
            'attribution': check_attribution(db, source.attribution, command),
            'verification_level': 'one_model', 'assessment_origin': 'external_chatgpt',
            'assessment': row, **({'cinema': cinema} if cinema and index == 0 else {})}
        sources.append({'url': frozen['final_url'], 'title': documents[0]['title'], 'type': 'web',
                        'source_version_id': frozen['source_version_id'], 'supports': [support]})
    if len(existing_ids) != 1:
        raise ValueError('intake_existing_fact_declarations_disagree')
    existing = next(iter(existing_ids))
    key = identity['candidate_id']
    known = _research_memory_facts(db, identity, None)
    if existing:
        old = next((x for x in known if x['fact_id'] == existing), None)
        if not old or old['text'] != fact.text or old['eligibility'] != 'eligible':
            raise ValueError('intake_existing_fact_not_unchanged_eligible')
        fact_id = existing
    else:
        fact_id = candidate_assertion_id(key + ':' + fact.claim_key, fact.text)
        prior = db.execute('SELECT text,eligibility FROM poi_research_assertions WHERE poi_key=? AND assertion_id=?',
                           (key, fact_id)).fetchone()
        if prior and (prior['text'] != fact.text or prior['eligibility'] != 'eligible'):
            raise ValueError('intake_prior_adverse_or_changed_fact')
    return {'fact_id': fact_id, 'claim_key': fact.claim_key, 'text': fact.text,
            'confidence': fact.confidence, 'evidence_supported': True, 'sources': sources, 'selected': False}


def commit(db, command, now):
    osm, _ = packet(db, command.osm_packet, command.campaign, command.pass_id, 'osm')
    looked, lookup_command = packet(db, command.lookup_packet, command.campaign, command.pass_id, 'lookup')
    if lookup_command['osm_packet'] != command.osm_packet:
        raise ValueError('intake_lookup_for_other_building')
    candidate = dict(osm['candidate'])
    matches = lookup_rows(db, candidate, lookup_command['query'])
    if looked['registry_digest'] != digest(matches) or len(matches) > 20:
        raise ValueError('intake_registry_changed_repeat_lookup')
    if set(command.reviewed_candidate_ids) != {x['id'] for x in matches}:
        raise ValueError('intake_all_identity_candidates_must_be_reviewed')
    if command.poi_id and command.poi_id not in {x['id'] for x in matches}:
        raise ValueError('intake_selected_poi_not_in_lookup')
    key = candidate['candidate_id']
    owners = {x['poi_id'] for x in db.execute('SELECT poi_id FROM poi_aliases WHERE '
        'namespace IN (\'street_story_candidate\',\'osm_id\',\'osm\') AND normalized_value=?', (key,))}
    canonical_owners = {_canonical_poi_owner(db, x) for x in owners}
    if canonical_owners and canonical_owners != {command.poi_id}:
        raise ValueError('intake_osm_alias_already_owned')
    candidate['name'] = command.canonical_name
    identity = {'candidate_id': key, 'candidate_name': command.canonical_name,
                'candidate_url': candidate['url'], 'candidates': [candidate]}
    if command.poi_id and not owners:
        # Explicit model identity decision, never inferred from name/proximity.
        for namespace in ('street_story_candidate', 'osm_id'):
            db.execute('INSERT INTO poi_aliases(poi_id,namespace,value,normalized_value,created_at) VALUES(?,?,?,?,?)',
                       (command.poi_id, namespace, key, key, now))
    facts = [checked_fact(db, fact, command, identity) for fact in command.facts]
    poi_id = ensure_poi_identity(db, identity, latitude=candidate['latitude'], longitude=candidate['longitude'], now=now)
    if not poi_id or command.poi_id and poi_id != command.poi_id:
        raise ValueError('intake_identity_conflict')
    persist_research_memory(db, identity, facts, [], 'external owner research: ' + command.campaign, now,
                            research_run_id='intake:' + command.request_key)
    for fact in facts:
        memory = db.execute('SELECT * FROM poi_research_assertions WHERE poi_key=? AND assertion_id=?',
                            (key, fact['fact_id'])).fetchone()
        prior = json.loads(memory['review_proof_json'] or '{}')
        if _canonical_admission_valid(db, prior, fact['text'], key):
            continue  # Compatible extra evidence must not rewrite original admission.
        supports = []
        for observation in db.execute('SELECT * FROM poi_research_observations WHERE poi_key=? AND assertion_id=? '
                'AND research_run_id=?', (key, fact['fact_id'], 'intake:' + command.request_key)):
            for source in json.loads(observation['sources_json']):
                for support in source['supports']:
                    supports.append({'observation_id': observation['observation_id'], 'source_url': source['url'].rstrip('/'),
                        'span_sha256': digest_text(support['text'].strip()), 'source_version_id': support['source_version_id']})
        proof = {'version': 1, 'claim': fact['text'], 'subject': key, 'assertion_id': fact['fact_id'],
            'supports': supports, 'operation': {'contract': CONTRACT, 'request_key': command.request_key,
                'campaign': command.campaign, 'pass_id': command.pass_id, 'verification_level': 'one_model',
                'assessment_origin': 'external_chatgpt'}, 'reviewed_at': now, 'source_story_id': None,
            'accepted_revision_digest': 'external:v1:' + digest([fact['text'], supports]),
            'detector': 'external_chatgpt_owned_reads'}
        if not _canonical_admission_valid(db, proof, fact['text'], key):
            raise ValueError('intake_canonical_proof_failed')
        db.execute('UPDATE poi_research_assertions SET review_status=\'eligible\',eligibility=\'eligible\','
                   'reviewed_at=?,review_proof_json=? WHERE poi_key=? AND assertion_id=?',
                   (now, canonical(proof), key, fact['fact_id']))
    return {'poi_id': poi_id, 'candidate_id': key, 'address': candidate['address'],
            'fact_ids': [f['fact_id'] for f in facts], 'cinema': cinema_context(facts),
            'completion': command.completion.model_dump(), 'new_poi': command.poi_id is None,
            'research_jobs_created': 0, 'publications_created': 0}


def cinema_context(facts):
    links, contributions = [], []
    for fact in facts:
        for source in fact.get('sources', []):
            for support in source.get('supports', []):
                credit = support.get('attribution') or {}
                if credit.get('origin', '').startswith('partner_'):
                    contributions.append({'fact_id': fact['fact_id'], 'source_url': source['url'], **credit})
                cinema = support.get('cinema')
                if cinema:
                    links.append({'fact_id': fact['fact_id'], 'source_url': source['url'], **cinema})
                    credit = cinema['metadata_attribution']
                    if credit['origin'].startswith('partner_'):
                        contributions.append({'fact_id': fact['fact_id'], 'role': 'film_metadata', **credit})
    return {'links': links, 'partner_contributions': contributions,
            'coauthor_action': 'owner_decides_for_actually_used_material',
            'frames_auto_selected': False}


def cinema_editor_context(inventory):
    """Small voice-context projection; evidence remains under get_evidence."""
    eligible = [f for f in inventory if f.get('eligibility') == 'eligible']
    all_links = cinema_context(eligible)['links']
    selected_ids = {f['fact_id'] for f in eligible if f.get('selected')}
    selected = cinema_context([f for f in eligible if f.get('selected')])
    return {'films': [{k: link.get(k) for k in ('fact_id', 'film_key', 'title', 'release_year', 'relation', 'screen_role')}
                      | {'selected': link['fact_id'] in selected_ids,
                         'film_metadata_origin': link['metadata_attribution']['origin'],
                         'frame_candidates': len(link.get('frames', []))} for link in all_links[:24]],
            'film_link_count': len(all_links), 'films_truncated': len(all_links) > 24,
            'selected_partner_contributions': [{k: item.get(k) for k in ('fact_id', 'role', 'partner_id', 'origin', 'source_url')}
                                               for item in selected['partner_contributions'][:24]],
            'partner_contribution_count': len(selected['partner_contributions']),
            'partner_contributions_truncated': len(selected['partner_contributions']) > 24,
            'frames_auto_selected': False, 'coauthor_action': 'owner_decides_for_actually_used_material',
            'scope': 'current_selected_facts_not_final_published_bytes'}


async def execute(store, raw, *, client=None, resolver=resolve_public):
    command = COMMAND.validate_python(raw)
    if len(canonical(raw).encode()) > 256 * 1024:
        raise ValueError('intake_packet_too_large')
    with store.connection() as db:
        saved = replay(db, command)
    if saved is not None:
        return saved
    if isinstance(command, (Read, Osm)):
        if isinstance(command, Read) and command.source_packet:
            if command.url:
                raise ValueError('intake_use_url_or_source_packet')
            with store.tx() as db:
                prior, _ = packet(db, command.source_packet, command.campaign, kind='read')
                result = source_page(db, prior['source_version_id'], command.ordinal, prior['media'])
                return save_packet(db, command, result, store.now())
        if isinstance(command, Read) and (not command.url or command.ordinal != 0):
            raise ValueError('intake_initial_read_requires_url_and_zero_cursor')
        url = (f'https://api.openstreetmap.org/api/0.6/way/{command.way_id}/full.json'
               if isinstance(command, Osm) else command.url)
        if client is None:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
                final, mime, body = await fetch_public(http, url, MAX_PAGE_BYTES, resolver=resolver)
        else:
            final, mime, body = await fetch_public(client, url, MAX_PAGE_BYTES, resolver=resolver)
        title, text, media = parse_page(body, mime, final)
        candidate = osm_candidate(text, command.way_id) if isinstance(command, Osm) else None
        with store.tx() as db:
            saved = replay(db, command)
            if saved is not None:
                return saved
            frozen = persist_source_version(db, run_id=None, requested_url=url, final_url=final, title=title,
                content_type=mime, http_status=200, redirect_chain=[url, final] if url != final else [],
                normalized_text=text, read_status='complete', access_scope='public', now=store.now(),
                source_bytes_sha256=hashlib.sha256(body).hexdigest())
            result = ({'candidate': candidate, 'source_version_id': frozen['source_version_id']}
                      if candidate else source_page(db, frozen['source_version_id'], 0, media))
            return save_packet(db, command, result, store.now())
    with store.tx() as db:
        saved = replay(db, command)
        if saved is not None:
            return saved
        if isinstance(command, Lookup):
            osm, _ = packet(db, command.osm_packet, command.campaign, command.pass_id, 'osm')
            matches = lookup_rows(db, osm['candidate'], command.query)
            result = {'candidates': matches[:20], 'has_more': len(matches) > 20,
                      'registry_digest': digest(matches), 'lookup_completed': True}
        elif isinstance(command, Commit):
            result = commit(db, command, store.now())
        else:
            result = {'checkpoint': command.model_dump(exclude={'op', 'request_key'})}
        return save_packet(db, command, result, store.now())


def readback(store, *, campaign=None, packet_id=None, poi_id=None, story_id=None, after=0, limit=30):
    with store.connection() as db:
        if packet_id:
            return packet(db, packet_id, campaign)[0]
        if poi_id:
            owner = _canonical_poi_owner(db, poi_id)
            row = db.execute('SELECT * FROM pois WHERE id=?', (owner,)).fetchone()
            if not row:
                raise ValueError('intake_poi_not_found')
            aliases = [dict(x) for x in db.execute('SELECT namespace,value FROM poi_aliases WHERE poi_id=?', (owner,))]
            key = next((x['value'] for x in aliases if x['namespace'] == 'street_story_candidate'), None)
            facts = _research_memory_facts(db, {'candidate_id': key}, None, include_unreviewed=False) if key else []
            return {'poi': dict(row), 'aliases': aliases, 'facts': facts, 'cinema': cinema_context(facts)}
        if story_id:
            if not db.execute('SELECT 1 FROM stories WHERE id=?', (story_id,)).fetchone():
                raise ValueError('intake_story_not_found')
            facts = [{'fact_id': x['fact_id'], 'sources': json.loads(x['sources_json'])}
                     for x in db.execute('SELECT f.* FROM facts f JOIN fact_assertions a ON '
                        'a.story_id=f.story_id AND a.assertion_id=f.fact_id WHERE f.story_id=? '
                        'AND f.selected=1 AND a.eligibility=\'eligible\'', (story_id,))]
            return {'story_id': story_id, 'selected_fact_ids': [x['fact_id'] for x in facts],
                    **cinema_context(facts), 'scope': 'current_selected_facts_not_final_published_bytes'}
        if not campaign:
            raise ValueError('intake_readback_scope_required')
        rows = list(db.execute('SELECT rowid,* FROM poi_intake_packets WHERE campaign=? AND rowid>? '
            'ORDER BY rowid LIMIT ?', (campaign, after, min(100, max(1, limit)) + 1)))
        limit = min(100, max(1, limit))
        page = rows[:limit]
        return {'campaign': campaign, 'items': [{'cursor': x['rowid'], 'packet_id': x['packet_id'],
                'kind': x['kind'], 'pass_id': json.loads(x['payload_json'])['pass_id'],
                'result': json.loads(x['result_json']) if x['kind'] in {'commit', 'checkpoint'} else None}
                for x in page], 'next_cursor': page[-1]['rowid'] if len(rows) > len(page) and page else None}


def instructions():
    return {'contract': CONTRACT, 'command_schema': COMMAND.json_schema(),
            'assessment_schema': NETWORK_SCHEMA, 'reading_instructions': CHECKS,
            'scope': 'All building history, not only cinema. Same public frozen source and exact physical POI.',
            'verification': 'One external ChatGPT reader. No independent verification is claimed.',
            'identity_limit': 'v1: one closed OSM building way with its own address or one unambiguous member entrance address.',
            'media_limit': 'Frame associations may be retained; publication permission and overlay are not granted here.'}
