# Street Story: historical depth and editorial use, 16 October 2026

## Короткий вывод

Проблема подтверждается на уровне результата: несколько сведений об одной современной теме доступности поданы как история здания и повторены на картинке и в подписи. Довоенность подтверждается источниками, но точные дата постройки, архитектор и первоначальное назначение пока не установлены. Это не разрешает дописывать их по внешнему виду.

В текущем коде уже есть требования исторического поиска, сохранения источников, переиспользования POI и запрет подменять дату события датой статьи. Простого добавления таких инструкций недостаточно. Найден конкретный редакционный механизм: без явного поднабора визуал получает все выбранные факты и обязан отразить каждый; сохранённый текст публикации в обычный визуальный brief не передаётся. Исправлять в первую очередь стоит согласованное распределение содержания между текстом и изображением через уже существующий выбор поднабора, сохраняя фактическую базу и решения автора.

Причина потери более ранней истории именно в этой попытке пока не доказана: нет связки скриншота с точной записью истории, исходными ответами исследования и выбранными фактами. Ниже это отделено от подтверждённых механизмов кода. Изменений продукта и новых платных прогонов не было.

## Finding and scope

The reported output gives the reader one narrow modern accessibility story, packaged as several facts and repeated in the image and caption. The output does not justify its opening promise of an “interesting history.” This is an editorial-depth failure, but available evidence does **not** establish that the product found no earlier history, lost it in verification, or ignored a richer owner-approved selection. Those are different failure locations requiring the exact story ledger.

Read-only audit. No code, deployment, provider research, new model run, or production record was changed. Registered backend health read at 2026-10-16 05:22:28 UTC returned deployed SHA `0ffa7276a0b88595966b0aae65ec876a9651f5fd`. Code references below use that immutable revision. GitHub `main` at `61783a3e9092d43958bd647db566501d3949cf8d` is the documentation lineage, not the deployed product code. The ordinary DevCoveer checkout is a dirty, different branch and was not used as runtime evidence.

Observation unit: one owner-supplied screenshot, with caption and generated image; no population failure rate or historical recall estimate is supported. The screenshot's generating revision, story ID, selected fact IDs, research receipts, and actual image prompt remain unjoined to this observation. Current code establishes mechanisms, not retrospective execution of those mechanisms.

The screenshot already explicitly says the building is prewar. The failure is therefore not omission of the word “prewar,” but the absence of a deeper supported account of its earlier life, together with repetitive presentation of modern accessibility details. Read-only Regional Knowledge POI registry search for `Калининград Генделя 6` returned no candidates; this search result does not prove that the Street Story database has no matching POI or historical facts. No eligible connected computer/remote execution environment was available for the exact database join.

## 1. Immediate editorial bottleneck: the visual consumes the same inventory by default

**Mechanism in current code.** `live.py:5834–5887`, `_generate_visual`, defaults omitted `fact_ids` to all eligible owner-selected facts. `mvp.py:42–65`, `_visual_brief`, forwards their full text and tells the image model to express **each** selected fact. The versioned `backend/prompts/street-story-image-v6.txt` repeats that duty: express every selected claim in a reader-facing note. `mvp.py:325–344` freezes selected facts, concept and visual instruction, but not the saved caption. The ordinary brief does not receive the actual caption either.

Meanwhile `live.py:826–835` tells the editor to avoid card recital, make the caption add meaning beyond image labels, and not include a fact merely to consume the selection. The requirements are individually sensible but are not represented as a shared allocation between image notes and prose. The image model cannot compare its notes against a caption it never receives. This explains a structural tendency toward the observed redundancy; the exact run still needs its frozen prompt.

**Strong counterevidence.** The product already supports a visual subset of the owner selection. `backend/tests/test_editorial_utilization.py` verifies that a subset preserves the full selection and saved draft, and rejects an unselected fact. Mira also sees the draft in its context (`live.py:2708`). Therefore this is not proof that complementary composition is impossible, and a new fact-selection subsystem is unnecessary.

**First independent improvement, proposed.** Use the existing editorial owner and existing `generate_visual(fact_ids=...)` subset as the one decision point for distribution of meaning. Make the subset intentional and persist it through the existing frozen context; remove the duplicate presumption that all publication-selected claims must become separate image notes. If the caption is used by image composition, include its exact revision in the frozen input so stale handling remains honest. Keep evidence IDs, owner authorization, qualifications and UNKNOWN/idempotency protections. The tradeoff is fewer individually represented facts in the image, without deleting them from the ledger or owner selection. Do not enforce a numeric fact quota or fabricate historical material to create variety.

## 2. Historical depth: existing prompt coverage is stronger than this output

**Mechanism and counterevidence.** This release already explicitly requires substantive historical exploration:

- `native_fact_research.py:50–122`: independent discovery and own reads, historical-name hypotheses, detail links, failures distinguished from absence of history, and optional scoped specialist help.
- `native_fact_contract.py:160–216`: investigate construction/architecture, address records, residents, organizations, events and later changes; assess novelty, exhausted directions and concrete gaps; save useful material already received; no count target.
- `fact_agents.py:382–394`: historical, architectural, people, organization and event gaps are part of the research goal.

Thus “add the word history to the prompt” is not an evidence-backed remedy. The missing observation is where the narrower output arose: discovery, actual receipt of a useful passage, claim extraction, admission/withholding, reuse, owner selection, or final editing.

**Proposed bounded investigation.** Join the screenshot to its story and original provider attempts. For each historically useful clause that was actually received, record whether it became a candidate, whether it was admitted, whether it was selected, and whether it appeared in the caption/image. Keep publication year distinct from event year. Read the original completion reason and remaining gaps instead of inferring exhaustion from a completed job or three checkboxes. Do not start another broad paid research run to reconstruct evidence that already exists.

If the stored researcher stopped with open worthwhile directions and available resources, repair that specific continuation/decision. If useful history was withheld, inspect its own source/physical-identity basis and local rejection. If history was eligible but unselected, the remedy is editorial choice with owner authorization, not weakening verification. Useful partial modern facts must remain available while earlier history is unresolved.

## 3. Dates and superficial multiplicity

**Observed screenshot.** The caption and image repeatedly use 2014 as the date for a clinic in adapted prewar premises and its four-storey/no-elevator condition, then 2017 for an outside lift. These are several independent factual attributes but largely one modern accessibility episode; they do not constitute broad historical coverage. Do not merge independent claims in the factual ledger just to improve prose. Group them editorially into one point instead.

**Unresolved accuracy issue.** “In 2014 it already operated” can be an accurately dated observation. It must not become “opened in 2014,” “became a clinic in 2014,” or a construction date. The screenshot alone does not prove that such a false event-date conversion happened. `live.py:829–832` and the image template explicitly prohibit publication-date-to-event-date substitution. `native_fact_contract.py:91–110` likewise preserves each claim's own temporal qualifiers.

**Proposed check.** Compare the stored source passage, source date and final sentence, retaining “reported by/in” semantics only when needed to avoid false chronology. Remove unsupported introductory promises and generic interest claims; begin with the strongest specific supported detail. Exact construction date, architect or former use should remain unknown until supported. Architectural appearance alone is insufficient to invent them.

## 4. Reuse exists; a total memory failure is not established

**Mechanism in code.** `poi_memory.py:653–669` hydrates eligible canonical POI assertions and regional knowledge using the existing identity keys, with no default numeric limit. It preserves owner choice and the draft. Its admission checks prevent withheld or unsupported memory from being revived. `fact_agents.py:420–522` passes known story assertions, original source references, source-review history, prior discovery queries/leads, read outcomes and conflicts into native research. `native_source_memory.py` deduplicates inherited calls and distinguishes delivered chunks from semantic exhaustion.

**Important limitation.** The native source-memory query is scoped to this story, photo and identity generation. Cross-story reuse is primarily canonical fact hydration, not a universal replay of every earlier reader session. `previous_editorial_context` (`poi_memory.py:1134–1162`) contains up to three prior concepts and selected IDs, not complete previous captions or image lettering. This limits what can be inferred about repetition across posts, but does not prove this case lost history. The alternative `prior_facts` helper has a 60-row default; that is not evidence of a 60-row cap on normal hydration.

**Proposed check.** Read the exact accepted POI key/aliases and canonical assertions, compare them with story assertions before editing, and distinguish cold/warm runs. Reuse current eligible revisions without another model re-verification. Preserve genuine conflicts and subject boundaries. Do not add a second canonical memory store or globally remove evidence checks.

## 5. Acceptance coverage does not establish finished-post quality

`docs/acceptance/editorial-utilization.md` describes a rich-inventory test that asks for different angles and reviews selection diversity and source support. The actual image is only queued in the test store: no image generation or Telegram send occurs. `test_editorial_utilization.py` proves authorization/subset mechanics and validates a manually supplied semantic assessment; it does not prove historical depth, useful narrative structure, or nonduplication of actual image lettering and caption. `test_fact_temporal_context.py` explicitly says its controlled transports do not prove a model cure.

**Proposed minimum distinguishing checks, not run here:**

1. A rich eligible inventory with modern and earlier history: use the full inventory, respect owner choice, choose an intentional visual subset and complementary caption.
2. A poor inventory with only the modern episode: concise useful partial output, no invented early history or generic promise of depth.
3. A source published in 2014 reporting an older undated condition: no invented opening/construction year.
4. An earlier-history clause legitimately about a neighboring building or institution: preserve rejection locally while retaining valid facts about this building.
5. Warm POI reuse with a genuine revised/withheld claim: reuse unchanged admitted history, do not resurrect the withheld one, preserve selected IDs and UNKNOWN operations.

For the original screenshot, review the actual retained image and caption together; queuing a visual is not successful visual acceptance. These are localized checks on the proposed editorial slice, not a new all-or-nothing product gate.

## Remaining evidence needed

Exact story ID and source/selection/editorial lineage: `stories.research_json`, `visual_context_json`, `draft_text`; `facts` and `fact_assertions`; `research_runs` and `research_provider_attempts`; relevant immutable source versions/chunks; canonical POI assertions/aliases; `live_commands`, `live_editor_state` and available publication snapshot. Without this join, claims that a named validator discarded the prewar past, that search stopped too early, or that memory failed would be hypotheses.

The recommended first deliverable is the existing-editorial-path correction plus a retained paired-output check. Research-depth changes should follow the exact saved failure, not a speculative rewrite of an already extensive research contract.

## Immutable code reference root

All code and tests above: https://github.com/onedayonemasterpiece/street-story/tree/0ffa7276a0b88595966b0aae65ec876a9651f5fd

## Историческое приложение: Генделя, 6

Отдельная проверка открытых источников проведена 16 октября 2026 года. Она показывает, что исторический слой не следует объявлять отсутствующим. Новые находки аудита не считаются фактами, которые продукт действительно получил в исходной попытке.

- **Подтверждено первичным источником:** администрация Калининграда уже 25 июля 2008 года описывала поликлинику на улице Генделя как расположенную в приспособленном довоенном здании: https://www.klgd.ru/rss/index.php?ELEMENT_ID=31704 . Это более ранняя точка наблюдения, а не год постройки или открытия поликлиники.
- **Подтверждено первичным источником:** доклад уполномоченного по правам человека за 2014 год, печатная страница 33, PDF-страница 34, связывает адрес Генделя, 6 с приспособленными довоенными помещениями и проблемой доступности: https://ombudsman39.ru/wp-content/uploads/2015/04/doklad2014kaliningrad39upch1.pdf#page=34 . Год доклада не определяет возраст здания.
- **Подтверждено публикацией с атрибуцией:** статья 6 апреля 2017 года об установленном подъёмнике содержит слова главврача Кабисова о старом немецком здании и объясняет наружное размещение лифта: https://srv.kgd.ru/news/medicine/item/62553-v-poliklinike-na-ul-gendelya-ustanovili-lift-dlya-pozhilyh-lyudej-i-invalidov . Здесь есть осмысленная связь старого здания с поздней адаптацией, даже без неизвестной даты постройки.
- **Адресная проверка:** региональная программа оснащения 2025 года указывает амбулаторно-поликлиническое отделение больницы № 3 по адресу Генделя, 6: https://base.garant.ru/412585110/ . Это проверка идентичности учреждения/адреса, не доказательство первоначального назначения дома.

**Не подтверждено:** точный год строительства, архитектор, первоначальное назначение и дореволюционный номер конкретного дома. Нельзя переносить на него историю соседней консистории на Генделя, 5 / Ушакова, 6, судебно-тюремного комплекса Генделя, 1–3 или автоматически приравнивать историческую Händelstraße, 6/8 к современному номеру 6. Совпадение названия улицы и номера не является доказанной адресной преемственностью.

**Только поисковая зацепка:** газета ветеранов за 2016 год упоминает четырёхэтажное здание на Генделя, позднее поликлинику № 3, в связи с временным размещением сотрудников МГБ в 1946 году. Полный PDF не получен, номер дома не установлен: https://www.kfvpr.ru/sites/default/files/gazeta_6_-2016.pdf . Не включать это в пост как факт до чтения источника и проверки физической привязки.

Практический смысл: даже узкая подтверждённая база допускает честный угол «как довоенное здание приспосабливали к современной доступности». Она не даёт основания обещать развёрнутую историю дома. Если автор хочет именно раннюю историю, нужно явно сохранять этот пробел и исследовать исторические адреса/назначение, а не умножать перефразировки 2014 года.


## Дополнение: ложная связь между приютом Типольтов и банком

### Наблюдение, 16 октября 2026 года

Источник инцидента: скриншот владельца в Slack, сообщение `1792132601.437009`. На нём сначала указано строительство здания в 1890–1900 годах как сиротского приюта Типольтов, затем написано: «эти стены продолжают служить своему назначению: с 1947 года здесь работает Банк России». В окончании повторяется `#Калининград`.

Здесь есть **самостоятельная фактическая ошибка в редакционной связке**. Приют и банковское учреждение имеют разные назначения. Даже если обе исходные карточки по отдельности верны, утверждение о сохранении назначения из них не следует и противоречит перечисленной смене функций. Это не безобидный стиль и не недостаток количества фактов: редактор добавил новое отношение между ними без опоры. Нейтральный переход — «Позже назначение здания изменилось».

Точный story ID, generating SHA, исходный набор выбранных фактов и журнал редактора скриншота не получены. Поэтому наблюдаемую ошибку не приписываем конкретному вызову модели или ревизии, а появление 1947 года пока не локализуем между исследованием, карточкой и текстом.

### Сверка с первичным источником

[Официальная история калининградского отделения Банка России](https://www.cbr.ru/kaliningrad/history/) проверена 16 октября 2026 года:

- Подтверждены строительство сиротского дома в 1890–1900 годах и пожертвования аптекаря Типольта.
- Кёнигсбергская областная контора **Госбанка СССР**, организованная приказом от 28 сентября **1945** года № 89, размещалась в уцелевшей части бывшего сиротского дома.
- В 1946 году контора была переименована в Калининградскую; наименование Главного управления Центрального банка Российской Федерации относится к 1991 году.

Следовательно, 1947 год в скриншоте не подтверждается этим источником и расходится с указанным там началом банковского использования. Нельзя молча исправлять дату в продуктовой базе: сначала сопоставить собственные источники конкретной карточки и сохранить разбор расхождения. Название современного учреждения также не следует переносить на 1940-е годы без пояснения преемственности. Сам сайт использует современное обобщающее название в подписи к фотографии; для точной хронологии полезнее его основной текст с историческими названиями.

Пример точной короткой связи на основании этого источника: «Здание строили как сиротский приют Типольтов. После войны его назначение изменилось: в 1945 году в уцелевшей части разместилась контора Госбанка СССР». Это предложение для исправления, не выполненная правка опубликованного поста.

### Что меняется в приоритетах аудита

Этот пример повышает приоритет **проверки смысла готового текста**, независимо от полноты исторического поиска. В ранее рассмотренной версии `0ffa727`, `live.py:826–832` уже требует выводить каждое фактическое утверждение из выбранных фактов и не добавлять события/классификации редакционными переходами. Тем не менее наблюдаемый текст нарушает именно эту обязанность. Добавление ещё одной общей инструкции «не выдумывать» не является достаточным доказанным исправлением.

Минимальное универсальное направление для Claude:

1. В существующем шаге композиции проверять не только выписанные даты/имена, но и добавленные отношения: сохранение назначения, изменение функции, причинность, непрерывность, одновременность и преемственность учреждения. Это работа существующего семантического редактора, а не новый обязательный независимый judge.
2. На сохранённом примере различить исходную ошибочную карточку и ошибку соединения корректных карточек. В первом случае локально исправлять утверждение/дату по своим источникам; во втором — переписать только редакционную связь, не обнуляя пригодный фактологический набор.
3. Сохранить историческое название учреждения и его период отдельно от современного названия. Нельзя выводить отсутствие смены функции из непрерывности существования самого здания.
4. В существующем механизме окончания публикации обеспечить единственное добавление адреса/городского хештега и проверить финальный provider-bound текст. Не удалять произвольные пользовательские хештеги по всей публикации. `publication_content.py` на `b06deb0` только компилирует именованные ссылки и сохраняет остальной текст; само по себе это не доказывает, где возник повтор.

**Минимальные различающие проверки, предложены, не запущены:** приют→банк требует смены функции; приют→приют допускает сохранение функции при наличии временной опоры; старое→современное название одной организации требует подтверждения преемственности; разные даты источников остаются адресным конфликтом; повторная сборка служебного окончания не удваивает городской хештег. Проверять итоговый текст, а не только наличие фраз в системной инструкции.

Поправки `8219c9e`, `8788a20`, `b06deb0` к уточнению адреса, окружению контура и пропорциям визуала этот редакционный дефект не исправляют. Их CI или успех отдельных сценариев 105/122 не заменяет проверку данного примера.


## Дополнение: доверие к тестовым привязкам фото 105 и 144

### Главный вывод и предел проверки

В опубликованном реестре обнаружена неподтверждённая привязка фото 105, которую acceptance harness способен превратить в имитацию подтверждённого владельцем контура. Это **механизм в коде и состояние опубликованной фикстуры**, а не доказательство неверного выбора в последних ночных прогонах. Для фото 144 опубликованная запись вообще не содержит физического ID, необходимого этому runner. Поэтому сначала нужно сверить фактически замороженный реестр и runner ночного запуска, а не объяснять отсутствие результата недостатком сведений от владельца.

Проверка ограничена доступными версиями GitHub. Локальный `research-stop-join-20261016.json` ночного запуска прочитать не удалось: доступный read-only probe допускает только пути относительно репозитория и отклонил абсолютный путь артефакта. Сведения о четырёх из шести остановках с `research_sufficient=false`, 78 секундах исследования Генделя, 6 и оставшейся квоте здесь **не проверены независимо**. Нет прочитанной полной цепочки фактического ночного запуска: фото → выбранный контур → теги/адрес → поисковые запросы.

### Что точно записано в опубликованном реестре

Источник: [photo-registry.json, неизменяемая версия 61783a3e](https://github.com/onedayonemasterpiece/street-story/blob/61783a3e9092d43958bd647db566501d3949cf8d/docs/testing/photo-registry.json). Записи можно найти по ключам `ss-tg-2-105` и `ss-tg-2-144`.

**105:**
- Оригинал: `20261007_115605.jpg`, SHA-256 `cc0dbb5ef8adf7266eae4a6ade70837672266bc33192e59e32450c235ab9b23b`.
- GPS: `54.714790399722226, 20.5081984`; `owner_hint` не документирован.
- `expected.status = historical_research_label`; `object_or_address = Университетская, 3`; `physical_ids = []`; `research_osm_ref = way/105154961`.
- `evaluation_status = frozen_evaluator_truth_unresolved`: запись сама не заявляет независимо установленный эталон физического здания.

[Исторический cases.json, версия 9e7e511d](https://github.com/onedayonemasterpiece/street-story/blob/9e7e511d0901a799c14ac6ab82b17dd41efa2d1b/docs/research/20261008-prussia-text/cases.json), case 105, дополнительно описывает кандидата ранга 4, расстояние до границы 39,11 м и выбор с использованием ранее известной идентичности (`exif_osm_address; candidate chosen with previous-known identity`). Там `geometry_identity_preserved=false`. Это не независимое слепое подтверждение соответствия фотографии контуру.

**144:**
- Оригинал: `20261012_123311.jpg`, SHA-256 `9c6068f4e659be40481baf794918103393161109cd6f706dd82c63cb38095862`.
- GPS: `54.721568699722226, 20.47810409972222`.
- Подсказка владельца из подписи документа: «Шиллера 2, Калининград», источник в реестре: [сообщение 144](https://t.me/c/4488229487/2/144).
- `expected.status = owner_supplied_address_not_independently_verified`; `physical_ids = []`; `research_osm_ref` отсутствует. Адресная подсказка есть; независимая привязка к физическому OSM ID не установлена.

Существование здания на карте, наличие адресных тегов у конкретного контура, соответствие этого контура фотографии и достоверность тестового expected ID — четыре разных утверждения. Приведённые записи не позволяют подменять одно другим.

### Как неподтверждённая зацепка превращается в тестовый выбор

В [owner_place_acceptance.py:243–269 на 0ffa7276](https://github.com/onedayonemasterpiece/street-story/blob/0ffa7276a0b88595966b0aae65ec876a9651f5fd/backend/tools/owner_place_acceptance.py#L243-L269) загрузчик `registry_item` проверяет SHA оригинала, берёт `expected.physical_ids` и добавляет туда `research_osm_ref` в нормализованном формате. Он не требует подтверждённого статуса expected/evaluator truth. Совпадение байтов фотографии защищает от подмены входа, но не доказывает правильность ожидаемого здания.

Далее [строки 523–551 той же версии](https://github.com/onedayonemasterpiece/street-story/blob/0ffa7276a0b88595966b0aae65ec876a9651f5fd/backend/tools/owner_place_acceptance.py#L523-L551) выбирают feature по этим ID, формируют exact-selection с её snapshot/candidate и передают обычному API подтверждения места. Таким образом, исследовательская зацепка из фикстуры может получить статус имитированного выбора владельцем. Тот же участок присутствует в [версии b06deb039](https://github.com/onedayonemasterpiece/street-story/blob/b06deb0390206fc1fafa8501137b8691780fc56e/backend/tools/owner_place_acceptance.py#L243-L269).

**Сильнейшее ограничение вывода:** для опубликованной записи 144 этот загрузчик должен остановиться на проверке отсутствующего ID (строки 264–265). Если ночной сценарий 144 прошёл дальше, использовался другой/изменённый реестр либо другой путь запуска. Нельзя объявлять нынешний прогон неправильным только по опубликованной старой записи. И успешный выбор ID из этой фикстуры не является доказательством, что пользователь выбрал бы тот же дом на фотографии.

### Минимальная следующая проверка для Claude, без новых запусков

1. В сохранённом `run.json` каждого сценария сверить `registry_sha256`, `harness_sha256`, `source_hashes`, SHA оригинала, физические ID, `author_selection`, режим `revision14` и qualification. Эти данные runner уже сохраняет: [строки 447–459](https://github.com/onedayonemasterpiece/street-story/blob/0ffa7276a0b88595966b0aae65ec876a9651f5fd/backend/tools/owner_place_acceptance.py#L447-L459). Сравнивать именно фактическую замороженную фикстуру, а не текущий изменяемый файл.
2. Для 105 и 144 сопоставить исходное фото с сохранёнными `map_response`, `confirmation_body`, ответом подтверждения и selected ID: контур, его геометрию, теги, адресные объекты и видимый фасад. Отдельно отметить, выбор сделан владельцем или подставлен runner из expected ID.
3. Затем прочитать фактически отправленные поисковые запросы и их основание. Различить неверный контур, верный контур без адресного тега, неверную адресную гипотезу и корректный адрес с недостаточной исторической выдачей. Для 105 отдельно сохранить исходный исход и исход после уточнения «Профессора Севастьянова, 3»: исправление человеком не доказывает автономный успех исходной привязки.
4. Только после этой сверки оценивать пользу вопроса владельцу. Уточнение может быть правильным продуктовым действием при реальной неоднозначности, но не должно превращать дефект эталона/runner в положительный результат теста.

**Минимальное изменение, предложено:** убрать автоматическое повышение `research_osm_ref` до доверенного owner-selection oracle. Оставить его поисковой зацепкой; существующий реестр должен явно различать независимо проверенный физический expected ID и неподтверждённый исследовательский label. Для второго можно проверять механику исполнения, но нельзя заявлять правильность определения здания. Это локальная коррекция тестового контракта, без новой продуктовой системы, без жёстко заданных адресов и без ослабления проверки источников.

Потеря при таком упрощении: некоторые прежние сценарии перестанут называться проверкой точности, пока их эталон не подтверждён. Сохраняются сами фотографии, поисковые зацепки, пригодные результаты и возможность проверять интерфейс/переходы состояния. Предложенные различающие проверки: неподтверждённая зацепка не становится эталоном; подтверждённый ID проходит; отсутствие ID у 144 даёт честный статус неподтверждённой фикстуры; существующий, но неверный соседний контур не считается успехом. Изменений и запусков этих проверок в рамках аудита не было.
