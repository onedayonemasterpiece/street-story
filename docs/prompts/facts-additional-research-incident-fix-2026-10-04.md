# Street Story — additional facts incident fix

Дата: 2026-10-04

Исполнительная задача. Не новый аудит и не новая архитектура.

## Контекст

Production incident: `inc_1f9a6773b5487068d183632a`.

Текущий production:
- branch: `work/street-story-mvp-20260908`
- deployed SHA: `747cc8d14bdabff81fa04b109ca66433e891d033`
- PR #128 уже merged/deployed.

Физический пользовательский тест:
- story: `story_0f6ff98f009cafcaf55690a6`
- POI: `wiki:403645` / Королевские ворота
- запрос: «Найди дополнительно ещё фактов о Королевских воротах. Используй уже накопленные факты и источники, ищи новое или недостающее. Не готовь текст публикации и ничего не выбирай за меня.»

Фактическая production-телеметрия после PR #128:
- `search_web` вернул 10 discovery sources, `facts=[]`;
- Live действительно пошла в `get_research_chunk -> save_research_facts`;
- выбранный full source `kaliningradinfo.ru` оказался в основном навигацией/меню;
- 7 durable chunk batches были сохранены как `facts=[]`, chunk завершился `no_claims`;
- story facts остались `6 -> 6`;
- POI assertions/observations/sources не изменились;
- run: `research_bfc13782b0f41f7c43414f9d`;
- terminal state: `partial / live_answer_partial`;
- после этого Mira голосом сообщила новый факт про 1843 год и Фридриха-Вильгельма IV;
- этот факт находился только в discovery search snippet Tripster:
  «Королевские ворота снесли, а вместо них в 1843 году решили построить новые. На закладке первого камня присутствовал лично король Фридрих-Вильгельм IV.»
- этот snippet не был сохранён как durable fact.

Итого: PR #128 исправил ранний обрыв «search -> voice», но остался provenance defect:
**несохранённый discovery snippet может попасть в голосовой factual answer после пустого full-source path.**

## Уже существующие возможности — не строить заново

`save_research_facts` уже умеет сохранять discovery evidence напрямую по:
- exact `source_ref`;
- exact `evidence_ref`;
- LLM verdict / atomic / support_complete / qualifiers_preserved;
- `selected=false`.

Не создавать:
- новый storage;
- новую БД/таблицу;
- новый reviewer;
- новую semantic helper model;
- новый graph/service;
- новый research framework.

Сохраняем:
- Live-first;
- LLM-first semantics;
- существующую POI-memory;
- product-first partial value.

## Что изменить

### 1. Snippet-first normal path

После `discovery_only search_web`:

- Mira сначала рассматривает returned search snippets как дешёвый evidence batch;
- если конкретный snippet сам полностью поддерживает новый атомарный факт:
  - вызвать `save_research_facts` сразу;
  - использовать exact `source_ref + evidence_ref`;
  - `verdict=supported`;
  - `atomic=true`;
  - `support_complete=true`;
  - `qualifiers_preserved=true`;
  - краткий `review_reason`;
  - `selected=false`.
- если snippet недостаточен — только тогда читать full source через `get_research_chunk`.
- несохранённый snippet нельзя озвучивать пользователю как найденный факт.

### 2. Empty source -> next source

Если full source дочитан и завершён как `no_claims`, а для текущего run:
- новых durable observations == 0;
- остаются unfetched non-failed discovery sources;

то не завершать run голосовым ответом.

Продолжить `get_research_chunk(run_id only)` со следующим источником.

Bounded MVP:
- максимум 3 full-source attempts на один пользовательский запрос;
- не строить exhaustive crawler.

Если после трёх источников новых durable facts нет:
- корректный product outcome: «новых подтверждённых фактов не нашла»;
- никаких factual claims из unsaved snippets.

### 3. Guard against premature get_facts/final answer

Пока active Live-first research run имеет:
- 0 new durable observations;
- remaining unfetched non-failed sources;
- full-source attempts < 3;

`get_facts`/factual completion не должны использоваться как способ закончить запрос.

Вернуть явный `ConflictError` с next action: `get_research_chunk`.

После появления хотя бы одного нового durable fact:
- product-first partial value разрешён;
- не требовать обходить весь corpus.

### 4. Model instruction

Явно закрепить:

- sufficient discovery snippet -> save immediately;
- insufficient snippet -> read full source;
- never speak unsaved discovery snippet as a fact;
- after `no_claims` source, try next bounded source if there are still zero new durable facts.

### 5. Semantic wording

Сохранять модальность и время источника:
- «стоимость составит» / planned / expected / estimated не превращать в actual/final «обошлось», «стоило», «составило» без отдельного evidence.

Не исправлять старый claim про 1,5 млн прямым SQL. В этой задаче достаточно не создавать новые claims с таким overclaim.

## Regression tests

Минимум:

1. `search sources + facts=[]` -> projected result сначала допускает/предлагает snippet-first `save_research_facts`, а не forced document-only.
2. first fetched source = `no_claims`, 0 new run observations, остаются sources -> continuation queues `get_research_chunk`, не `partial`.
3. в том же состоянии `get_facts` отклоняется кодом `live_research_more_sources_required`.
4. после хотя бы одного new durable observation product-first partial completion разрешён.
5. direct snippet-save реально сохраняет один новый evidence-backed fact и `selected=false`.
6. regression на modality: future/projected cost не нормализуется как actual cost.

## Проверки

Последовательность:
1. focused facts tests;
2. full backend regression;
3. маленький PR против `work/street-story-mvp-20260908`;
4. CI;
5. merge;
6. deploy exact merge SHA;
7. production canary на **той же** story:
   `story_0f6ff98f009cafcaf55690a6`.

Повторить тот же user request.

## Acceptance

PASS только если на production:

### Вариант A
- появился хотя бы один NEW evidence-backed durable fact;
- story/POI-memory реально выросла;
- новый факт доступен в facts projection;
- Mira не озвучивает unsaved claims.

### Вариант B
- bounded источники реально проверены;
- новых фактов действительно нет;
- Mira прямо сообщает, что новых подтверждённых фактов не найдено;
- factual claims из unsaved snippets отсутствуют.

FAIL:
- run снова `live_answer_partial`;
- 0 new durable observations;
- Mira при этом сообщает новый исторический факт из search snippet.

## Не трогать

- Android UI;
- auth/device token;
- publication/VibePublish;
- DB schema;
- requirements contract;
- historical graph;
- unrelated Brandenburg/golden-fixture identity defect;
- owner selections;
- unrelated cleanup.

## Результат

Не заканчивать задачей «код написан» или «CI зелёный».

Вернуть:
- PR;
- merge SHA;
- deployed SHA;
- production canary before/after counts;
- какие новые facts реально сохранились;
- incident status.

Если production canary FAIL — инцидент не закрывать и не объявлять продукт исправленным.
