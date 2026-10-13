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
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.Toast
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
    private var selectionMode = "exact"
    private val selectedGroup = linkedMapOf<String, Feature>()
    private var selectionDirty = false
    private var selectionSignature: String? = null
    private var durableGroup: PlaceGroupSnapshot? = null
    private val modes = RadioGroup(activity)
    private val exactMode = RadioButton(activity)
    private val roughMode = RadioButton(activity)
    private val pointButton = Button(activity)
    private val automaticAction = TextView(activity)
    private val groupStatus = TextView(activity)
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
                selected = null; explicitPoint = null
                updateSelection()
            }
        }
        addView(modes)
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
        addView(groupStatus.apply { textSize = 13f; contentDescription = "owner-place-group-status"; visibility = GONE })
        val buttons = LinearLayout(activity)
        confirm.text = "Это мой дом"
        confirm.contentDescription = "owner-place-confirm"
        confirm.isEnabled = false
        confirm.setOnClickListener { confirmSelection() }
        buttons.addView(confirm, LayoutParams(0, -2, 1f))
        buttons.addView(pointButton.apply {
            text = "Указать точку"
            contentDescription = "owner-place-point"
            setOnClickListener {
                explicitPoint = nativeMap?.cameraPosition?.target
                selected = null
                selectionDirty = true
                updateSelection()
                askAnchor(true)
            }
        }, LayoutParams(0, -2, 1f))
        addView(buttons)
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
                style.addLayer(FillLayer("owner-buildings-fill", "owner-buildings").withProperties(fillColor("#819f89"), fillOpacity(.42f)))
                style.addLayer(LineLayer("owner-buildings-outline", "owner-buildings").withProperties(lineColor("#345747"), lineWidth(1.4f)))
                style.addLayer(FillLayer("owner-selected-fill", "owner-selection").withProperties(fillColor("#e48439"), fillOpacity(.6f)))
                style.addLayer(LineLayer("owner-selected-line", "owner-selection").withProperties(lineColor("#d85d15"), lineWidth(3f)))
                style.addLayer(CircleLayer("owner-selected-point", "owner-selection").withProperties(circleColor("#e48439"), circleRadius(7f), circleStrokeColor("#ffffff"), circleStrokeWidth(2f)))
                style.addLayer(FillLayer("owner-narrowed-fill", "owner-narrowed").withProperties(fillColor("#7752ac"), fillOpacity(.72f)))
                style.addLayer(LineLayer("owner-narrowed-line", "owner-narrowed").withProperties(lineColor("#543583"), lineWidth(4f)))
                style.addLayer(CircleLayer("owner-camera-dot", "owner-camera").withProperties(circleColor("#3569ae"), circleRadius(6f), circleStrokeColor("#ffffff"), circleStrokeWidth(2f)))
                map.addOnMapClickListener { point ->
                    val hit = map.queryRenderedFeatures(map.projection.toScreenLocation(point), "owner-buildings-fill").firstOrNull()
                    if (hit != null) {
                        selectionDirty = true
                        explicitPoint = null
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
                map.addOnMapLongClickListener { point ->
                    if (selectionMode == "rough") {
                        Toast.makeText(activity, "Для группы выберите контуры. Для точки включите «Точно».", Toast.LENGTH_SHORT).show()
                    } else { explicitPoint = point; selected = null; selectionDirty = true; updateSelection() }
                    true
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
        durableGroup = PlaceGroupPresentation.current(remote?.placeSelection, store.story(storyId)?.photoSha256, remote?.identityGeneration, remote?.identityClarification)
        val signature = remote?.placeSelection?.toString()
        if (restored && signature != selectionSignature && !selectionDirty) {
            selectionSignature = signature
            val group = durableGroup
            if (group != null) {
                selectionMode = "rough"; modes.check(roughMode.id); selectionDirty = false
                selected = null; explicitPoint = null; selectedGroup.clear()
                val retained = group.features?.let { runCatching { FeatureCollection.fromJson(it.toString()).features().orEmpty() }.getOrNull() }.orEmpty()
                (retained + buildings()).filter { candidateId(it) in group.candidateIds }.forEach { selectedGroup.putIfAbsent(candidateId(it), it) }
            } else if (remote?.placeSelection != null && remote.placeSelection.get("mode")?.asString == "exact") {
                selectionMode = "exact"; modes.check(exactMode.id); selectionDirty = false
                selectedGroup.clear()
                val id = remote.placeSelection.get("candidate_id")?.takeIf { !it.isJsonNull }?.asString
                    ?: remote.placeSelection.get("poi_id")?.takeIf { !it.isJsonNull }?.asString
                selected = buildings().firstOrNull { candidateId(it) == id }
                explicitPoint = null
                if (selected == null) {
                    val coordinates = remote.placeSelection.getAsJsonObject("candidate")?.getAsJsonObject("map_coordinates")
                    val lat = coordinates?.get("latitude")?.asDouble
                    val lon = coordinates?.get("longitude")?.asDouble
                    if (lat != null && lon != null) explicitPoint = LatLng(lat, lon)
                }
            } else {
                selected = null; explicitPoint = null; selectedGroup.clear()
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
                        selected = null; explicitPoint = null; selectedGroup.clear()
                        buildings().filter { candidateId(it) in ids }.forEach { selectedGroup[candidateId(it)] = it }
                    }
                }
            }
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
        val rough = selectionMode == "rough"
        val features = if (rough) selectedGroup.values.toList() else feature?.let { listOf(it) }.orEmpty()
        source?.setGeoJson(FeatureCollection.fromFeatures(features))
        val pending = store.pendingOperations(storyId).any { it.kind == "place" }
        val narrowed = if (rough && !selectionDirty && !pending) durableGroup?.narrowedCandidateId?.let { selectedGroup[it] } else null
        nativeMap?.style?.getSourceAs<GeoJsonSource>("owner-narrowed")?.setGeoJson(FeatureCollection.fromFeatures(narrowed?.let { listOf(it) }.orEmpty()))
        val groupInSnapshot = selectedGroup.keys.all { id -> buildings().any { candidateId(it) == id } }
        confirm.text = if (rough) "Это группа домов" else "Это мой дом"
        confirm.isEnabled = (if (rough) selectedGroup.size in 2..16 && groupInSnapshot else feature != null) && !fetching &&
            (mapData == null || mapData!!.identityGeneration >= (projection.get(storyId)?.identityGeneration ?: 0)) && !pending
        pointButton.isEnabled = !rough && !pending
        automaticAction.visibility = if (rough) GONE else VISIBLE
        groupStatus.visibility = if (rough) VISIBLE else GONE
        groupStatus.text = when {
            pending -> "Группа сохраняется · уточнение начнётся после подтверждения сервером"
            !selectionDirty && durableGroup != null -> durableGroup!!.notice
            !groupInSnapshot -> "Выбранные контуры вне загруженной области. Вернитесь к группе или измените выбор."
            else -> "Выбрано домов: ${selectedGroup.size} · нажмите ещё раз, чтобы убрать. Нужно от 2 до 16."
        }
        val cameraLabel = when (mapData?.camera?.origin) {
            "exif" -> "Синяя точка: камера (EXIF GPS)."
            "manual" -> "Синяя точка: вручную заданная камера."
            else -> "GPS в фото нет: выберите место вручную."
        }
        status.text = when {
            rough && buildings().isEmpty() -> "Для группы нужны контуры. Переместите карту или включите «Точно» и укажите точку. $cameraLabel"
            rough -> "Оранжевый: выбранная группа. Фиолетовый: определённый дом. $cameraLabel"
            selected != null -> "${featureLabel(selected!!)} · $cameraLabel"
            explicitPoint != null -> "Ручная точка: добавьте название/адрес."
            buildings().isEmpty() -> "Контуров нет. Переместите карту или укажите точку и название/адрес."
            else -> "Нажмите на контур дома. $cameraLabel"
        }
    }

    private fun observedContext(feature: Feature?): Map<String, String> {
        if (feature == null) return emptyMap()
        val anchor = feature.properties()?.get("search_anchor")?.takeIf { it.isJsonObject }?.asJsonObject
        val address = feature.properties()?.get("address")?.takeIf { it.isJsonObject }?.asJsonObject
        val tags = feature.properties()?.get("tags")?.takeIf { it.isJsonObject }?.asJsonObject
        fun value(objectValue: JsonObject?, key: String): String? = objectValue?.get(key)?.takeIf { it.isJsonPrimitive }?.asString?.trim()?.takeIf { it.isNotBlank() }
        val city = value(anchor, "city") ?: value(address, "city") ?: value(tags, "addr:city")
        val street = value(anchor, "street") ?: value(address, "street") ?: value(tags, "addr:street")
        val number = value(anchor, "house_number") ?: value(address, "house_number") ?: value(tags, "addr:housenumber")
        val streetAddress = value(anchor, "address") ?: if (street != null && number != null) "$street $number" else null
        val name = anchor?.get("names")?.takeIf { it.isJsonArray }?.asJsonArray?.firstOrNull { it.isJsonPrimitive && it.asString.isNotBlank() }?.asString
            ?: value(tags, "name")
        return mapOf("city" to city.orEmpty(), "address" to streetAddress.orEmpty(), "name" to name.orEmpty())
    }

    private fun confirmSelection() {
        if (selectionMode == "rough") {
            if (selectedGroup.size !in 2..16) return
            askAnchor(false)
            return
        }
        if (explicitPoint != null) { askAnchor(true); return }
        val feature = selected ?: return
        val context = observedContext(feature)
        if (context["city"].isNullOrBlank() || (context["address"].isNullOrBlank() && context["name"].isNullOrBlank())) {
            askAnchor(false)
        } else enqueuePlace(emptyMap())
    }

    private fun askAnchor(pointMode: Boolean) {
        val rough = selectionMode == "rough"
        // Never use one member's address or name as the whole group's identity.
        val cities = selectedGroup.values.mapNotNull { observedContext(it)["city"]?.takeIf(String::isNotBlank) }.distinct()
        val observed = if (rough) mapOf("city" to cities.singleOrNull().orEmpty()) else observedContext(if (pointMode) null else selected)
        val fields = LinearLayout(activity).apply { orientation = VERTICAL; setPadding(dp(16f), 0, dp(16f), 0) }
        fields.addView(TextView(activity).apply {
            text = if (rough) "Город и сведения о группе (если известны). Дом будет определён только внутри выбранных контуров." else if (pointMode) "Укажите город и адрес или название выбранной точки." else "Добавьте город и адрес или название выбранного дома."
            textSize = 13f
        })
        val city = EditText(activity).apply {
            hint = "Город"; contentDescription = "owner-place-city"; setSingleLine(); setText(observed["city"].orEmpty())
        }; fields.addView(city)
        val address = EditText(activity).apply {
            hint = if (rough) "Уточнение адреса (необязательно)" else "Адрес выбранного дома"; contentDescription = "owner-place-address"; setSingleLine(); setText(observed["address"].orEmpty())
        }; fields.addView(address)
        val name = EditText(activity).apply {
            hint = "Название объекта (если известно)"; contentDescription = "owner-place-name"; setSingleLine(); setText(observed["name"].orEmpty())
        }; fields.addView(name)
        val point = explicitPoint
        val latitude = EditText(activity).apply { hint = "Широта"; setSingleLine(); setText(point?.latitude?.toString().orEmpty()) }
        val longitude = EditText(activity).apply { hint = "Долгота"; setSingleLine(); setText(point?.longitude?.toString().orEmpty()) }
        if (pointMode) { fields.addView(latitude); fields.addView(longitude) }
        val dialog = AlertDialog.Builder(activity).setTitle("Контекст выбранного места").setView(fields)
            .setNegativeButton("Назад к карте", null).setPositiveButton("Подтвердить", null).create()
        dialog.setOnShowListener { dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
            if (!rough && city.text.isBlank()) { city.error = "Укажите город"; return@setOnClickListener }
            if (!rough && address.text.isBlank() && name.text.isBlank()) {
                address.error = "Укажите адрес или название выбранного дома"
                return@setOnClickListener
            }
            val owner = mapOf("city" to city.text.toString().trim(), "address" to address.text.toString().trim(), "name" to name.text.toString().trim())
                .filterValues { it.isNotBlank() }
            if (pointMode) {
                val lat = latitude.text.toString().toDoubleOrNull(); val lon = longitude.text.toString().toDoubleOrNull()
                if (lat == null || lon == null || lat !in -90.0..90.0 || lon !in -180.0..180.0) { latitude.error = "Укажите координаты точки"; return@setOnClickListener }
                explicitPoint = LatLng(lat, lon)
                enqueuePlace(mapOf("fallback" to (owner + mapOf("latitude" to lat, "longitude" to lon))))
            } else enqueuePlace(mapOf("owner_context" to owner))
            dialog.dismiss()
        } }
        dialog.show()
    }

    private fun enqueuePlace(extra: Map<String, Any>) {
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
        payload.putAll(extra)
        store.enqueueOperation(storyId, "place", commandId, gson.toJson(payload))
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
