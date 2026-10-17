package com.onedayonemasterpiece.streetstory

import android.app.Activity
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.MotionEvent
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.Toast
import com.google.gson.JsonObject
import com.google.gson.annotations.SerializedName
import org.maplibre.android.MapLibre
import org.maplibre.android.camera.CameraPosition
import org.maplibre.android.geometry.LatLng
import org.maplibre.android.maps.MapLibreMap
import org.maplibre.android.maps.MapView
import org.maplibre.android.maps.Style
import org.maplibre.android.style.layers.CircleLayer
import org.maplibre.android.style.layers.FillLayer
import org.maplibre.android.style.layers.LineLayer
import org.maplibre.android.style.layers.PropertyFactory.*
import org.maplibre.android.style.sources.GeoJsonSource
import org.maplibre.geojson.Feature
import org.maplibre.geojson.FeatureCollection
import org.maplibre.geojson.Point
import java.util.UUID

class PlaceCameraWire {
    var latitude: Double? = null
    var longitude: Double? = null
    var origin: String = ""
    var zoom: Double? = null
}
class PlaceMapFocusWire {
    var lat: Double? = null
    var lon: Double? = null
    var zoom: Double = 17.0
    var label: String = ""
    var revision: Int = 0
    @SerializedName("area_id") var areaId: String = ""
    fun valid(): Boolean = lat?.let { it.isFinite() && it in -90.0..90.0 } == true &&
        lon?.let { it.isFinite() && it in -180.0..180.0 } == true && zoom.isFinite() && zoom in 1.0..20.0 && revision >= 0
}

class PlaceMapWire {
    @SerializedName("snapshot_id") var snapshotId: String = ""
    @SerializedName("identity_generation") var identityGeneration: Int = 0
    @SerializedName("photo_sha256") var photoSha256: String = ""
    var revision: Int = 0
    var camera: PlaceCameraWire? = null
    var center: PlaceCameraWire? = null
    var zoom: Double? = null
    @field:com.google.gson.annotations.JsonAdapter(NullableJsonObjectAdapter::class)
    var buildings: JsonObject? = null
    @field:com.google.gson.annotations.JsonAdapter(NullableJsonObjectAdapter::class)
    var streets: JsonObject? = null
    @field:com.google.gson.annotations.JsonAdapter(NullableJsonObjectAdapter::class)
    var selected: JsonObject? = null
}

/** One native map: the owner chooses server-supplied contours by touch or voice. */
class PlaceMapPanel(
    private val activity: Activity,
    private val storyId: String,
    private val store: StoryStore,
    private val projection: ResearchProjectionStore,
    private val config: ConfigStore,
    private val onHouseElsewhere: () -> Unit = {},
    private val onPlaceUnknown: () -> Unit = {},
    private val onClarifyPlace: () -> Unit = {},
) : LinearLayout(activity) {
    private val handler = Handler(Looper.getMainLooper())
    val mapView: MapView
    private var nativeMap: MapLibreMap? = null
    private var mapData: PlaceMapWire? = null
    private var selected: Feature? = null
    private var selectionMode = "exact"
    private val selectedGroup = linkedMapOf<String, Feature>()
    private var selectionDirty = false
    private var selectionSignature: String? = null
    private var durableGroup: PlaceGroupSnapshot? = null
    private val modes = RadioGroup(activity)
    private val exactMode = RadioButton(activity)
    private val roughMode = RadioButton(activity)
    private val elsewhereButton = Button(activity)
    private val unknownButton = Button(activity)
    private var appliedFocus: String? = null
    private val automaticAction = TextView(activity)
    private val groupStatus = TextView(activity)
    private val clarification = Button(activity)
    private var requestedCenter: LatLng? = null
    private var requestedZoom: Double? = null
    private var fetching = false
    private var disposed = false
    private var restored = false
    private val status = TextView(activity)
    private val confirm = Button(activity)
    private val fetchMoved = Runnable { loadMap(nativeMap?.cameraPosition?.target) }

    init {
        orientation = VERTICAL
        contentDescription = "owner-place-map-panel"
        MapLibre.getInstance(activity)
        org.maplibre.android.module.http.HttpRequestUtil.setOkHttpClient(okhttp3.OkHttpClient.Builder()
            .addInterceptor { chain -> chain.proceed(chain.request().newBuilder()
                .header("User-Agent", "StreetStory/${BuildConfig.VERSION_NAME} (+https://github.com/onedayonemasterpiece/street-story)").build()) }.build())
        addView(TextView(activity).apply { text = "Выберите дом на карте"; textSize = 16f })
        modes.orientation = HORIZONTAL
        exactMode.id = android.view.View.generateViewId()
        roughMode.id = android.view.View.generateViewId()
        modes.addView(exactMode.apply { text = "Точно"; contentDescription = "owner-place-mode-exact" }, LayoutParams(0, -2, 1f))
        modes.addView(roughMode.apply { text = "Примерно"; contentDescription = "owner-place-mode-rough" }, LayoutParams(0, -2, 1f))
        modes.check(exactMode.id)
        modes.setOnCheckedChangeListener { _, id ->
            val mode = if (id == roughMode.id) "rough" else "exact"
            if (mode != selectionMode) {
                selectionMode = mode
                selectionDirty = true
                selectedGroup.clear()
                if (mode == "rough") selected?.let { selectedGroup[candidateId(it)] = it }
                selected = null
                updateSelection()
            }
        }
        addView(modes)
        mapView = object : MapView(activity, org.maplibre.android.maps.MapLibreMapOptions.createFromAttributes(activity).textureMode(true)) {
            override fun dispatchTouchEvent(event: MotionEvent): Boolean {
                val finished = event.actionMasked == MotionEvent.ACTION_UP || event.actionMasked == MotionEvent.ACTION_CANCEL
                // Own the sequence before native render children dispatch it.
                // Release the scroll parent only after the SDK consumes its end.
                parent?.requestDisallowInterceptTouchEvent(true)
                val handled = super.dispatchTouchEvent(event)
                if (finished) parent?.requestDisallowInterceptTouchEvent(false)
                if (event.actionMasked == MotionEvent.ACTION_DOWN || finished) {
                    android.util.Log.i("StreetStoryMap", "event=touch action=${event.actionMasked} handled=$handled attached=$isAttachedToWindow focused=${hasWindowFocus()}")
                }
                return handled
            }
        }.apply { contentDescription = "owner-place-map"; onCreate(Bundle()) }
        val available = activity.resources.displayMetrics.heightPixels / activity.resources.displayMetrics.density
        addView(mapView, LayoutParams(-1, dp((available * .25f).coerceIn(112f, 240f))))
        addView(status.apply { text = "Загружаем фото и карту…"; textSize = 13f; contentDescription = "owner-place-status" })
        addView(groupStatus.apply { textSize = 13f; contentDescription = "owner-place-group-status"; visibility = GONE })
        val buttons = LinearLayout(activity)
        confirm.text = "Это мой дом"
        confirm.contentDescription = "owner-place-confirm"
        confirm.isEnabled = false
        confirm.setOnClickListener { confirmSelection() }
        buttons.addView(confirm, LayoutParams(0, -2, 1f))
        addView(buttons)
        val navigation = LinearLayout(activity)
        navigation.addView(elsewhereButton.apply {
            text = "Дом не здесь"; contentDescription = "owner-place-elsewhere"
            setOnClickListener { onHouseElsewhere() }
        }, LayoutParams(0, -2, 1f))
        navigation.addView(unknownButton.apply {
            text = "Не знаю, где это"; contentDescription = "owner-place-unknown"
            setOnClickListener { onPlaceUnknown() }
        }, LayoutParams(0, -2, 1f))
        addView(navigation)
        addView(clarification.apply {
            text = "Уточнить с Мирой"; contentDescription = "owner-place-clarify"
            visibility = GONE; setOnClickListener { onClarifyPlace() }
        })
        addView(automaticAction.apply {
            text = "Автоматическое распознавание"; textSize = 13f
            contentDescription = "owner-place-automatic"
            setPadding(0, dp(6f), 0, dp(6f))
            setOnClickListener {
                if (!store.pendingOperations(storyId).any { it.kind == "identity" }) {
                    store.enqueueOperation(storyId, "identity", "ss-identify-${UUID.randomUUID()}", "{}")
                    SyncScheduler.enqueue(activity)
                    status.text = "Автоматическое распознавание запрошено отдельно."
                }
            }
        })
        addView(TextView(activity).apply {
            text = "© OpenStreetMap contributors · точка камеры ≠ выбранный дом"
            textSize = 10f
            setOnClickListener { activity.startActivity(android.content.Intent(android.content.Intent.ACTION_VIEW,
                android.net.Uri.parse("https://www.openstreetmap.org/copyright"))) }
        })
        mapView.getMapAsync { map ->
            if (disposed) return@getMapAsync
            nativeMap = map
            map.setPrefetchZoomDelta(0)
            map.setStyle(Style.Builder().fromJson(BASE_STYLE)) { style ->
                style.addSource(GeoJsonSource("owner-buildings", EMPTY_FEATURES))
                style.addSource(GeoJsonSource("owner-selection", EMPTY_FEATURES))
                style.addSource(GeoJsonSource("owner-camera", EMPTY_FEATURES))
                style.addSource(GeoJsonSource("owner-narrowed", EMPTY_FEATURES))
                style.addSource(GeoJsonSource("owner-house", EMPTY_FEATURES))
                style.addLayer(FillLayer("owner-buildings-fill", "owner-buildings").withProperties(fillColor("#819f89"), fillOpacity(.42f)))
                style.addLayer(LineLayer("owner-buildings-outline", "owner-buildings").withProperties(lineColor("#345747"), lineWidth(1.4f)))
                style.addLayer(FillLayer("owner-selected-fill", "owner-selection").withProperties(fillColor("#e48439"), fillOpacity(.6f)))
                style.addLayer(LineLayer("owner-selected-line", "owner-selection").withProperties(lineColor("#d85d15"), lineWidth(3f)))
                style.addLayer(FillLayer("owner-narrowed-fill", "owner-narrowed").withProperties(fillColor("#7752ac"), fillOpacity(.72f)))
                style.addLayer(LineLayer("owner-narrowed-line", "owner-narrowed").withProperties(lineColor("#543583"), lineWidth(4f)))
                style.addLayer(CircleLayer("owner-house-dot", "owner-house").withProperties(circleColor("#d85d15"), circleRadius(8f), circleStrokeColor("#ffffff"), circleStrokeWidth(2f)))
                style.addLayer(CircleLayer("owner-camera-dot", "owner-camera").withProperties(circleColor("#3569ae"), circleRadius(6f), circleStrokeColor("#ffffff"), circleStrokeWidth(2f)))
                map.addOnMapClickListener { point ->
                    val hit = map.queryRenderedFeatures(map.projection.toScreenLocation(point), "owner-buildings-fill").firstOrNull()
                    android.util.Log.i("StreetStoryMap", "event=map_click hit=${hit != null} mode=$selectionMode disposed=$disposed")
                    if (hit != null) {
                        selectionDirty = true
                        if (selectionMode == "rough") {
                            val id = candidateId(hit)
                            if (selectedGroup.containsKey(id)) selectedGroup.remove(id)
                            else if (selectedGroup.size < 16) selectedGroup[id] = hit
                            else Toast.makeText(activity, "В группе может быть до 16 домов", Toast.LENGTH_SHORT).show()
                        } else selected = hit
                        updateSelection()
                    }
                    hit != null
                }
                map.addOnCameraIdleListener {
                    val center = map.cameraPosition.target
                    if (restored && center != null && PlaceMapRefresh.needed(requestedCenter?.distanceTo(center), map.cameraPosition.zoom, requestedZoom)) {
                        handler.removeCallbacks(fetchMoved); handler.postDelayed(fetchMoved, 600)
                    }
                }
                refresh()
            }
        }
        mapView.onStart(); mapView.onResume()
    }

    fun refresh() {
        if (!config.configured && projection.get(storyId)?.placeMap == null) {
            status.text = "Фото сохранено. Подключите сервер в настройках для выбора дома."
        }
        val remote = projection.get(storyId)
        val saved = remote?.placeMap
        if (!restored && saved != null) applyMap(saved, true)
        if (!restored && nativeMap?.style != null && store.story(storyId)?.serverStoryId != null) loadMap(null)
        if (restored && mapData != null && mapData!!.identityGeneration < (remote?.identityGeneration ?: 0)) {
            loadMap(nativeMap?.cameraPosition?.target)
        }
        durableGroup = PlaceGroupPresentation.current(remote?.placeSelection, store.story(storyId)?.photoSha256, remote?.identityGeneration, remote?.identityClarification)
        val signature = remote?.placeSelection?.toString()
        if (restored && signature != selectionSignature && !selectionDirty) {
            selectionSignature = signature
            val group = durableGroup
            if (group != null) {
                selectionMode = "rough"; modes.check(roughMode.id); selectionDirty = false
                selected = null; selectedGroup.clear()
                val retained = group.features?.let { runCatching { FeatureCollection.fromJson(it.toString()).features().orEmpty() }.getOrNull() }.orEmpty()
                (retained + buildings()).filter { candidateId(it) in group.candidateIds }.forEach { selectedGroup.putIfAbsent(candidateId(it), it) }
            } else if (remote?.placeSelection != null && remote.placeSelection.get("mode")?.asString == "exact") {
                selectionMode = "exact"; modes.check(exactMode.id); selectionDirty = false
                selectedGroup.clear()
                val id = remote.placeSelection.get("candidate_id")?.takeIf { !it.isJsonNull }?.asString
                    ?: remote.placeSelection.get("poi_id")?.takeIf { !it.isJsonNull }?.asString
                selected = buildings().firstOrNull { candidateId(it) == id }
                if (selected == null) {
                    val coordinates = remote.placeSelection.getAsJsonObject("candidate")?.getAsJsonObject("map_coordinates")
                    val lat = coordinates?.get("latitude")?.asDouble
                    val lon = coordinates?.get("longitude")?.asDouble
                    if (lat != null && lon != null && !id.isNullOrBlank()) selected = Feature.fromGeometry(Point.fromLngLat(lon, lat)).apply {
                        addStringProperty("candidate_id", id)
                        addStringProperty("label", remote.candidateName ?: id)
                    }
                }
            } else {
                selected = null; selectedGroup.clear()
                selectionMode = "exact"; modes.check(exactMode.id); selectionDirty = false
            }
        }
        // The durable outbox restores an unacknowledged group without claiming it was accepted.
        if (restored && !selectionDirty) {
            store.pendingOperations(storyId).lastOrNull { it.kind == "place" }?.let { operation ->
                runCatching {
                    val payload = com.google.gson.JsonParser.parseString(operation.payloadJson).asJsonObject
                    if (payload["mode"]?.asString == "rough" && payload["expected_photo_sha256"]?.asString == store.story(storyId)?.photoSha256 &&
                        payload["expected_identity_generation"]?.asInt == (remote?.identityGeneration ?: 0)) {
                        val ids = payload.getAsJsonArray("candidate_ids").map { it.asString }
                        selectionMode = "rough"; modes.check(roughMode.id); selectionDirty = false
                        selected = null; selectedGroup.clear()
                        buildings().filter { candidateId(it) in ids }.forEach { selectedGroup[candidateId(it)] = it }
                    }
                }
            }
        }
        if (restored) updateSelection()
        val focus = remote?.placeMapFocus?.takeIf { it.valid() && remote?.photoSha256 == store.story(storyId)?.photoSha256 }
        val focusKey = focus?.let { "${remote?.photoSha256}:${it.revision}:${it.areaId}" }
        // Reopening or choosing B must not replay an already applied voice focus
        // over the author's later pan. A fresh server revision still moves the map.
        if (appliedFocus == null && focus != null && remote?.appliedPlaceMapFocusRevision == focus.revision) appliedFocus = focusKey
        if (focus != null && focusKey != appliedFocus && nativeMap?.style != null && !fetching) {
            appliedFocus = focusKey
            val target = LatLng(focus.lat!!, focus.lon!!)
            requestedCenter = target; requestedZoom = focus.zoom
            nativeMap?.cameraPosition = CameraPosition.Builder().target(target).zoom(focus.zoom).build()
            loadMap(target)
        }
    }

    private fun buildings(): List<Feature> = mapData?.buildings?.let {
        runCatching { FeatureCollection.fromJson(it.toString()).features().orEmpty() }.getOrDefault(emptyList())
    }.orEmpty()

    private fun loadMap(center: LatLng?) {
        if (disposed || fetching || !config.configured) return
        val story = store.story(storyId) ?: return
        val serverId = story.serverStoryId ?: return
        fetching = true
        status.text = "Загружаем контуры…"
        confirm.isEnabled = false
        val zoom = nativeMap?.cameraPosition?.zoom
        val focused = projection.get(storyId)
        val focusRevision = focused?.placeMapFocus?.takeIf {
            appliedFocus == "${focused?.photoSha256}:${it.revision}:${it.areaId}"
        }?.revision
        Thread {
            val result = runCatching { ApiClient(config.backendUrl!!, config.deviceToken!!).placeMap(serverId, center?.latitude, center?.longitude, zoom) }
            handler.post {
                if (disposed) return@post
                fetching = false
                result.onSuccess { data ->
                    if (data.identityGeneration != (projection.get(storyId)?.identityGeneration ?: 0) ||
                        (data.photoSha256.isNotBlank() && data.photoSha256 != store.story(storyId)?.photoSha256)) { refresh(); return@onSuccess }
                    requestedCenter = center
                    requestedZoom = zoom
                    projection.replacePlaceMap(storyId, data)
                    if (focusRevision != null) projection.markPlaceMapFocusApplied(storyId, focusRevision)
                    applyMap(data, !restored)
                    refresh()
                }.onFailure {
                    status.text = "Контуры недоступны. Переместите карту или расскажите Мире, где находится дом."
                    restored = true
                    if (center == null && story.latitude != null && story.longitude != null) {
                        nativeMap?.cameraPosition = CameraPosition.Builder().target(LatLng(story.latitude, story.longitude)).zoom(17.0).build()
                    }
                }
            }
        }.start()
    }

    private fun applyMap(data: PlaceMapWire, move: Boolean) {
        mapData = data
        requestedZoom = data.zoom ?: 17.0
        val style = nativeMap?.style ?: return
        style.getSourceAs<GeoJsonSource>("owner-buildings")?.setGeoJson(data.buildings?.toString() ?: EMPTY_FEATURES)
        val camera = projection.get(storyId)?.cameraPoint ?: data.camera
        if (camera?.latitude != null && camera.longitude != null) {
            val point = LatLng(camera.latitude!!, camera.longitude!!)
            style.getSourceAs<GeoJsonSource>("owner-camera")?.setGeoJson(Feature.fromGeometry(Point.fromLngLat(point.longitude, point.latitude)))
            if (move) {
                val center = data.center
                val target = if (center?.latitude != null && center.longitude != null) LatLng(center.latitude!!, center.longitude!!) else point
                nativeMap?.cameraPosition = CameraPosition.Builder().target(target).zoom(data.zoom ?: camera.zoom ?: 17.0).build()
            }
            if (requestedCenter == null) requestedCenter = point
        } else if (move) {
            val center = data.center
            val target = if (center?.latitude != null && center.longitude != null) LatLng(center.latitude!!, center.longitude!!) else LatLng(0.0, 0.0)
            nativeMap?.cameraPosition = CameraPosition.Builder().target(target).zoom(data.zoom ?: if (center == null) 1.0 else 17.0).build()
        }
        // A freshly loaded area never substitutes another building for the owner's choice.
        restored = true
        updateSelection()
    }

    private fun updateSelection() {
        val source = nativeMap?.style?.getSourceAs<GeoJsonSource>("owner-selection")
        val feature = selected
        val rough = selectionMode == "rough"
        val features = if (rough) selectedGroup.values.toList() else feature?.let { listOf(it) }.orEmpty()
        source?.setGeoJson(FeatureCollection.fromFeatures(features))
        val pending = store.pendingOperations(storyId).any { it.kind == "place" }
        val narrowed = if (rough && !selectionDirty && !pending) durableGroup?.narrowedCandidateId?.let { selectedGroup[it] } else null
        nativeMap?.style?.getSourceAs<GeoJsonSource>("owner-narrowed")?.setGeoJson(FeatureCollection.fromFeatures(narrowed?.let { listOf(it) }.orEmpty()))
        val groupInSnapshot = selectedGroup.keys.all { id -> buildings().any { candidateId(it) == id } }
        confirm.text = if (rough) "Это группа домов" else "Это мой дом"
        val selectedInSnapshot = feature != null && buildings().any { candidateId(it) == candidateId(feature) }
        confirm.isEnabled = (if (rough) selectedGroup.size in 2..16 && groupInSnapshot else selectedInSnapshot) && !fetching &&
            (mapData == null || mapData!!.identityGeneration >= (projection.get(storyId)?.identityGeneration ?: 0)) && !pending
        val navigationPending = store.pendingOperations(storyId).any { it.kind in setOf("place-navigation", "place-unknown") }
        val serverReady = store.story(storyId)?.serverStoryId != null && config.configured
        elsewhereButton.isEnabled = serverReady && !pending && !navigationPending
        unknownButton.isEnabled = serverReady && !pending && !navigationPending
        val remote = projection.get(storyId)
        val question = remote?.identityClarification?.get("question")?.takeIf { it.isJsonPrimitive }?.asString
        clarification.visibility = if (!rough && !question.isNullOrBlank()) VISIBLE else GONE
        clarification.isEnabled = serverReady && !pending && !navigationPending
        val house = (narrowed ?: feature)?.let(::houseMarker)
        nativeMap?.style?.getSourceAs<GeoJsonSource>("owner-house")?.setGeoJson(FeatureCollection.fromFeatures(house?.let { listOf(it) }.orEmpty()))
        automaticAction.visibility = if (rough) GONE else VISIBLE
        groupStatus.visibility = if (rough) VISIBLE else GONE
        groupStatus.text = when {
            pending -> "Группа сохраняется · уточнение начнётся после подтверждения сервером"
            !selectionDirty && durableGroup != null -> durableGroup!!.notice
            !groupInSnapshot -> "Выбранные контуры вне загруженной области. Вернитесь к группе или измените выбор."
            else -> "Выбрано домов: ${selectedGroup.size} · нажмите ещё раз, чтобы убрать. Нужно от 2 до 16."
        }
        val cameraLabel = "Синяя точка: GPS снимка. Оранжевая точка: выбранный дом."
        status.text = when {
            store.story(storyId)?.stage == StoryStage.PLACE_UNKNOWN -> "Место съёмки не определено"
            !rough && !question.isNullOrBlank() -> question
            rough && buildings().isEmpty() -> "Для группы нужны контуры. Переместите карту или поговорите с Мирой. $cameraLabel"
            rough -> "Оранжевый: выбранная группа. Фиолетовый: определённый дом. $cameraLabel"
            selected != null -> "${featureLabel(selected!!)} · $cameraLabel"
            buildings().isEmpty() -> "Контуров нет. Переместите карту или нажмите «Дом не здесь»."
            else -> "Нажмите на контур дома. $cameraLabel"
        }
    }

    private fun houseMarker(feature: Feature): Feature? {
        val geometry = feature.geometry() ?: return null
        val point = PlaceFootprintGeometry.interiorPoint(geometry)
        return point?.let { Feature.fromGeometry(it) }
    }

    private fun confirmSelection() {
        if (!confirm.isEnabled) return
        if (selectionMode == "rough" && selectedGroup.size !in 2..16) return
        if (selectionMode == "exact" && selected == null) return
        enqueuePlace()
    }

    private fun enqueuePlace() {
        val story = store.story(storyId) ?: return
        val state = projection.get(storyId)
        val commandId = "ss-place-${UUID.randomUUID()}"
        val payload = linkedMapOf<String, Any?>("command_id" to commandId, "mode" to selectionMode, "snapshot_id" to mapData?.snapshotId,
            "candidate_id" to selected?.let(::candidateId), "expected_photo_sha256" to story.photoSha256,
            "expected_identity_generation" to (state?.identityGeneration ?: 0), "expected_candidate_id" to state?.candidateId)
        if (selectionMode == "rough") {
            payload.remove("candidate_id")
            payload["candidate_ids"] = selectedGroup.keys.toList()
        }
        store.enqueueOperation(storyId, "place", commandId, PlaceCommandJson.encode(payload))
        selectionDirty = false
        updateSelection()
        confirm.isEnabled = false
        status.text = if (selectionMode == "rough") "Сохраняем группу. Сначала уточним дом внутри неё." else "Сохраняем выбранное место. Факты начнут появляться здесь."
        SyncScheduler.enqueue(activity)
    }

    private fun candidateId(feature: Feature): String = propertyString(feature, "candidate_id") ?: propertyString(feature, "poi_id") ?: feature.id().orEmpty()
    private fun featureLabel(feature: Feature): String = listOf("label", "name", "address").mapNotNull { propertyString(feature, it)?.takeIf(String::isNotBlank) }.firstOrNull() ?: candidateId(feature)
    private fun propertyString(feature: Feature, key: String): String? = feature.properties()?.get(key)?.takeIf { it.isJsonPrimitive && it.asJsonPrimitive.isString }?.asString
    fun resume() { mapView.onResume() }
    fun pause() { mapView.onPause() }
    fun start() { mapView.onStart() }
    fun stop() { mapView.onStop() }
    fun lowMemory() { mapView.onLowMemory() }
    fun dispose() { disposed = true; handler.removeCallbacksAndMessages(null); mapView.onPause(); mapView.onStop(); mapView.onDestroy() }
    private fun dp(value: Float) = (value * resources.displayMetrics.density).toInt()

    companion object {
        private const val EMPTY_FEATURES = "{\"type\":\"FeatureCollection\",\"features\":[]}"
        // Interactive on-demand tiles only; the SDK supplies attribution and normal HTTP caching.
        private const val BASE_STYLE = """{"version":8,"sources":{"osm":{"type":"raster","tiles":["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],"tileSize":256,"maxzoom":19,"attribution":"© OpenStreetMap contributors"}},"layers":[{"id":"osm","type":"raster","source":"osm"}]}"""
    }
}
