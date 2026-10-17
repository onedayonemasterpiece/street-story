package com.onedayonemasterpiece.streetstory

import com.google.gson.GsonBuilder

/**
 * Place commands fence on an explicitly stated expected candidate, including
 * "none yet". Plain Gson drops null map values, which the server rejects as
 * place_selection_stale, so these payloads keep JSON nulls.
 */
object PlaceCommandJson {
    private val gson = GsonBuilder().serializeNulls().create()

    fun encode(payload: Map<String, Any?>): String = gson.toJson(payload)
}
