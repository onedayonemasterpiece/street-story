"""The strongest fact about the house: the post's opening, shown to the owner above all facts.

Owner decision (18 Oct 2026): the researcher (Luna) nominates a candidate while it works, since it read
the sources; when the research run ends a lite model decides once over the whole verified inventory,
the researcher's candidate and the photo. Both opinions and reasons are kept. The owner can dispute the
choice through Mira: a fact the owner names stays until the owner changes it; a disputed fact is excluded
from the next decision. A later research run decides again, but never overrides the owner's choice; its
result is kept as a proposal Mira can mention.
"""
from __future__ import annotations

import io
import json
import logging
from typing import Any

LOG = logging.getLogger(__name__)


def _inventory(db, story_id: str) -> list[dict[str, str]]:
    return [{'fact_id': str(row[0]), 'claim_key': str(row[1] or ''), 'text': str(row[2])}
            for row in db.execute(
                "SELECT a.assertion_id,a.semantic_key,f.text FROM fact_assertions a JOIN facts f "
                "ON f.story_id=a.story_id AND f.fact_id=a.assertion_id "
                "WHERE a.story_id=? AND a.eligibility='eligible' ORDER BY f.rowid", (story_id,))
            if str(row[2] or '').strip()]


def current(research: dict[str, Any], eligible_ids: set[str]) -> dict[str, Any] | None:
    record = research.get('strongest_fact')
    return record if isinstance(record, dict) and record.get('fact_id') in eligible_ids else None


def photo_snapshot(service, story_id: str) -> bytes | None:
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(service._source_photo_bytes(story_id))) as opened:
            image = ImageOps.exif_transpose(opened).convert('RGB')
        image.thumbnail((768, 768))
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=76)
        return buffer.getvalue()
    except Exception as exc:  # the decision still runs on text alone
        LOG.warning('street_story_strongest_fact photo=unavailable error_type=%s', type(exc).__name__)
        return None


async def decide(service, story_id: str, *, luna_candidate: dict[str, Any] | None = None,
                 run_id: str | None = None, exclude: set[str] | None = None) -> dict[str, Any] | None:
    """One lite decision over the whole verified inventory; returns the stored record."""
    from .gemini import GeminiUnavailable
    from .service import canonical
    ranker = getattr(service.providers.gemini, 'strongest_hook', None)
    with service.store.connection() as db:
        row = service._story_row(db, story_id)
        research = json.loads(row['research_json'] or '{}')
        inventory = [item for item in _inventory(db, story_id) if item['fact_id'] not in (exclude or set())]
    if not callable(ranker) or not inventory:
        return None
    luna = None
    if isinstance(luna_candidate, dict):
        match = next((item for item in inventory if item['claim_key'] == luna_candidate.get('claim_key')), None)
        if match is not None:
            luna = {'fact_id': match['fact_id'], 'reason': str(luna_candidate.get('reason') or '')[:400]}
    luna_index = next((i for i, item in enumerate(inventory) if luna and item['fact_id'] == luna['fact_id']), None)
    try:
        ranked = await ranker(facts=[item['text'] for item in inventory], photo_jpeg=photo_snapshot(service, story_id),
                              researcher_choice={'index': luna_index, 'reason': luna['reason']} if luna else None)
    except GeminiUnavailable:
        LOG.warning('street_story_strongest_fact %s', canonical({'story_id': story_id, 'outcome': 'lite_unavailable'}))
        if luna is None:
            return None
        ranked = {'ranked': [{'index': luna_index, 'reason': 'lite model unavailable; researcher candidate kept'}],
                  'model': None}
    valid = [row for row in ranked.get('ranked') or [] if isinstance(row.get('index'), int) and 0 <= row['index'] < len(inventory)]
    if not valid:
        return None
    top = valid[0]
    record = {'fact_id': inventory[top['index']]['fact_id'], 'reason': str(top.get('reason') or '')[:400],
              'source': 'auto', 'model': ranked.get('model'), 'researcher': luna,
              'agrees_with_researcher': bool(luna) and luna['fact_id'] == inventory[top['index']]['fact_id'],
              'run_id': run_id, 'decided_at': service.store.now()}
    if exclude:
        record['disputed'] = sorted(exclude)
    with service.store.tx() as db:
        row = service._story_row(db, story_id)
        research = json.loads(row['research_json'] or '{}')
        eligible = {item['fact_id'] for item in _inventory(db, story_id)}
        held = current(research, eligible)
        if held and held.get('source') == 'owner':
            research['strongest_proposal'] = record
        else:
            research['strongest_fact'] = record
        db.execute('UPDATE stories SET research_json=?,revision=revision+1,updated_at=? WHERE id=?',
                   (canonical(research), service.store.now(), story_id))
    LOG.info('street_story_strongest_fact %s', canonical({
        'story_id': story_id, 'outcome': 'decided', 'fact_id': record['fact_id'], 'model': record['model'],
        'researcher_fact_id': (luna or {}).get('fact_id'), 'agrees': record['agrees_with_researcher'],
        'owner_held': bool(held and held.get('source') == 'owner'), 'run_id': run_id}))
    return research.get('strongest_fact')


def choose(service, story_id: str, fact_id: str, reason: str) -> dict[str, Any]:
    """The owner names the strongest fact; it stays until the owner changes it."""
    from .service import ConflictError, canonical
    with service.store.tx() as db:
        if fact_id not in {item['fact_id'] for item in _inventory(db, story_id)}:
            raise ConflictError('strongest_fact_not_eligible', 'Only a verified fact about this house can be the strongest.')
        row = service._story_row(db, story_id)
        research = json.loads(row['research_json'] or '{}')
        previous = research.get('strongest_fact') if isinstance(research.get('strongest_fact'), dict) else {}
        research['strongest_fact'] = {'fact_id': fact_id, 'reason': reason[:400], 'source': 'owner',
                                      'replaced': previous.get('fact_id'), 'decided_at': service.store.now()}
        research.pop('strongest_proposal', None)
        db.execute('UPDATE stories SET research_json=?,revision=revision+1,updated_at=? WHERE id=?',
                   (canonical(research), service.store.now(), story_id))
    LOG.info('street_story_strongest_fact %s', canonical({'story_id': story_id, 'outcome': 'owner_chose', 'fact_id': fact_id}))
    return research['strongest_fact']


async def dispute(service, story_id: str, reason: str) -> dict[str, Any] | None:
    """The owner disagrees without naming another fact: decide again without the disputed ones."""
    with service.store.connection() as db:
        research = json.loads(service._story_row(db, story_id)['research_json'] or '{}')
    held = research.get('strongest_fact') if isinstance(research.get('strongest_fact'), dict) else {}
    disputed = set(held.get('disputed') or []) | ({held['fact_id']} if held.get('fact_id') else set())
    if held.get('source') == 'owner':
        with service.store.tx() as db:  # the owner's own earlier choice is withdrawn by the owner
            row = service._story_row(db, story_id)
            research = json.loads(row['research_json'] or '{}')
            research['strongest_fact'] = {**held, 'source': 'auto'}
            from .service import canonical
            db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story_id))
    record = await decide(service, story_id, exclude=disputed)
    LOG.info('street_story_strongest_fact outcome=disputed story_id=%s reason_chars=%s', story_id, len(reason))
    return record
