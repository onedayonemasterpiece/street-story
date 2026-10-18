package com.onedayonemasterpiece.streetstory

import android.content.Context

object AppGraph {
    @Volatile private var storeRef: StoryStore? = null
    @Volatile private var configRef: ConfigStore? = null
    @Volatile private var liveRef: LiveSessionController? = null

    fun store(context: Context): StoryStore =
        storeRef ?: synchronized(this) { storeRef ?: StoryStore(context.applicationContext).also { storeRef = it } }

    fun config(context: Context): ConfigStore =
        configRef ?: synchronized(this) { configRef ?: ConfigStore(context.applicationContext).also { configRef = it } }

    fun live(context: Context): LiveSessionController =
        liveRef ?: synchronized(this) { liveRef ?: LiveSessionController(context.applicationContext).also { liveRef = it } }
}
