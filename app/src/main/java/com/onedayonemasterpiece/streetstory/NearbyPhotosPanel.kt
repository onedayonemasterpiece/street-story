package com.onedayonemasterpiece.streetstory

import android.app.Activity
import android.app.Dialog
import android.content.ClipData
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.provider.MediaStore
import android.util.LruCache
import android.util.Size
import android.view.DragEvent
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.AbsListView
import android.widget.BaseAdapter
import android.widget.Button
import android.widget.FrameLayout
import android.widget.GridLayout
import android.widget.GridView
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import java.util.UUID
import java.util.concurrent.Executors

/**
 * Owner's single visual photo workflow: choose near the two EXIF/POI centers,
 * drag the ordered photo tiles, and inspect finished cards. No extra server state.
 */
class NearbyPhotosPanel(
    private val activity: Activity,
    private val storyId: String,
    private val store: StoryStore,
    private val research: ResearchProjectionStore,
    private val config: ConfigStore,
) : LinearLayout(activity) {
    private data class Candidate(val id: String, val uri: String, val source: Boolean)
    private val thumbCache = object : LruCache<String, Bitmap>(24 * 1024) {
        override fun sizeOf(key: String, value: Bitmap): Int =
            (value.byteCount / 1024).coerceAtLeast(1)
    }
    private val workers = Executors.newFixedThreadPool(2)
    private var mode = "choose"
    private var lastCandidateSignature: String? = null
    private var lastCardsSignature: String? = null
    private var candidates: List<Candidate> = emptyList()
    private val selected = store.selectedStoryPhotos(storyId).sortedBy { it.position }.toMutableList()
    private val title = text("Фото рядом", 17, true)
    private val pending = text("", 13, false)
    private val chooseGrid = GridView(activity)
    private val chooseAdapter = CandidateAdapter()
    private val done = Button(activity)
    private val puzzle = GridLayout(activity)
    private val changeSelection = Button(activity)
    private val cardTitle = text("Карточки истории", 17, true)
    private val cardsGrid = GridLayout(activity)

    init {
        orientation = VERTICAL
        setPadding(dp(10), dp(13), dp(10), dp(16))
        setBackgroundColor(0xfffffdf8.toInt())
        visibility = GONE
        contentDescription = "nearby-photos"
        addView(title)
        addView(pending)
        chooseGrid.apply {
            numColumns = 3
            verticalSpacing = dp(5); horizontalSpacing = dp(5)
            columnWidth = dp(108)
            stretchMode = GridView.STRETCH_COLUMN_WIDTH
            adapter = chooseAdapter
            contentDescription = "nearby-photo-grid"
            isVerticalScrollBarEnabled = true
        }
        addView(chooseGrid, LayoutParams(LayoutParams.MATCH_PARENT, dp(316)))
        done.contentDescription = "nearby-photo-done"
        done.setOnClickListener {
            if (selected.isEmpty()) {
                Toast.makeText(activity, "Выберите хотя бы одно фото", Toast.LENGTH_SHORT).show()
            } else {
                mode = "order"
                saveAndQueue()
                showMode()
            }
        }
        addView(done)
        puzzle.apply {
            columnCount = 3; rowCount = 3
            contentDescription = "photo-order-puzzle"
        }
        addView(puzzle)
        changeSelection.text = "Изменить выбор"
        changeSelection.contentDescription = "change-photo-selection"
        changeSelection.setOnClickListener { mode = "choose"; showMode() }
        addView(changeSelection)
        cardTitle.visibility = GONE
        cardsGrid.apply { columnCount = 3; rowCount = 3; contentDescription = "story-cards-grid" }
        addView(cardTitle)
        addView(cardsGrid)
        showMode()
    }

    fun refresh() {
        val story = store.story(storyId) ?: return
        val projection = research.get(storyId)
        val selection = projection?.placeSelection
        val exact = selection?.get("mode")?.asString == "exact"
        if (!exact) { visibility = GONE; return }
        visibility = VISIBLE
        val coordinates = runCatching {
            selection?.getAsJsonObject("candidate")?.getAsJsonObject("map_coordinates")
                ?: selection?.getAsJsonObject("candidate")?.getAsJsonObject("center")
        }.getOrNull()
        val objectPoint = runCatching {
            val lat = coordinates?.get("latitude")?.asDouble
            val lon = coordinates?.get("longitude")?.asDouble
            if (lat == null || lon == null) null else PhotoPoint(lat, lon)
        }.getOrNull()
        val camera = if (story.latitude != null && story.longitude != null)
            PhotoPoint(story.latitude, story.longitude) else null
        val nearby = PhonePhotoIndex.nearby(store, camera, objectPoint)
        val original = runCatching {
            MediaStore.getMediaUri(activity, Uri.parse(story.photoPath)).toString()
        }.getOrNull() ?: story.photoPath
        val fresh = listOf(Candidate("source", story.photoPath, true)) + nearby
            .filterNot { it.uri == original || it.uri == story.photoPath }
            .map { image ->
                val id = UUID.nameUUIDFromBytes((storyId + ":" + image.mediaId).toByteArray(Charsets.UTF_8)).toString()
                Candidate(id, image.uri, false)
            }
        val signature = fresh.joinToString("|") { it.id + ":" + it.uri }
        if (signature != lastCandidateSignature) {
            lastCandidateSignature = signature
            candidates = fresh
            chooseAdapter.notifyDataSetChanged()
        }
        val index = PhonePhotoIndex.progress
        pending.text = if (index.running) "Ещё индексируется " + index.processed + " из " + index.total
            else if (PhonePhotoIndex.partialAccess(activity)) "Доступ только к выбранным фото"
            else if (!PhonePhotoIndex.galleryGranted(activity)) "Разрешите доступ к галерее для поиска фото"
            else "Найдено рядом: " + fresh.size
        done.text = "Готово (" + selected.size + ")"
        done.isEnabled = selected.isNotEmpty()
        val cards = projection?.cards.orEmpty().sortedBy { it.position }.filter { it.url.isNotBlank() }
        val cardsSignature = cards.joinToString("|") { it.position.toString() + ":" + it.url }
        if (cardsSignature != lastCardsSignature) {
            lastCardsSignature = cardsSignature
            showCards(cards)
        }
    }

    fun dispose() {
        workers.shutdownNow()
    }

    private fun showMode() {
        chooseGrid.visibility = if (mode == "choose") VISIBLE else GONE
        done.visibility = chooseGrid.visibility
        puzzle.visibility = if (mode == "order") VISIBLE else GONE
        changeSelection.visibility = puzzle.visibility
        if (mode == "order") drawPuzzle()
        done.text = "Готово (" + selected.size + ")"
        done.isEnabled = selected.isNotEmpty()
    }

    private fun toggle(candidate: Candidate) {
        val existing = selected.indexOfFirst { it.photoId == candidate.id }
        if (existing >= 0) selected.removeAt(existing)
        else if (selected.size >= 8) {
            Toast.makeText(activity, "не больше 8 карточек", Toast.LENGTH_SHORT).show()
            return
        } else selected += LocalStoryPhoto(candidate.id, candidate.uri, selected.size)
        persistSelection()
        chooseAdapter.notifyDataSetChanged()
        done.text = "Готово (" + selected.size + ")"
        done.isEnabled = selected.isNotEmpty()
    }

    private fun persistSelection() {
        store.saveStoryPhotos(storyId, selected)
    }

    private fun saveAndQueue() {
        persistSelection()
        if (selected.size == 1 && selected.first().photoId == "source") return // unchanged single-photo path
        store.enqueueStoryPhotoOrder(storyId, selected)
        SyncScheduler.enqueue(activity)
    }

    private fun drawPuzzle() {
        puzzle.removeAllViews()
        for (index in 0..8) {
            val tile = FrameLayout(activity)
            val params = GridLayout.LayoutParams(GridLayout.spec(index / 3), GridLayout.spec(index % 3)).apply {
                width = tileWidth(); height = tileWidth()
                setMargins(dp(2), dp(2), dp(2), dp(2))
            }
            val item = selected.getOrNull(index)
            if (item == null) tile.setBackgroundColor(0xffd7ddd1.toInt())
            else {
                tile.setBackgroundColor(0xffe8ece4.toInt())
                val image = ImageView(activity).apply { scaleType = ImageView.ScaleType.CENTER_CROP }
                tile.addView(image, FrameLayout.LayoutParams(-1, -1))
                bindImage(image, item.uri)
                val label = text(if (index == 0) "1 · Обложка" else (index + 1).toString(), 12, true)
                label.setTextColor(Color.WHITE)
                label.setBackgroundColor(0x99000000.toInt())
                tile.addView(label, FrameLayout.LayoutParams(-1, dp(22), Gravity.BOTTOM))
                tile.contentDescription = "order-tile-" + index
                tile.setOnLongClickListener {
                    it.startDragAndDrop(
                        ClipData.newPlainText("order", index.toString()),
                        View.DragShadowBuilder(it), null, 0,
                    )
                    true
                }
            }
            tile.setOnDragListener { _, event ->
                if (event.action == DragEvent.ACTION_DROP) {
                    val from = event.clipData?.getItemAt(0)?.text?.toString()?.toIntOrNull()
                    if (from != null && from in selected.indices) {
                        val itemToMove = selected.removeAt(from)
                        selected.add(index.coerceAtMost(selected.size), itemToMove)
                        saveAndQueue()
                        drawPuzzle()
                    }
                }
                true
            }
            puzzle.addView(tile, params)
        }
    }

    private fun showCards(cards: List<StoryCardWire>) {
        cardsGrid.removeAllViews()
        val visible = cards.size > 1
        cardTitle.visibility = if (visible) VISIBLE else GONE
        cardsGrid.visibility = if (visible) VISIBLE else GONE
        if (!visible) return
        for (index in 0..8) {
            val tile = FrameLayout(activity)
            val params = GridLayout.LayoutParams(GridLayout.spec(index / 3), GridLayout.spec(index % 3)).apply {
                width = tileWidth(); height = tileWidth()
                setMargins(dp(2), dp(2), dp(2), dp(2))
            }
            val card = cards.getOrNull(index)
            if (card == null) tile.setBackgroundColor(0xffd7ddd1.toInt())
            else {
                val image = ImageView(activity).apply { scaleType = ImageView.ScaleType.CENTER_CROP }
                tile.addView(image, FrameLayout.LayoutParams(-1, -1))
                loadCard(image, card.url)
                val number = text((index + 1).toString(), 12, true).apply {
                    setTextColor(Color.WHITE)
                    setBackgroundColor(0x99000000.toInt())
                }
                tile.addView(number, FrameLayout.LayoutParams(dp(26), dp(30), Gravity.TOP or Gravity.START))
                tile.contentDescription = "story-card-" + index
                tile.setOnClickListener { showCardCarousel(cards, index) }
            }
            cardsGrid.addView(tile, params)
        }
    }

    private fun showCardCarousel(cards: List<StoryCardWire>, start: Int) {
        val dialog = Dialog(activity, android.R.style.Theme_Black_NoTitleBar_Fullscreen)
        val frame = LinearLayout(activity).apply {
            orientation = VERTICAL; setBackgroundColor(Color.BLACK)
            // The full-screen window draws behind Android navigation. Keep carousel controls visible.
            setPadding(0, dp(8), 0, dp(42))
        }
        val photo = ImageView(activity).apply { scaleType = ImageView.ScaleType.FIT_CENTER }
        frame.addView(photo, LayoutParams(-1, 0, 1f))
        val controls = LinearLayout(activity).apply { gravity = Gravity.CENTER }
        val previous = Button(activity).apply { text = "‹"; contentDescription = "card-previous" }
        val counter = text("", 16, false).apply { setTextColor(Color.WHITE) }
        val next = Button(activity).apply { text = "›"; contentDescription = "card-next" }
        val close = Button(activity).apply { text = "Закрыть"; contentDescription = "card-close" }
        controls.addView(previous)
        controls.addView(counter, LayoutParams(0, -2, 1f))
        controls.addView(next)
        controls.addView(close)
        frame.addView(controls)
        dialog.setContentView(frame)
        var current = start
        fun display() {
            counter.text = (current + 1).toString() + " / " + cards.size
            previous.isEnabled = current > 0
            next.isEnabled = current < cards.lastIndex
            loadCard(photo, cards[current].url,
                maxOf(activity.resources.displayMetrics.widthPixels, activity.resources.displayMetrics.heightPixels))
        }
        previous.setOnClickListener { if (current > 0) { current--; display() } }
        next.setOnClickListener { if (current < cards.lastIndex) { current++; display() } }
        close.setOnClickListener { dialog.dismiss() }
        dialog.show()
        display()
    }

    private fun loadCard(image: ImageView, url: String, targetPixels: Int = tileWidth()) {
        val key = "card:" + url + ":" + targetPixels
        image.tag = key
        thumbCache.get(key)?.let { image.setImageBitmap(it); return }
        image.setImageDrawable(null)
        val base = config.backendUrl ?: return
        val token = config.deviceToken ?: return
        if (workers.isShutdown) return
        workers.execute {
            val bytes = runCatching { ApiClient(base, token).readAsset(url) }.getOrNull() ?: return@execute
            val info = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeByteArray(bytes, 0, bytes.size, info)
            val maxDimension = maxOf(info.outWidth, info.outHeight).coerceAtLeast(1)
            var sample = 1
            while (maxDimension / sample > targetPixels * 2) sample *= 2
            val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size,
                BitmapFactory.Options().apply { inSampleSize = sample }) ?: return@execute
            thumbCache.put(key, bitmap)
            activity.runOnUiThread { if (image.tag == key) image.setImageBitmap(bitmap) }
        }
    }

    private fun bindImage(image: ImageView, uri: String) {
        image.tag = uri
        thumbCache.get(uri)?.let { image.setImageBitmap(it); return }
        image.setImageDrawable(null)
        if (workers.isShutdown) return
        workers.execute {
            val bitmap = runCatching {
                if (uri.startsWith("content:")) activity.contentResolver.loadThumbnail(
                    Uri.parse(uri), Size(240, 240), null,
                ) else ImagePreviewDecoder.decode(activity, uri, 240, 240)
            }.getOrNull() ?: return@execute
            thumbCache.put(uri, bitmap)
            activity.runOnUiThread { if (image.tag == uri) image.setImageBitmap(bitmap) }
        }
    }

    private inner class CandidateAdapter : BaseAdapter() {
        override fun getCount() = candidates.size
        override fun getItem(position: Int) = candidates[position]
        override fun getItemId(position: Int) = position.toLong()
        override fun getView(position: Int, reused: View?, parent: ViewGroup): View {
            val frame = (reused as? FrameLayout) ?: FrameLayout(activity).apply {
                layoutParams = AbsListView.LayoutParams(-1, dp(102))
                addView(ImageView(activity).apply {
                    scaleType = ImageView.ScaleType.CENTER_CROP
                }, FrameLayout.LayoutParams(-1, -1))
                addView(text("", 12, true).apply {
                    setTextColor(Color.WHITE); setBackgroundColor(0x99000000.toInt())
                }, FrameLayout.LayoutParams(-1, dp(26), Gravity.BOTTOM))
            }
            val candidate = getItem(position)
            bindImage(frame.getChildAt(0) as ImageView, candidate.uri)
            val chosen = selected.any { it.photoId == candidate.id }
            (frame.getChildAt(1) as TextView).text = listOfNotNull(
                if (chosen) "✓" else null,
                if (candidate.source) "Исходное" else null,
            ).joinToString(" ")
            frame.contentDescription = (if (candidate.source) "source-photo" else "nearby-photo-" + position) +
                (if (chosen) "-selected" else "")
            frame.setOnClickListener { toggle(candidate) }
            return frame
        }
    }

    private fun tileWidth() = (activity.resources.displayMetrics.widthPixels -
        dp(18 * 2 + 10 * 2 + 18)) / 3

    private fun text(value: String, size: Int, bold: Boolean): TextView = TextView(activity).apply {
        text = value
        textSize = size.toFloat()
        setTextColor(0xff242822.toInt())
        typeface = if (bold) Typeface.DEFAULT_BOLD else Typeface.DEFAULT
        setPadding(dp(4), dp(7), dp(4), dp(7))
    }

    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()
}
