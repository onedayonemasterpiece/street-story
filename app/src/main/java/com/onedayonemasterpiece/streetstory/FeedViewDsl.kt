package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.Display
import android.widget.LinearLayout
import android.widget.TextView

/** Small programmatic-view primitives shared by the feed renderer. */
internal fun Context.surface(fill: Int, radiusDp: Int): LinearLayout {
    val density = resources.displayMetrics.density
    return LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        background = GradientDrawable().apply {
            setColor(ColorStateList.valueOf(fill))
            cornerRadius = radiusDp * density
        }
    }
}

/**
 * Inside LinearLayout.apply { ... }, Android's View.display is the nearest implicit
 * `display` receiver. Keep that DSL call deterministic and render with the intended
 * Street Story display face rather than leaking the platform Display object into UI code.
 */
@Suppress("UNUSED_PARAMETER")
internal fun LinearLayout.label(value: String, sp: Int, color: Int, viewDisplay: Display): TextView =
    TextView(context).apply {
        text = value
        textSize = sp.toFloat()
        setTextColor(color)
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        includeFontPadding = false
    }
