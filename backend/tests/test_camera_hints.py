from __future__ import annotations

import hashlib
import io
import json

from PIL import Image
from PIL.TiffImagePlugin import IFDRational
import pytest

from street_story.camera_hints import (annotate_camera_alignment, read_camera_hints,
    reference_order, metadata_summary, model_camera_hints)
from street_story.identity_visual import identify_nearest
from test_identity_lifecycle import create, make_service
from test_identity_recovery_policy import create_photo, match


def jpeg(*, direction=None, ref='T', redacted=False, gps=True, error=None, orientation=6, track=None):
    image = Image.new('RGB', (16, 12), (80, 100, 130))
    exif = Image.Exif()
    exif[274] = orientation
    exif[34665] = {37386: IFDRational(9), 41989: 72, 41988: IFDRational(3)}
    if gps:
        tags = {1: '\x00' if redacted else 'N', 2: (IFDRational(54), IFDRational(42), IFDRational(6)),
                3: '\x00' if redacted else 'E', 4: (IFDRational(20), IFDRational(30), IFDRational(9))}
        if direction is not None:
            tags[17] = IFDRational(direction)
            if ref is not None:
                tags[16] = ref
        if error is not None:
            tags[31] = IFDRational(error)
        if track is not None:
            tags[15] = IFDRational(track)
        exif[34853] = tags
    output = io.BytesIO()
    image.save(output, format='JPEG', exif=exif)
    return output.getvalue()


def test_metadata_less_attachment_does_not_invent_any_hints():
    output = io.BytesIO()
    Image.new('RGB', (8, 9)).save(output, format='JPEG')
    hints = read_camera_hints(output.getvalue())
    assert hints['exif_present'] is False
    assert hints['direction_status'] == 'missing'
    assert model_camera_hints(hints) == {}
    assert read_camera_hints(b'not an image')['read_error']


def test_observed_lens_fields_do_not_multiply_zoom_or_claim_direction():
    hints = read_camera_hints(jpeg())
    assert hints['direction_status'] == 'missing'
    assert hints['focal_length_mm'] == 9
    assert hints['focal_length_35mm'] == 72
    assert hints['digital_zoom_ratio'] == 3
    assert hints['diagonal_fov_35mm_deg'] == pytest.approx(33.4, abs=.2)
    assert hints['pixel_orientation'] == 6
    assert 'direction_degrees' not in hints


@pytest.mark.parametrize('direction,ref,status', [
    (0, 'T', 'true_north'), (359.9, 'T', 'true_north'), (90, 'M', 'magnetic_uncorrected'),
    (90, None, 'invalid_or_unspecified_reference'), (90, '\x00', 'invalid_or_unspecified_reference'),
    (360, 'T', 'invalid_or_unspecified_reference'), (IFDRational(0, 0), 'T', 'invalid_or_unspecified_reference'),
])
def test_direction_ref_and_invalid_rationals(direction, ref, status):
    assert read_camera_hints(jpeg(direction=direction, ref=ref))['direction_status'] == status


def test_orientation_and_gps_track_are_never_camera_compass():
    hints = read_camera_hints(jpeg(track=90, orientation=8))
    assert hints['direction_status'] == 'missing'
    assert hints['gps_track_present'] is True
    assert 'direction_degrees' not in hints
    north = read_camera_hints(jpeg(direction=0, orientation=8))
    assert north['direction_degrees'] == 0
    summary = metadata_summary(north)
    assert 'direction_degrees' not in summary and 'latitude' not in summary
    assert summary['direction_status'] == 'true_north'


def geometry():
    candidates = [
        {'candidate_id': 'osm:way:1', 'name': 'Behind', 'distance_m': 110},
        {'candidate_id': 'wiki:2', 'name': 'Ahead', 'distance_m': 111},
        {'candidate_id': 'wiki:3', 'name': 'Unknown position', 'distance_m': 112},
    ]
    osm = {'nearby': [{'type': 'way', 'id': 1, 'center': {'lat': 54.699, 'lon': 20.5}}]}
    wiki = [{'pageid': 2, 'lat': 54.701, 'lon': 20.5}, {'pageid': 3}]
    return candidates, osm, wiki


def test_bearing_wrap_and_soft_reference_order_preserve_every_candidate():
    candidates, osm, wiki = geometry()
    annotated = annotate_camera_alignment(candidates, osm, wiki, 54.7, 20.5,
                                          read_camera_hints(jpeg(direction=359)), position_verified=True)
    assert [x['candidate_id'] for x in annotated] == [x['candidate_id'] for x in candidates]
    assert annotated[1]['camera_direction_difference_deg'] == pytest.approx(1)
    assert annotated[1]['camera_alignment'] == 'ahead'
    assert annotated[0]['camera_alignment'] == 'off_axis'
    assert 'camera_alignment' not in annotated[2]
    assert [x['name'] for x in reference_order(annotated)] == ['Ahead', 'Unknown position', 'Behind']
    assert 'camera_alignment' not in candidates[0]  # No mutation of cached objects.


@pytest.mark.parametrize('hints,verified', [
    ({}, True), (read_camera_hints(jpeg(direction=0, ref='M')), True),
    (read_camera_hints(jpeg(direction=0)), False),
    (read_camera_hints(jpeg(direction=0, error=100)), True),
    ({**read_camera_hints(jpeg(direction=0)), 'map_datum_wgs84': False}, True),
])
def test_missing_uncertain_position_and_magnetic_hints_preserve_behavior(hints, verified):
    candidates, osm, wiki = geometry()
    assert annotate_camera_alignment(candidates, osm, wiki, 54.7, 20.5, hints,
                                     position_verified=verified) == candidates
    assert reference_order(candidates) == candidates


def test_bearing_at_camera_position_is_unknown_not_a_guessed_direction():
    candidates, osm, wiki = geometry()
    wiki[0]['lat'] = 54.7
    annotated = annotate_camera_alignment(candidates, osm, wiki, 54.7, 20.5,
        read_camera_hints(jpeg(direction=0)), position_verified=True)
    assert 'camera_alignment' not in annotated[1]


@pytest.mark.asyncio
async def test_direction_never_promotes_far_batch_or_changes_early_exit_proof(tmp_path):
    service, _ = make_service(tmp_path)
    story = create(service)
    candidates = [{'candidate_id': f'c{i}', 'distance_m': i * 30,
                   'camera_alignment': 'ahead' if i == 5 else 'off_axis'} for i in range(1, 17)]
    batches = []
    async def compare(_story, _text, batch, reference_limit):
        batches.append([x['candidate_id'] for x in batch])
        return match('c1')
    service._identify_photo_batch = compare
    result = await identify_nearest(service, {'id': story['id']}, '', candidates)
    assert result['candidate_id'] == 'c1'
    assert batches == [['c1', 'c2', 'c3', 'c4']]


@pytest.mark.asyncio
async def test_recovered_original_hints_persist_with_photo_without_repeated_identification(tmp_path):
    service, gemini = make_service(tmp_path)
    old = jpeg(redacted=True)  # Server keeps this immutable redacted source.
    story = create_photo(service, old, client='recovered-direction')
    service.recover_photo_location(story['id'], hashlib.sha256(old).hexdigest(), jpeg(direction=90))
    await service.run_once()
    with service.store.connection() as db:
        prior = json.loads(db.execute('SELECT research_json FROM stories WHERE id=?', (story['id'],)).fetchone()[0])
    binding = prior['photo_camera_hints']
    assert binding['photo_sha256'] == hashlib.sha256(old).hexdigest()
    assert binding['source'] == 'selected_original_exif'
    assert binding['metadata']['direction_degrees'] == 90
    count = len(gemini.identity_calls)
    await service.resolve_identity(story['id'])
    assert len(gemini.identity_calls) == count
