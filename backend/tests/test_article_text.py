from street_story.providers import _read_article_text


def test_wikipedia_body_keeps_infobox_history_and_citations_without_chrome():
    html = """<body><nav>Главное меню Выбор языка</nav><main>
      <h1>Ворота</h1><aside>Настройки сайта</aside>
      <div class="mw-parser-output"><table><tr><td>Памятник федерального значения</td></tr></table>
      <p>Построены в 1848–1853 годах.</p><h2>История</h2>
      <p>Архитектор: автор проекта.<sup>[1]</sup></p>
      <ol><li>Архивная публикация, страница 42.</li></ol></div></main>
      <footer>Политика сайта</footer></body>"""
    text, limited = _read_article_text(html)
    assert not limited
    assert "Главное меню" not in text and "Настройки сайта" not in text
    assert "Политика сайта" not in text
    assert all(value in text for value in (
        "федерального значения", "1848–1853", "Архитектор", "[1]", "страница 42",
    ))


def test_all_articles_survive_with_attribution_and_nested_structure():
    html = """<header>Меню сайта</header><article><header>Автор и дата</header>
    <p>Первое свидетельство.</p><article><p>Вложенное свидетельство.</p></article>
    <footer>Источники первого.</footer></article>
    <article><p>Второе свидетельство.</p></article><footer>Служебные ссылки</footer>"""
    text, _ = _read_article_text(html)
    assert "Меню сайта" not in text and "Служебные ссылки" not in text
    assert text.count("Вложенное свидетельство") == 1
    assert all(value in text for value in ("Автор и дата", "Источники первого", "Второе свидетельство"))


def test_unmarked_html_preserves_evidence_and_reports_partial_text():
    text, limited = _read_article_text("<body><div>" + "Документированный факт. " * 200 + "</div></body>", limit=1000)
    assert limited
    assert len(text) <= 1000
    assert text.startswith("Документированный факт.")


def test_main_retains_every_article_and_its_title():
    text, _ = _read_article_text("<main><h1>Объект</h1><article><p>Факт А.</p></article><article><p>Факт Б.</p></article></main>")
    assert all(value in text for value in ("Объект", "Факт А.", "Факт Б."))
