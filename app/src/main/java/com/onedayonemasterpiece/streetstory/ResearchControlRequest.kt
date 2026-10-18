package com.onedayonemasterpiece.streetstory

/** Exact current photo/object scope; one outbox request key survives lost replies. */
internal fun researchControlPayload(action: String, purpose: String, photoSha256: String, generation: Int): Map<String, Any> {
    require(action in setOf("stop", "resume"))
    require(purpose in setOf("identity", "facts", "all"))
    require(photoSha256.matches(Regex("[0-9a-f]{64}")) && generation >= 0)
    return mapOf("action" to action, "purpose" to purpose,
        "expected_photo_sha256" to photoSha256, "expected_identity_generation" to generation)
}
