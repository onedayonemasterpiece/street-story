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
    from .identity_subject_binding import article_candidate
    entries = scene_entries(story, candidates)
    catalog = {entry['candidate_id']: entry for entry in entries}
    if not isinstance(decision, dict) or not Draft202012Validator(
            geometry_decision_schema(list(catalog))).is_valid(decision):
        return None
    cid = decision.get('candidate_id')
    observed = story.get('_identity_observed_candidates') or []
    candidate = next((item for item in [*observed, *candidates]
        if item.get('candidate_id') == cid), {})
    receipt = source_map_receipt or {}
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
    proof = {'validated': True, 'policy': POLICY, 'candidate_id': cid, **scope,
        'source_photo_sha256': scope['photo_sha256'], 'map_image_sha256': receipt['map_image_sha256'],
        'original_source_sha256': receipt['original_source_sha256'],
        'model_source_sha256': receipt['model_source_sha256'],
        'scene_context_sha256': _digest({'map': story.get('_identity_map_snapshot') or
            json.loads(story.get('research_json') or '{}').get('osm') or {}, 'camera': scene_camera_context(story)}),
        'decision': decision, 'observed_features': features, 'source_map_receipt': receipt}
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
    if identity.get('proof_kind') in {'geometry', 'combined'}:
        # Stop/resume changes the dispatch epoch, not an already committed
        # object's physical identity. Pending raw verdict replay remains strict
        # in geometry_result_valid; every new fact operation has its own control
        # fence. Do not rewrite the original receipt to a later control epoch.
        resolved = identity.get('resolved_at')
        if (not isinstance(resolved, bool) and isinstance(resolved, (int, float))
                and math.isfinite(resolved)):
            control_revision = None
        return _valid_frozen(identity, photo_sha256=photo_sha256,
            generation=generation, control_revision=control_revision)
    return (identity.get('visual_reference_verified') is True
        and all(expected is None or key not in identity or identity.get(key) == expected for key, expected in (
            ('photo_sha256', photo_sha256), ('generation', generation), ('control_revision', control_revision))))


def accepted_identity(identity, photo_sha256=None, generation=None, control_revision=None):
    if not isinstance(identity, dict) or identity.get('status') not in {'match', 'owner_confirmed'}:
        return False
    if identity.get('proof_kind') in {'geometry', 'combined'}:
        return verified_physical_identity(identity, photo_sha256, generation, control_revision)
    return all(expected is None or key not in identity or identity.get(key) == expected for key, expected in (
        ('photo_sha256', photo_sha256), ('generation', generation), ('control_revision', control_revision)))
