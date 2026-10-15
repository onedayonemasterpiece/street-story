package com.onedayonemasterpiece.streetstory

import com.google.gson.Gson
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonParseException
import com.google.gson.TypeAdapter
import com.google.gson.stream.JsonReader
import com.google.gson.stream.JsonWriter

/** Gson's JsonObject adapter rejects JsonNull even on nullable Kotlin fields. */
class NullableJsonObjectAdapter : TypeAdapter<JsonObject>() {
    private val elements = Gson().getAdapter(JsonElement::class.java)

    override fun read(input: JsonReader): JsonObject? {
        val path = input.path
        val value = elements.read(input)
        return when {
            value == null || value.isJsonNull -> null
            value.isJsonObject -> value.asJsonObject
            else -> throw JsonParseException("Expected JSON object or null at $path")
        }
    }

    override fun write(output: JsonWriter, value: JsonObject?) = elements.write(output, value)
}
