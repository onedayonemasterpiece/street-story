"""Owner photo-format preference; changing it never starts generation/research."""
from __future__ import annotations
import json

from .service import ConflictError, canonical, digest


def choice(research):
    return research.get('visual_photo_format') or {'mode': 'original', 'revision': 0, 'crop_rect': None}


def projection(research, visual, asset_ref):
    selected = choice(research)
    proof = visual.get('output_frame_evidence') or {}
    current = (selected['revision'] == 0 or
        (visual.get('photo_format_revision') == selected['revision']
         and not visual.get('stale', False) and proof.get('state') == 'verified'
         and bool(asset_ref) and proof.get('asset_ref') == asset_ref))
    return {**selected, 'current_visual': current}


def set_photo_format(service, story_id, key, body):
    mode = body.get('mode')
    crop = body.get('crop_rect')
    if (mode not in {'original', 'crop_4_5'} or mode == 'original' and crop is not None
            or crop is not None and (not isinstance(crop, list) or len(crop) != 4
                or any(type(v) is not int or v < 0 for v in crop) or crop[2] <= 0 or crop[3] <= 0)):
        raise ConflictError('photo_format_invalid', 'Выберите оригинал или настоящую обрезку 4:5.')
    with service.store.tx() as db:
        row = service._story_row(db, story_id)
        if service._idem(db, key, 'photo-format', digest(body), 'story', story_id):
            return service._story_repr(db, row)
        research = json.loads(row['research_json'] or '{}')
        if (body.get('expected_photo_sha256') != row['photo_sha256']
                or body.get('expected_identity_generation') != int(research.get('identity_generation') or 0)):
            raise ConflictError('photo_format_source_changed', 'Исходное фото или выбранный дом изменились.')
        from .visual_source import photo_transform
        # Validate the actual pixel window now; no network or provider work.
        cover = db.execute("SELECT photo_id,mime_type FROM story_photos WHERE story_id=? AND position=0",
                           (story_id,)).fetchone() if hasattr(service, '_comic_photo_bytes') else None
        cover_id = cover['photo_id'] if cover else 'source'
        pixels, mime = (service._comic_photo_bytes(story_id, cover_id) if cover
                        else (service._source_photo_for_job(story_id), row['photo_mime_type']))
        _raw, _mime, transform = photo_transform(pixels, mime, mode, crop)
        previous = choice(research)
        research['visual_photo_format'] = {'mode': mode, 'crop_rect': transform['crop_rect'] if mode == 'crop_4_5' else None,
            'revision': previous['revision']+1, 'source_photo_sha256': row['photo_sha256'],
            'photo_id': cover_id,
            'oriented_size': transform['oriented_size']}
        db.execute('UPDATE stories SET research_json=?,revision=revision+1,updated_at=? WHERE id=?',
            (canonical(research), service.store.now(), story_id))
        return service._story_repr(db, service._story_row(db, story_id))
