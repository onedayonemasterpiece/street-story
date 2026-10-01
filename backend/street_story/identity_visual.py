"""Nearest-first bounded visual batches; geographic proximity is never proof."""
from __future__ import annotations
import asyncio
import time
from .identity_lifecycle import confidence, distance, visual_match
from .identity_telemetry import record_identity_event


async def identify_nearest(service, story, transcript, candidates):
    ordered = sorted(candidates, key=lambda item: (distance(item), str(item.get('candidate_id'))))[:16]
    best = {'status': 'uncertain', 'candidate_id': '', 'confidence': 0.0,
            'observations': ['Нет достаточных визуальных свидетельств.'], 'alternative_candidate_ids': []}
    remaining_refs = 6
    started = time.monotonic()
    offset = 0
    for number, count in enumerate((4, 6, 6), 1):
        batch = ordered[offset:offset + count]
        offset += count
        if not batch:
            break
        record_identity_event(service, story['id'], 'identity_batch_started', {
            'batch': number, 'candidate_ids': [item['candidate_id'] for item in batch], 'reference_budget': remaining_refs})
        remaining_time = 60 - (time.monotonic() - started)
        if remaining_time <= 0:
            break
        result = await asyncio.wait_for(service._identify_photo_batch(
            story, transcript, batch, reference_limit=min(2, remaining_refs)), timeout=remaining_time)
        remaining_refs -= min(remaining_refs, len(result.get('_references_sent', [])))
        accepted = visual_match(result, batch)
        record_identity_event(service, story['id'], 'identity_batch_finished', {
            'batch': number, 'candidate_id': result.get('candidate_id'), 'status': result.get('status'),
            'confidence': confidence(result), 'reference_ids_sent': result.get('_references_sent', []),
            'early_exit': accepted, 'duration_ms': round((time.monotonic() - started) * 1000)})
        if accepted:
            return result
        if result.get('candidate_id') in {x['candidate_id'] for x in batch} and confidence(result) >= confidence(best):
            best = {**result, 'status': 'mismatch' if result.get('status') == 'mismatch' else 'uncertain'}
    return best
