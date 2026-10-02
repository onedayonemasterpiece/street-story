from street_story.identity_candidate_policy import candidate_identity_eligible, wikipedia_identity_eligible
from street_story.identity_lifecycle import visual_match


def test_locality_articles_are_search_context_not_final_object_identity():
    assert not wikipedia_identity_eligible(
        "Багратионовск",
        "Багратионовск — город в Калининградской области России.",
    )
    assert not wikipedia_identity_eligible(
        "Московский район (Калининград)",
        "Московский район — административный район города Калининграда.",
    )
    assert wikipedia_identity_eligible(
        "Кирха Святого Семейства (Калининград)",
        "Кирха Святого Семейства — католический храм, ныне концертный зал.",
    )


def test_visual_gate_rejects_context_container_even_with_model_confidence():
    candidates = [{
        "candidate_id": "wiki:city",
        "name": "Багратионовск",
        "identity_eligible": False,
        "reference_image_urls": ["https://upload.wikimedia.org/city.jpg"],
    }]
    result = {
        "status": "match",
        "candidate_id": "wiki:city",
        "confidence": 1.0,
        "observations": ["Совпадает изображение на странице города"],
        "alternative_candidate_ids": [],
        "_references_sent": ["wiki:city"],
    }
    assert not visual_match(result, candidates)
    assert not candidate_identity_eligible(candidates[0])
