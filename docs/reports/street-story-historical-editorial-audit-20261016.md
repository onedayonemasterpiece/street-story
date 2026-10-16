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
