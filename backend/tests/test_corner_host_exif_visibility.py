"""Real original photo102: strict host must not accept OSM back-facing wall."""
from street_story.identity_geometry_contract import CONTRACT, measured_correspondence
from street_story.identity_scene import render_scene
from street_story.identity_camera_visibility import observed_corner_halfplane
from test_original_osm_geometry_regression import story_for


def test_original_exif_camera_rejects_real_joined_but_back_facing_corner():
    story = story_for(102)
    scene = render_scene(story, [])
    manifest = scene['manifest']
    subject = 'osm:way:133035113'
    receipt = {'manifest': manifest, 'geometry_contract': CONTRACT,
        'physical_body_candidate_ids': [r[1] for r in manifest['objects']['rows']],
        'material_alternative_candidate_ids': []}

    def measured(indices):
        first, second = indices
        correspondence = {
            'pattern_kind': 'corner', 'candidate_ids': [subject],
            'source_pattern': 'Two visible joined facade walls intersect at a physical corner.',
            'pitch_basis': 'Upward camera tilt possible; not a compass estimate.',
            'coverage_basis': 'All received physical bodies retained.',
            'front_segments': [{
                'first': {'kind':'segment','candidate_id':subject,
                          'ring_index':0,'segment_index':first},
                'second': {'kind':'segment','candidate_id':subject,
                           'ring_index':0,'segment_index':second}}],
            'street_axis': None,
            'pose': {'east_m':0.,'north_m':0.,'heading_degrees':300.},
            'uncertainty_scenarios': [
                {'pose':{'east_m':2.,'north_m':0.,'heading_degrees':300.},
                 'assumption':'Fixed two metre trial shift, not measured accuracy.',
                 'source_pattern_preserved': True}],
        }
        decision = {'candidate_id': subject, 'rejected_alternatives': [],
                    'spatial_correspondence': correspondence}
        return measured_correspondence(story, [], decision, receipt)

    wrong = observed_corner_halfplane(story,manifest,subject,0,0,5)
    right = observed_corner_halfplane(story,manifest,subject,0,1,2)
    assert wrong['status'] == 'corner_not_nominally_visible'
    assert right['status'] == 'both_walls_nominally_exterior'
    assert measured((0,5)) is None
    # This validates only the 2D numeric *necessary* predicate, not that
    # the actual SOURCE depicts this alternative wall corner.
    assert measured((1,2)) is not None
    # Camera-facing joined edges can still be nearly collinear rather than
    # a discriminating visible building corner.
    assert measured((2,3)) is None


def test_owner_approximation_does_not_claim_precise_behind_wall_certificate():
    story=story_for(102)
    story['_camera_position_verified']=False
    story['_location_provenance']={'kind':'owner_approx_camera'}
    manifest=render_scene(story,[])['manifest']
    assert manifest['camera']['position_status']=='owner_approximate'
    diagnostic=observed_corner_halfplane(story,manifest,'osm:way:133035113',0,0,5)
    assert diagnostic['status']=='corner_not_nominally_visible'
    # The measured host check only takes this strict numeric rear-wall veto
    # when the EXIF camera anchor is verified, not a loose owner hint.