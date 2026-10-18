package com.onedayonemasterpiece.streetstory

import android.text.Spanned
import androidx.core.text.HtmlCompat

/** Safe, small Markdown subset for Mira replies. */
internal object MarkdownLite {
    private val heading = Regex("^#{1,3}\\s+")
    private val unordered = Regex("^[-*]\\s+")
    private val link = Regex("\\[([^]\\n]+)]\\((https://[^)\\s]+)\\)")
    private val bold = Regex("\\*\\*([^*\\n]+)\\*\\*")
    private val code = Regex("`([^`\\n]+)`")
    private val italic = Regex("(?<!\\*)\\*([^*\\n]+)\\*(?!\\*)")

    fun render(markdown: String): Spanned =
        HtmlCompat.fromHtml(toHtml(markdown), HtmlCompat.FROM_HTML_MODE_LEGACY)

    internal fun toHtml(markdown: String): String =
        markdown.replace("\r\n", "\n").replace('\r', '\n').split('\n').joinToString("<br>") { raw ->
            var line = escape(raw.trimEnd())
            line = when {
                heading.containsMatchIn(line) -> "<b>" + heading.replace(line, "") + "</b>"
                unordered.containsMatchIn(line) -> "• " + unordered.replace(line, "")
                else -> line
            }
            line = link.replace(line) { match ->
                "<a href=\"${match.groupValues[2]}\">${match.groupValues[1]}</a>"
            }
            line = bold.replace(line, "<b>$1</b>")
            line = code.replace(line, "<code>$1</code>")
            italic.replace(line, "<i>$1</i>")
        }

    private fun escape(value: String): String = value
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
}