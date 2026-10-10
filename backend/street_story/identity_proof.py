"""Validate evidence transport for accepted physical identity, without a second judge.

The joint model decides whether spatial evidence is sufficient. These checks only
bind that decision to the actual SOURCE/map, observed primitives and generation.
Reference vision keeps its separate, stricter comparison contract.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

POLICY = 'spatial_identity_v1'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _scope(story):
    research = json.loads(story.get('research_json') or '{}')
    control = (research.get('research_controls') or {}).get('identity') or {}
    return {'photo_sha256': story.get('photo_sha256'),
        'generation': int(story.get('_identity_generation', research.get('identity_generation') or 0)),
        'control_revision': int(story.get('_identity_research_control_revision', control.get('revision') or 0))}


def freeze_geometry_proof(story, decision, source_map_receipt, candidates):
    """Accept a semantic verdict only when its supplied evidence really exists."""
    from jsonschema import Draft202012Validator
    from .identity_candidate_policy import candidate_identity_eligible
    from .identity_scene import scene_entries
    from .identity_scene import scene_camera_context
    from .identity_source_selection import geometry_decision_schema
    from .identity_spatial_features import geometry_feature
    from .identity_geometry_contract import CONTRACT, measured_correspondence
    from .identity_subject_binding import article_candidate
    entries = scene_entries(story, candidates)
    catalog = {entry['candidate_id']: entry for entry in entries}
    receipt = source_map_receipt or {}
    structured = receipt.get('geometry_contract') == CONTRACT
    if not isinstance(decision, dict) or not Draft202012Validator(
            geometry_decision_schema(list(catalog), structured=structured or
                isinstance(decision, dict) and 'spatial_correspondence' in decision)).is_valid(decision):
        return None
    cid = decision.get('candidate_id')
    observed = story.get('_identity_observed_candidates') or []
    candidate = next((item for item in [*observed, *candidates]
        if item.get('candidate_id') == cid), {})
    scope = _scope(story)
    if (decision['decision'] != 'accepted_geometry'
            or decision.get('next_action', {}).get('kind', 'none') != 'none'
            or not candidate or not candidate_identity_eligible(candidate)
            or article_candidate(candidate) or receipt.get('joint_image_input') is not True
            or not isinstance(scope['photo_sha256'], str) or not scope['photo_sha256'].strip()
            or receipt.get('source_photo_sha256') != scope['photo_sha256']
            or not _hash(receipt.get('original_source_sha256'))
            or not _hash(receipt.get('model_source_sha256'))
            or story.get('_identity_original_source_sha256') is not None
                and story['_identity_original_source_sha256'] != receipt.get('original_source_sha256')
            or not _hash(receipt.get('map_image_sha256'))):
        return None
    manifest = receipt.get('manifest') or {}
    table = manifest.get('objects') or {}
    columns = table.get('columns') or []
    received = {dict(zip(columns, row)).get('candidate_id') for row in table.get('rows') or []}
    if cid not in received or manifest.get('image_sha256') != receipt['map_image_sha256']:
        return None
    labels = {dict(zip(columns, row)).get('label'): dict(zip(columns, row)).get('candidate_id')
        for row in table.get('rows') or []}
    if (receipt.get('map_identity_labels_required') is True or 'candidate_label' in decision) and (
            labels.get(decision.get('candidate_label')) != cid):
        return None
    coverage = decision['bounded_coverage']
    if (coverage['material_alternatives_resolved'] is not True
            or not decision['scope'].strip() or not coverage['scope'].strip()
            or any(not value.strip() for value in decision['camera_pose'].values())
            or not decision['decisive_relations']):
        return None
    features = []
    selected_feature = False
    for relation in decision['decisive_relations']:
        if (not relation['source_observation'].strip() or not relation['correspondence'].strip()
                or not relation['map_features']):
            return None
        for reference in relation['map_features']:
            if reference['candidate_id'] not in received:
                return None
            feature = geometry_feature(story, candidates, reference)
            if not feature:
                return None
            selected_feature |= reference['candidate_id'] == cid
            features.append({'reference': reference, 'observed_feature': feature})
    if not selected_feature or any(not item['reason'].strip() or item['candidate_id'] == cid
            or item['candidate_id'] not in received
            for item in decision['rejected_alternatives']):
        return None
    measurement = measured_correspondence(story, candidates, decision, receipt) if structured else None
    if structured and measurement is None:
        return None
    proof = {'validated': True, 'policy': POLICY, 'candidate_id': cid, **scope,
        'source_photo_sha256': scope['photo_sha256'], 'map_image_sha256': receipt['map_image_sha256'],
        'original_source_sha256': receipt['original_source_sha256'],
        'model_source_sha256': receipt['model_source_sha256'],
        'scene_context_sha256': _digest({'map': story.get('_identity_map_snapshot') or
            json.loads(story.get('research_json') or '{}').get('osm') or {}, 'camera': scene_camera_context(story)}),
        'decision': decision, 'observed_features': features, 'source_map_receipt': receipt}
    if structured:
        proof['spatial_correspondence'] = measurement
    # Freeze the same JSON object keys that ordinary durable storage reads.
    # Keep the historical digest algorithm unchanged for existing receipts.
    proof = json.loads(json.dumps(proof, ensure_ascii=False))
    proof['proof_sha256'] = _digest(proof)
    return proof


def _valid_frozen(identity, *, photo_sha256=None, generation=None, control_revision=None):
    proof = identity.get('geometry_proof') or {}
    if (identity.get('status') != 'match' or identity.get('proof_kind') not in {'geometry', 'combined'}
            or proof.get('validated') is not True or proof.get('policy') != POLICY
            or proof.get('candidate_id') != identity.get('candidate_id')
            or not isinstance(proof.get('photo_sha256'), str) or not proof['photo_sha256'].strip()
            or not _hash(proof.get('map_image_sha256'))
            or proof.get('source_photo_sha256') != proof.get('photo_sha256')
            or (proof.get('decision') or {}).get('decision') != 'accepted_geometry'
            or not proof.get('observed_features')
            or proof.get('proof_sha256') != _digest({key: value for key, value in proof.items() if key != 'proof_sha256'})):
        return False
    for key, expected in (('photo_sha256', photo_sha256), ('generation', generation), ('control_revision', control_revision)):
        value = identity.get(key)
        if value is not None and value != proof.get(key):
            return False
        if expected is not None and expected != proof.get(key):
            return False
    return True


def geometry_result_valid(raw, candidates, story=None):
    if not _valid_frozen(raw, **(_scope(story) if story is not None else {})):
        return False
    if story is None:
        return raw.get('candidate_id') in {item.get('candidate_id') for item in candidates}
    proof = raw['geometry_proof']
    replay = freeze_geometry_proof(story, proof['decision'], proof['source_map_receipt'], candidates)
    return bool(replay and replay['proof_sha256'] == proof['proof_sha256'])


def verified_physical_identity(identity, photo_sha256=None, generation=None, control_revision=None):
    if not isinstance(identity, dict) or identity.get('status') != 'match':
        return False
    if identity.get('proof_kind') in {'geometry', 'architectural_text', 'combined'}:
        # Stop/resume changes the dispatch epoch, not an already committed
        # object's physical identity. Pending raw verdict replay remains strict
        # in geometry_result_valid; every new fact operation has its own control
        # fence. Do not rewrite the original receipt to a later control epoch.
        resolved = identity.get('resolved_at')
        if (not isinstance(resolved, bool) and isinstance(resolved, (int, float))
                and math.isfinite(resolved)):
            control_revision = None
        return (_valid_frozen(identity, photo_sha256=photo_sha256,
            generation=generation, control_revision=control_revision)
            or _valid_text_frozen(identity, photo_sha256=photo_sha256,
                generation=generation, control_revision=control_revision))
    return (identity.get('visual_reference_verified') is True
        and all(expected is None or key not in identity or identity.get(key) == expected for key, expected in (
            ('photo_sha256', photo_sha256), ('generation', generation), ('control_revision', control_revision))))


def accepted_identity(identity, photo_sha256=None, generation=None, control_revision=None):
    if not isinstance(identity, dict) or identity.get('status') not in {'match', 'owner_confirmed'}:
        return False
    if identity.get('proof_kind') in {'geometry', 'architectural_text', 'combined'}:
        return verified_physical_identity(identity, photo_sha256, generation, control_revision)
    return all(expected is None or key not in identity or identity.get(key) == expected for key, expected in (
        ('photo_sha256', photo_sha256), ('generation', generation), ('control_revision', control_revision)))


TEXT_POLICY = 'architectural_text_identity_v1'
TEXT_CONTRACT = 'source-text-architecture-v2'
STRUCTURAL_FEATURES = {'levels', 'window_axes', 'bay', 'roof', 'openings', 'composition', 'outline'}


def architectural_text_decision_schema(candidate_ids, article_ids, *, material_alternative_limit=8, structural=False,
        source_span_refs=None, physical_link_inventory=None):
    """The joint SOURCE/text model decides sufficiency and physical scope."""
    text = {'type': 'string', 'maxLength': 600}
    cid = {'type': 'string', 'enum': list(dict.fromkeys([*candidate_ids, '']))}
    aid = {'type': 'string', 'enum': list(dict.fromkeys(article_ids))}
    schema = {'type': 'object', 'properties': {
        'decision': {'type': 'string', 'enum': ['accepted_architectural_text', 'uncertain'],
            'description': 'Resolve both the SOURCE/article architectural match and the exact main photographed '
                'OSM body using its received MAP label, contours and adjacency. An article/address can cover a '
                'complex containing several attached bodies. If a limitation leaves which individual footprint '
                'is pictured unresolved, choose uncertain; a matching article or generic feature combination '
                'does not resolve that physical scope. Preserve the alternative bodies as useful hypotheses.'},
        'candidate_id': cid, 'scope': text, 'discriminating_combination': text,
        'article_bindings': {'type': 'array', 'maxItems': 2, 'items': {'type': 'object', 'properties': {
            'article_id': aid, 'candidate_id': cid, 'scope': text, 'binding_basis': text,
            'physical_binding_resolved': {'type': 'boolean'}},
            'required': ['article_id', 'candidate_id', 'scope', 'binding_basis', 'physical_binding_resolved'],
            'additionalProperties': False}},
        'correspondences': {'type': 'array', 'maxItems': 12, 'items': {'type': 'object', 'properties': {
            'article_id': aid, 'source_quote': text, 'source_observation': text,
            'status': {'type': 'string', 'enum': ['stable_match', 'not_observable',
                'structural_contradiction', 'historical_or_mutable_difference']}, 'reason': text},
            'required': ['article_id', 'source_quote', 'source_observation', 'status', 'reason'],
            'additionalProperties': False}},
        'material_alternatives': {'type': 'array', 'maxItems': material_alternative_limit, 'items': {'type': 'object', 'properties': {
            'candidate_id': cid, 'reason': text}, 'required': ['candidate_id', 'reason'], 'additionalProperties': False}},
        'material_alternatives_resolved': {'type': 'boolean'},
        'unresolved_contradictions': {'type': 'array', 'maxItems': 8, 'items': text},
        'limitations': {'type': 'array', 'maxItems': 8, 'items': text}},
        'required': ['decision', 'candidate_id', 'scope', 'discriminating_combination', 'article_bindings',
            'correspondences', 'material_alternatives', 'material_alternatives_resolved',
            'unresolved_contradictions', 'limitations'], 'additionalProperties': False}
    if physical_link_inventory is not None:
        from .identity_architectural_evidence import physical_link_schema
        schema['properties']['physical_link_evidence'] = physical_link_schema(
            article_ids, physical_link_inventory['candidate_ids'],
            physical_link_inventory['publisher_refs'], physical_link_inventory['osm_refs'])
        schema['required'].append('physical_link_evidence')
    if structural:
        correspondence = schema['properties']['correspondences']['items']
        correspondence['properties']['feature_kind'] = {'type': 'string', 'enum': sorted(
            STRUCTURAL_FEATURES | {'color_or_finish', 'generic_style', 'historical_fact'})}
        correspondence['required'].append('feature_kind')
    if source_span_refs:
        correspondence = schema['properties']['correspondences']['items']
        correspondence['properties'].pop('source_quote')
        correspondence['properties']['source_span_ref'] = {'type': 'string', 'enum': list(source_span_refs)}
        correspondence['required'].remove('source_quote')
        correspondence['required'].append('source_span_ref')
    from .identity_candidate_policy import research_priority_schema
    schema['properties']['research_priority'] = research_priority_schema(candidate_ids)
    return schema


def _text_candidate_context(candidate):
    # Promotion/model labels and aliases decorate the same exact physical ID.
    # Actual received physical metadata changes still invalidate pending replay.
    return {key: candidate[key] for key in ('candidate_id', 'osm_id', 'type',
        'identity_role', 'identity_eligible', 'physical_subject_candidate_id',
        'physical_subject_evidence', 'map_object', 'map_geometry', 'map_address',
        'map_coordinates', 'physical_component', 'physical_components', 'wikidata',
        'wikipedia_url') if key in candidate}


def _received_literal_record_matches(received, observed):
    # Compact DTOs can omit fields restored later from the same OSM record.
    # Every received value must still match literally; new fields are not
    # credited to the model's original evidence or interpreted by the host.
    left = {key: value for key, value in received.items() if key != 'ref'}
    right = {key: value for key, value in observed.items() if key != 'ref'}
    literal = left.get('literal_value')
    if isinstance(literal, dict) and isinstance(right.get('literal_value'), dict):
        if not literal or any(right['literal_value'].get(key) != value for key, value in literal.items()):
            return False
        right = {**right, 'literal_value': literal}
    return left == right


def freeze_architectural_text_proof(story, decision, source_text_receipt, candidates):
    """Freeze actual SOURCE/text inputs and literal pointers, never judge features.

    The source adapter verifies raw cached body bytes before setting
    raw_body_sha256_verified. Text here is the actual normalized model input;
    no page/image is credited from its title, snippet or search result alone.
    """
    from jsonschema import Draft202012Validator
    from .article_media import public_url
    from .identity_candidate_policy import candidate_identity_eligible
    from .identity_subject_binding import article_candidate
    scope = _scope(story)
    receipt = source_text_receipt or {}
    if not isinstance(receipt, dict):
        return None
    articles = receipt.get('articles') or []
    if not isinstance(articles, list) or not 1 <= len(articles) <= 2:
        return None
    table = {}
    for article in articles:
        if not isinstance(article, dict):
            return None
        aid, text = article.get('article_id'), article.get('text')
        if (not isinstance(aid, str) or not aid or aid in table
                or not public_url(str(article.get('url') or ''))
                or article.get('raw_body_sha256_verified') is not True
                or article.get('input_kind') != 'acquired_article_text'
                or not _hash(article.get('source_sha256'))
                or not isinstance(text, str) or not text.strip()
                or article.get('text_sha256') != hashlib.sha256(text.encode()).hexdigest()):
            return None
        table[aid] = article
    if isinstance(decision, dict) and any('source_span_ref' in item
            for item in decision.get('correspondences') or [] if isinstance(item, dict)):
        from .identity_architectural_pool import resolve_joint_source_spans
        try:
            decision = resolve_joint_source_spans(decision, receipt)
        except (ValueError, KeyError, TypeError):
            return None
    observed = story.get('_identity_observed_candidates') or []
    catalog = {item.get('candidate_id'): item for item in [*candidates, *observed] if isinstance(item, dict)}
    prior = receipt.get('conditional_initial_decision')
    prior_ids = prior.get('candidate_ids') if isinstance(prior, dict) else None
    structural = receipt.get('text_contract') == TEXT_CONTRACT
    inventory = receipt.get('physical_link_inventory')
    link_proof = None
    if inventory is not None:
        from .identity_architectural_evidence import literal_evidence_inventory, validate_model_physical_links
        links = decision.get('physical_link_evidence') if isinstance(decision, dict) else None
        try:
            expected = literal_evidence_inventory(story, list(catalog.values()), list(table.values()),
                candidate_ids=inventory['candidate_ids'])
            for link in links or []:
                for kind, field in [('publisher_refs', 'publisher_ref'), ('osm_refs', 'osm_ref')]:
                    received = inventory[kind].get(link.get(field))
                    if not received or not any(_received_literal_record_matches(received, row)
                            for row in expected[kind].values()):
                        return None
            link_proof = validate_model_physical_links(inventory, links, decision)
        except (ValueError, KeyError, TypeError):
            return None
        if link_proof.get('supported') is not True:
            return None
    if not isinstance(decision, dict) or not Draft202012Validator(
            architectural_text_decision_schema(list(catalog), list(table),
                material_alternative_limit=max(8, len(prior_ids)) if isinstance(prior_ids, list) else 8,
                physical_link_inventory=inventory,
                structural=structural or isinstance(decision, dict) and any('feature_kind' in item
                    for item in decision.get('correspondences') or [] if isinstance(item, dict)))).is_valid(decision):
        return None
    cid, candidate = decision.get('candidate_id'), catalog.get(decision.get('candidate_id'))
    if (decision['decision'] != 'accepted_architectural_text' or not candidate
            or not candidate_identity_eligible(candidate) or article_candidate(candidate)
            or receipt.get('source_image_input') is not True
            or receipt.get('source_photo_sha256') != scope['photo_sha256']
            or not isinstance(scope['photo_sha256'], str) or not scope['photo_sha256'].strip()
            or not _hash(receipt.get('original_source_sha256')) or not _hash(receipt.get('model_source_sha256'))
            or story.get('_identity_original_source_sha256') is not None
                and story['_identity_original_source_sha256'] != receipt['original_source_sha256']
            or not decision['scope'].strip() or not decision['discriminating_combination'].strip()
            or decision['material_alternatives_resolved'] is not True or decision['unresolved_contradictions']
            or not decision['article_bindings'] or not decision['correspondences']):
        return None
    bound = set()
    for binding in decision['article_bindings']:
        if binding['physical_binding_resolved'] is False:
            continue  # Keep the model's negative comparison without crediting it.
        if (binding['candidate_id'] != cid or binding['physical_binding_resolved'] is not True
                or not binding['scope'].strip() or not binding['binding_basis'].strip()):
            return None
        bound.add(binding['article_id'])
    stable = False
    for relation in decision['correspondences']:
        if (not relation['source_quote'].strip()
                or relation['source_quote'] not in table[relation['article_id']]['text']
                or not relation['source_observation'].strip() or not relation['reason'].strip()):
            return None
        stable |= relation['article_id'] in bound and relation['status'] == 'stable_match' and (not structural
            or relation.get('feature_kind') in STRUCTURAL_FEATURES)
    if not stable or any(item['candidate_id'] in {'', cid} or not item['reason'].strip()
            for item in decision['material_alternatives']):
        return None
    if prior is not None:
        if (not isinstance(prior, dict) or prior.get('policy') != 'conditional-initial-joint-v1'
                or prior.get('input_kind') != 'model_hypothesis_not_evidence'
                or not isinstance(prior.get('candidate_ids'), list)
                or any(not isinstance(value, str) or value not in catalog
                    for value in prior['candidate_ids'])
                or len(set(prior['candidate_ids'])) != len(prior['candidate_ids'])):
            return None
        # This checks pointer coverage only. A previous model rejection is
        # conditional context, never proof of architectural discrimination.
        required = set(prior['candidate_ids']) - {cid}
        addressed = {item['candidate_id'] for item in decision['material_alternatives']}
        if not required <= addressed:
            return None
    proof = {'validated': True, 'policy': TEXT_POLICY, 'candidate_id': cid, **scope,
        'source_photo_sha256': scope['photo_sha256'], 'original_source_sha256': receipt['original_source_sha256'],
        'model_source_sha256': receipt['model_source_sha256'], 'candidate_context_sha256': _digest(_text_candidate_context(candidate)),
        'decision': decision, 'source_text_receipt': receipt,
        'article_sources': [{key: article[key] for key in ('article_id', 'url', 'source_sha256', 'text_sha256',
            'source_version_id', 'title', 'address', 'coordinates', 'scope') if key in article}
            for aid, article in table.items() if aid in bound]}
    # Freeze the same JSON object keys that ordinary durable storage reads.
    # Keep the historical digest algorithm unchanged for existing receipts.
    if link_proof is not None:
        proof['physical_link_validation'] = link_proof
    proof = json.loads(json.dumps(proof, ensure_ascii=False))
    proof['proof_sha256'] = _digest(proof)
    return proof


def _valid_text_frozen(identity, *, photo_sha256=None, generation=None, control_revision=None):
    proof = identity.get('architectural_text_proof') or {}
    if not isinstance(proof, dict) or not isinstance(proof.get('decision'), dict):
        return False
    if (identity.get('status') != 'match' or identity.get('proof_kind') not in {'architectural_text', 'combined'}
            or proof.get('validated') is not True or proof.get('policy') != TEXT_POLICY
            or proof.get('candidate_id') != identity.get('candidate_id')
            or not isinstance(proof.get('photo_sha256'), str) or not proof['photo_sha256'].strip()
            or proof.get('source_photo_sha256') != proof.get('photo_sha256')
            or not _hash(proof.get('original_source_sha256')) or not _hash(proof.get('model_source_sha256'))
            or (proof.get('decision') or {}).get('decision') != 'accepted_architectural_text'
            or not proof.get('article_sources')
            or proof.get('proof_sha256') != _digest({key: value for key, value in proof.items() if key != 'proof_sha256'})):
        return False
    for key, expected in (('photo_sha256', photo_sha256), ('generation', generation), ('control_revision', control_revision)):
        if identity.get(key) is not None and identity[key] != proof.get(key):
            return False
        if expected is not None and expected != proof.get(key):
            return False
    return True


def architectural_text_result_valid(raw, candidates, story=None):
    if not _valid_text_frozen(raw, **(_scope(story) if story is not None else {})):
        return False
    if story is None:
        return raw.get('candidate_id') in {item.get('candidate_id') for item in candidates}
    proof = raw['architectural_text_proof']
    replay = freeze_architectural_text_proof(story, proof['decision'], proof['source_text_receipt'], candidates)
    return bool(replay and replay['proof_sha256'] == proof['proof_sha256'])


def non_reference_result_valid(raw, candidates, story=None):
    return geometry_result_valid(raw, candidates, story) or architectural_text_result_valid(raw, candidates, story)


def physical_scope(identity):
    proof = (identity.get('geometry_proof') or identity.get('architectural_text_proof') or {}) if isinstance(identity, dict) else {}
    return str((proof.get('decision') or {}).get('scope') or '')
