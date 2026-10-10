"""Story-local research leads for conditional dialogue, never canonical facts."""
from __future__ import annotations

import base64
import hashlib

from .identity_proof import accepted_identity


def identity_research_context(service, row, research):
    generation = int(research.get('identity_generation') or 0)
    revision = int(((research.get('research_controls') or {}).get('identity') or {}).get('revision') or 0)
    identity = research.get('visual_identity') or {}
    history = research.get('identity_article_discovery') or {}
    plan = history.get('search_plan') or {}
    current = (plan.get('photo_sha256') == row['photo_sha256']
        and plan.get('generation') == generation and plan.get('control_revision') == revision)
    payload = (plan.get('payload') or {}) if current else {}
    image_receipt = payload.get('source_map_receipt') or {}
    joint_visual_input = (current and image_receipt.get('joint_image_input') is True
        and image_receipt.get('source_photo_sha256') == row['photo_sha256']
        and isinstance(image_receipt.get('map_image_sha256'), str)
        and len(image_receipt['map_image_sha256']) == 64)
    identity_current = (identity.get('photo_sha256') == row['photo_sha256'] and identity.get('generation') == generation)
    if identity_current and accepted_identity(identity, photo_sha256=row['photo_sha256'],
            generation=generation, control_revision=revision):
        return None
    candidates = {str(item.get('candidate_id')): item for item in
        [*(identity.get('observed_candidates') or []), *(identity.get('candidates') or [])]
        if identity_current and isinstance(item, dict) and item.get('candidate_id')}
    hypotheses = []
    for item in (payload.get('spatial_hypotheses') or [])[:3]:
        if not isinstance(item, dict):
            continue
        cid = str(item.get('candidate_id') or '')
        if cid not in candidates or item.get('support_status') not in {'spatially_supported', 'plausible', 'contradicted', 'unknown'}:
            continue
        hypotheses.append({'candidate_id': cid, 'support_status': item['support_status'],
            **{key: [str(value)[:300] for value in (item.get(key) or [])[:3]] for key in
                ('basis', 'counterevidence', 'assumptions')},
            'next_action': str(item.get('next_action') or '')[:300],
            'name': str(candidates[cid].get('name') or '')[:160],
            'joint_visual_input_verified': joint_visual_input})
    sources = []
    history_current = (history.get('photo_sha256') == row['photo_sha256'] and history.get('generation') == generation
        and current)
    if hypotheses and history_current:
        # Only already acquired public pages from this story's frozen discovery.
        # A page is not proof that its subject is the SOURCE physical object.
        from .providers import _read_article_text
        from bs4 import BeautifulSoup, UnicodeDammit
        for source in (history.get('sources') or [])[:12]:
            if not isinstance(source, dict):
                continue
            url = str(source.get('url') or '')
            saved = service.store.cache_get('public-article-acquisition-v1:' + hashlib.sha256(url.encode()).hexdigest())
            if not saved or saved.get('mime') not in {'text/html', 'application/xhtml+xml', 'text/plain'}:
                continue
            try:
                body = base64.b64decode(saved['body'], validate=True)
                if hashlib.sha256(body).hexdigest() != saved.get('sha256'):
                    continue
                if saved['mime'] == 'text/plain':
                    raw_text = UnicodeDammit(body).unicode_markup or ''
                    text, truncated = raw_text[:2000], len(raw_text) > 2000
                else:
                    text, truncated = _read_article_text(str(BeautifulSoup(body, 'html.parser')), limit=2000)
            except (KeyError, TypeError, ValueError):
                continue
            if not text.strip():
                continue
            sources.append({'url': url, 'final_url': saved.get('final_url'), 'title': str(source.get('title') or '')[:160],
                'article_sha256': saved['sha256'], 'text': text, 'text_truncated': truncated,
                'source_identity_verified': False, 'canonical_eligible': False,
                **{key: source[key] for key in ('candidate_id', 'candidate_ids', 'physical_subject_candidate_ids') if key in source}})
            if len(sources) == 2:
                break
    missing_location = row['latitude'] is None or row['longitude'] is None
    if not hypotheses and not missing_location:
        return None
    saved_question = research.get('identity_clarification') or {}
    question_current = (saved_question.get('photo_sha256') == row['photo_sha256']
        and saved_question.get('generation') == generation and saved_question.get('control_revision') == revision)
    clarification = ({'reason': saved_question.get('reason'), 'question': str(saved_question.get('question') or '')[:300]}
        if question_current and saved_question.get('question') and not saved_question.get('answered') else
        {'reason': 'geographic_context_missing', 'question': 'В каком городе или месте сделан снимок?'}
        if missing_location and not hypotheses and not research.get('identity_owner_hint')
            and not str(research.get('transcript') or '').strip() else None)
    return {'photo_sha256': row['photo_sha256'], 'generation': generation, 'control_revision': revision,
        'hypotheses': hypotheses, 'article_sources': sources, 'canonical_eligible': False,
        'joint_visual_input_verified': joint_visual_input,
        'owner_context': {'text': str(research.get('transcript') or '')[-1500:], 'provenance': 'owner_conversation',
            'independently_verified': False},
        'clarification': clarification,
        'instruction': 'These are story-only hypotheses and acquired source text, not verified facts about SOURCE. '
            'Only explicit spatially_supported WITH joint_visual_input_verified=true permits a probable spatial identification; '
            'a text-only plan is a hypothesis without SOURCE+map visual support, never visual MATCH. '
            'You may explain useful source claims conditionally (if this is that building, the cited article says...), '
            'preserving article subject, dates, uncertainty and contradictions. Do not save/select/publish them or put them in POI memory. '
            'Ask one short distinguishing question only if missing context or ambiguity changes the publication subject; '
            'do not require the owner to identify every uploaded object.'}
