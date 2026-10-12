package com.onedayonemasterpiece.streetstory

import android.app.Activity
import android.app.AlertDialog
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.MotionEvent
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import com.google.gson.Gson
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
class PlaceMapWire {
    @SerializedName("snapshot_id") var snapshotId: String = ""
    @SerializedName("identity_generation") var identityGeneration: Int = 0
    @SerializedName("photo_sha256") var photoSha256: String = ""
    var revision: Int = 0
    var camera: PlaceCameraWire? = null
    var center: PlaceCameraWire? = null
    var zoom: Double? = null
    var buildings: JsonObject? = null
    var streets: JsonObject? = null
    var selected: JsonObject? = null
}

/** One native map; selection is always a server-supplied building or an explicit owner point. */
class PlaceMapPanel(
    private val activity: Activity,
    private val storyId: String,
    private val store: StoryStore,
    private val projection: ResearchProjectionStore,
    private val config: ConfigStore,
) : LinearLayout(activity) {
    private val gson = Gson()
    private val handler = Handler(Looper.getMainLooper())
    val mapView: MapView
    private var nativeMap: MapLibreMap? = null
    private var mapData: PlaceMapWire? = null
    private var selected: Feature? = null
    private var explicitPoint: LatLng? = null
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
        mapView = MapView(activity, org.maplibre.android.maps.MapLibreMapOptions.createFromAttributes(activity).textureMode(true)).apply { contentDescription = "owner-place-map"; onCreate(Bundle()) }
        val available = activity.resources.displayMetrics.heightPixels / activity.resources.displayMetrics.density
        addView(mapView, LayoutParams(-1, dp((available * .25f).coerceIn(112f, 240f))))
        // A native gesture owns this touch sequence; scrolling resumes after UP/CANCEL.
        mapView.setOnTouchListener { view, event ->
            parent?.requestDisallowInterceptTouchEvent(event.actionMasked != MotionEvent.ACTION_UP && event.actionMasked != MotionEvent.ACTION_CANCEL)
            if (event.actionMasked == MotionEvent.ACTION_UP) view.performClick()
            false
        }
        addView(status.apply { text = "Загружаем фото и карту…"; textSize = 13f; contentDescription = "owner-place-status" })
        val buttons = LinearLayout(activity)
        confirm.text = "Это мой дом"
        confirm.contentDescription = "owner-place-confirm"
        confirm.isEnabled = false
        confirm.setOnClickListener { confirmSelection() }
        buttons.addView(confirm, LayoutParams(0, -2, 1f))
        buttons.addView(Button(activity).apply {
            text = "Указать точку"
            contentDescription = "owner-place-point"
            setOnClickListener {
                explicitPoint = nativeMap?.cameraPosition?.target
                selected = null
                updateSelection()
                askAnchor(true)
            }
        }, LayoutParams(0, -2, 1f))
        addView(buttons)
        addView(TextView(activity).apply {
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
                style.addLayer(FillLayer("owner-buildings-fill", "owner-buildings").withProperties(fillColor("#819f89"), fillOpacity(.42f)))
                style.addLayer(LineLayer("owner-buildings-outline", "owner-buildings").withProperties(lineColor("#345747"), lineWidth(1.4f)))
                style.addLayer(FillLayer("owner-selected-fill", "owner-selection").withProperties(fillColor("#e48439"), fillOpacity(.6f)))
                style.addLayer(LineLayer("owner-selected-line", "owner-selection").withProperties(lineColor("#d85d15"), lineWidth(3f)))
                style.addLayer(CircleLayer("owner-selected-point", "owner-selection").withProperties(circleColor("#e48439"), circleRadius(7f), circleStrokeColor("#ffffff"), circleStrokeWidth(2f)))
                style.addLayer(CircleLayer("owner-camera-dot", "owner-camera").withProperties(circleColor("#3569ae"), circleRadius(6f), circleStrokeColor("#ffffff"), circleStrokeWidth(2f)))
                map.addOnMapClickListener { point ->
                    val hit = map.queryRenderedFeatures(map.projection.toScreenLocation(point), "owner-buildings-fill").firstOrNull()
                    if (hit != null) { selected = hit; explicitPoint = null; updateSelection() }
                    hit != null
                }
                map.addOnMapLongClickListener { point ->
                    explicitPoint = point; selected = null; updateSelection(); true
                }
                map.addOnCameraIdleListener {
                    val center = map.cameraPosition.target
                    if (restored && center != null && (requestedCenter == null || requestedCenter!!.distanceTo(center) > 120 || kotlin.math.abs(map.cameraPosition.zoom - (requestedZoom ?: map.cameraPosition.zoom)) > .4)) {
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
        if (restored && remote?.placeSelection != null && selected == null && explicitPoint == null) {
            val id = remote.placeSelection.get("candidate_id")?.asString ?: remote.placeSelection.get("poi_id")?.asString
            selected = buildings().firstOrNull { candidateId(it) == id }
            if (selected == null) {
                val coordinates = remote.placeSelection.getAsJsonObject("candidate")?.getAsJsonObject("map_coordinates")
                val lat = coordinates?.get("latitude")?.asDouble
                val lon = coordinates?.get("longitude")?.asDouble
                if (lat != null && lon != null) explicitPoint = LatLng(lat, lon)
            }
            updateSelection()
        }
        if (restored) updateSelection()
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
        Thread {
            val result = runCatching { ApiClient(config.backendUrl!!, config.deviceToken!!).placeMap(serverId, center?.latitude, center?.longitude, zoom) }
            handler.post {
                if (disposed) return@post
                fetching = false
                result.onSuccess { data ->
                    if (data.identityGeneration < (projection.get(storyId)?.identityGeneration ?: 0)) { refresh(); return@onSuccess }
                    requestedCenter = center
                    requestedZoom = zoom
                    projection.replacePlaceMap(storyId, data)
                    applyMap(data, !restored)
                }.onFailure {
                    status.text = "Контуры недоступны. Укажите точку и название/адрес."
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
        val camera = data.camera
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
        val feature = selected ?: explicitPoint?.let { Feature.fromGeometry(Point.fromLngLat(it.longitude, it.latitude)) }
        source?.setGeoJson(FeatureCollection.fromFeatures(feature?.let { listOf(it) }.orEmpty()))
        confirm.isEnabled = feature != null && !fetching && (mapData == null || mapData!!.identityGeneration >= (projection.get(storyId)?.identityGeneration ?: 0)) && !store.pendingOperations(storyId).any { it.kind == "place" }
        val cameraLabel = when (mapData?.camera?.origin) {
            "exif" -> "Синяя точка: камера (EXIF GPS)."
            "manual" -> "Синяя точка: вручную заданная камера."
            else -> "GPS в фото нет: выберите место вручную."
        }
        status.text = when {
            selected != null -> "${featureLabel(selected!!)} · $cameraLabel"
            explicitPoint != null -> "Ручная точка: добавьте название/адрес."
            buildings().isEmpty() -> "Контуров нет. Переместите карту или укажите точку и название/адрес."
            else -> "Нажмите на контур дома. $cameraLabel"
        }
    }

    private fun confirmSelection() {
        if (explicitPoint != null) { askAnchor(true); return }
        val feature = selected ?: return
        if (featureLabel(feature) == candidateId(feature) || featureLabel(feature).startsWith("Здание osm:")) askAnchor(false) else enqueuePlace(emptyMap())
    }

    private fun askAnchor(pointMode: Boolean) {
        val fields = LinearLayout(activity).apply { orientation = VERTICAL; setPadding(dp(16f), 0, dp(16f), 0) }
        val city = EditText(activity).apply { hint = "Город"; setSingleLine() }; fields.addView(city)
        val address = EditText(activity).apply { hint = "Адрес или название объекта"; setSingleLine() }; fields.addView(address)
        val point = explicitPoint
        val latitude = EditText(activity).apply { hint = "Широта"; setSingleLine(); setText(point?.latitude?.toString().orEmpty()) }
        val longitude = EditText(activity).apply { hint = "Долгота"; setSingleLine(); setText(point?.longitude?.toString().orEmpty()) }
        if (pointMode) { fields.addView(latitude); fields.addView(longitude) }
        val dialog = AlertDialog.Builder(activity).setTitle("Название или адрес выбранного места").setView(fields)
            .setNegativeButton("Назад к карте", null).setPositiveButton("Подтвердить", null).create()
        dialog.setOnShowListener { dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
            if (address.text.isBlank() || city.text.isBlank()) { address.error = "Укажите город и адрес или название"; return@setOnClickListener }
            if (pointMode) {
                val lat = latitude.text.toString().toDoubleOrNull(); val lon = longitude.text.toString().toDoubleOrNull()
                if (lat == null || lon == null || lat !in -90.0..90.0 || lon !in -180.0..180.0) { latitude.error = "Укажите координаты точки"; return@setOnClickListener }
                explicitPoint = LatLng(lat, lon)
                enqueuePlace(mapOf("fallback" to mapOf("latitude" to lat, "longitude" to lon, "city" to city.text.toString(), "name" to address.text.toString(), "address" to address.text.toString())))
            } else enqueuePlace(mapOf("owner_context" to mapOf("city" to city.text.toString(), "address" to address.text.toString(), "name" to address.text.toString())))
            dialog.dismiss()
        } }
        dialog.show()
    }

    private fun enqueuePlace(extra: Map<String, Any>) {
        val story = store.story(storyId) ?: return
        val state = projection.get(storyId)
        val commandId = "ss-place-${UUID.randomUUID()}"
        val payload = linkedMapOf<String, Any?>("command_id" to commandId, "mode" to "exact", "snapshot_id" to mapData?.snapshotId,
            "candidate_id" to selected?.let(::candidateId), "expected_photo_sha256" to story.photoSha256,
            "expected_identity_generation" to (state?.identityGeneration ?: 0), "expected_candidate_id" to state?.candidateId)
        payload.putAll(extra)
        store.enqueueOperation(storyId, "place", commandId, gson.toJson(payload))
        confirm.isEnabled = false
        status.text = "Сохраняем выбранное место. Факты начнут появляться здесь."
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
