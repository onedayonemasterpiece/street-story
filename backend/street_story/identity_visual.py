"""Nearest-first bounded visual batches; geographic proximity is never proof."""
from __future__ import annotations
import asyncio
import time
from .identity_lifecycle import confidence, distance, visual_match
from .identity_telemetry import record_identity_event
from .camera_hints import read_camera_hints
from pathlib import Path


async def identify_nearest(service, story, transcript, candidates):
    story = dict(story)
    if '_camera_hints' not in story:
        story['_camera_hints'] = read_camera_hints(Path(story['photo_path'])) if story.get('photo_path') else {}
    def trace(event, fields):
        record_identity_event(service, story['id'], event, {**fields, 'generation': story.get('_identity_generation', 0)})
    ordered = sorted(candidates, key=lambda item: (distance(item), str(item.get('candidate_id'))))[:16]
    best = {'status': 'uncertain', 'candidate_id': '', 'confidence': 0.0,
            'observations': ['Нет достаточных визуальных свидетельств.'], 'alternative_candidate_ids': []}
    remaining_refs = 6
    started = time.monotonic()
    offset = 0

    async def evaluate(batch, budget):
        remaining_time = 60 - (time.monotonic() - started)
        if remaining_time <= 0:
            raise TimeoutError('Identity visual deadline')
        return await asyncio.wait_for(service._identify_photo_batch(
            story, transcript, batch, reference_limit=budget), timeout=remaining_time)

    for number, count in enumerate((4, 6, 6), 1):
        batch = ordered[offset:offset + count]
        offset += count
        if not batch:
            break
        trace( 'identity_batch_started', {
            'batch': number, 'candidate_ids': [item['candidate_id'] for item in batch], 'reference_budget': remaining_refs})
        if time.monotonic() - started >= 60:
            break
        result = await evaluate(batch, min(2, remaining_refs))
        if result.get('_references_rate_limited') and not visual_match(result, batch):
            trace( 'identity_batch_finished', {
                'batch': number, 'batch_candidate_count': len(batch), 'candidate_id': result.get('candidate_id'),
                'status': 'uncertain', 'reference_ids_sent': result.get('_references_sent', []),
                'early_exit': False, 'reason': 'reference_host_rate_limited',
                'duration_ms': round((time.monotonic() - started) * 1000)})
            if result.get('candidate_id') in {x['candidate_id'] for x in batch}:
                return {**result, 'status': 'uncertain'}
            return best
        remaining_refs -= min(remaining_refs, len(result.get('_references_sent', [])))
        chosen = next((x for x in batch if x['candidate_id'] == result.get('candidate_id')), None)
        if (chosen and chosen['candidate_id'] in result.get('_references_unavailable_ids', [])
                and result.get('status') == 'match' and confidence(result) >= .90):
            trace('identity_batch_finished', {
                'batch': number, 'batch_candidate_count': len(batch), 'candidate_id': chosen['candidate_id'],
                'status': 'uncertain', 'early_exit': False, 'reason': 'selected_reference_unavailable',
                'duration_ms': round((time.monotonic() - started) * 1000)})
            return {**result, 'status': 'uncertain'}
        # A promising candidate must not lose solely because its image was not
        # among the first two references. Verify just that candidate before
        # moving farther away; share the same six-image / sixty-second budget.
        if (chosen and chosen.get('reference_image_urls') and remaining_refs > 0
                and chosen['candidate_id'] not in result.get('_references_sent', [])
                and confidence(result) >= .60 and result.get('status') in {'match', 'uncertain'}):
            trace( 'identity_targeted_reference', {
                'batch': number, 'candidate_id': chosen['candidate_id']})
            targeted = await evaluate([chosen], 1)
            remaining_refs -= min(remaining_refs, len(targeted.get('_references_sent', [])))
            # A failed reference verification cannot preserve an earlier claim
            # that the same object was already confirmed.
            result = targeted
        accepted = visual_match(result, batch)
        trace( 'identity_batch_finished', {
            'batch': number, 'batch_candidate_count': len(batch), 'candidate_id': result.get('candidate_id'), 'status': result.get('status'),
            'confidence': confidence(result), 'reference_ids_sent': result.get('_references_sent', []),
            'early_exit': accepted, 'duration_ms': round((time.monotonic() - started) * 1000)})
        if accepted:
            return result
        if result.get('candidate_id') in {x['candidate_id'] for x in batch} and confidence(result) >= confidence(best):
            best = {**result, 'status': 'mismatch' if result.get('status') == 'mismatch' else 'uncertain'}
    return best
