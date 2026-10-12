# Street Story facts — ретроспектива Live-first разработки 2026-10-02 → 2026-10-04

Статус: **обязательный контекст перед следующей реализацией facts flow**.

Эта ретроспектива не является новой архитектурой и не требует повторить уже выполненную работу. Её задача — не дать следующему исполнителю начать с чистого листа, забыть проверенные решения или снова пройти уже известные тупики.

## 1. Исходная продуктовая гипотеза

Исходная гипотеза Street Story по фактам была прагматичной:

- пользователь разговаривает с Мирой через `gemini-3.8-live`;
- Live-модель относительно слабая, зато предполагается очень дешёвой / практически безлимитной для нашего сценария;
- поэтому вместо одной большой «умной» операции ей можно дать **много маленьких, простых, ограниченных semantic operations**;
- search/fetch даёт ей материал;
- Live небольшими шагами извлекает факты, связывает их с evidence и постепенно накапливает полезный набор;
- цель MVP — **полезный работающий продукт**, а не идеальный recall/precision.

Эта гипотеза не была доказана полностью, но **за последние сутки она также не была опровергнута**. Были реальные ненулевые результаты Live. Архитектура начала дрейфовать раньше, чем был проведён честный product-level Live-first эксперимент до конца.

С 2026-10-04 эта гипотеза защищена требованиями:
- `CORE-LIVE-FIRST-FACT-RESEARCH`;
- `CORE-PRODUCT-FIRST-PARTIAL-VALUE`;
- `CORE-SHARED-POI-HISTORY-GRAPH`.

### 1.1. Накопление знаний — не в story

Нужно различать два слоя:
- `story` — текущая публикация: выбранные факты, editorial angle, draft, visual;
- общий серверный `POI/history graph` — каноническая накопительная память региона.

Street Story — клиент и producer этого общего слоя, а не его story-local замена. Новая история должна сначала переиспользовать накопленные POI claims/sources/aliases/relations и только затем искать новые источники или закрывать пробелы.

Новый research должен обогащать общий граф:
- POI и алиасы;
- evidence-backed claims и источники;
- личности;
- исторические события/сюжеты;
- связи POI ↔ person ↔ event/thread и другие подтверждённые source-backed relations.

Regional Knowledge Base поставляет provenance-rich evidence в этот слой. Обратная синхронизация Street Story → Regional Knowledge Base **не является требованием**.

Физически текущие POI tables пока находятся рядом со Street Story backend; это implementation detail, а не основание считать знания собственностью одной story или приложения.

## 2. Что реально удалось доказать

### 2.1. Live умеет извлекать полезные факты — результат не нулевой

Сохранённый ordinary targeted-run Королевских ворот дошёл до:
- 3 отдельных eligible facts по трём фасадным фигурам;
- 2/2 обработанных cores;
- completed run;
- 119.59 s;
- последующая ручная проверка собственных evidence признала эти три связи корректными.

Это не полноценный продуктовый acceptance, но это прямое доказательство, что Live-first semantic work **может работать**, а не нулевая гипотеза.

Broad-run на той же стадии дошёл до 18 eligible candidates за 140.03 s. Качество было неровным: часть тезисов была составной или недостаточно подтверждённой, а консервативная ручная оценка признала полностью атомарными и поддержанными 6/18. Но сам объём важен: проблема была не «Live ничего не умеет найти», а **precision/atomicity и orchestration вокруг результата**.

Holdout также завершался с 7 eligible facts за 94.09 s, хотя имел semantic defects и потерю корректного POI context.

Вывод: **не возвращаться к предположению, что Live бесполезна для facts extraction.**

## 3. Удачные решения — сохранить

### A. Компактный model-facing projection

PR #95 уменьшил реальный model-facing discovery result примерно с 8,618 до 2,976 символов (~65%) после наблюдаемого запроса на 11,066 resource units.

**Урок:** Live хорошо кормить короткими source refs/snippets/passages; durable богатые данные хранить на сервере. Не возвращать огромные структуры в каждый tool response.

### B. Один поисковый механизм на одну Live-сессию

PR #97 убрал одновременно включённые native Google Search и application `search_web`. Такая конфигурация была взаимоисключающей по SDK и дополнительно тратила ~23.7k startup grant.

**Урок:** не дублировать search modes. Live получает один понятный search tool / evidence path.

### C. Прямой semantic fallback показал ценность, но не должен становиться normal path по умолчанию

PR #96 появился после реального случая: Live выполнила три discovery searches, но не сделала второй `save_research_facts`, итог — 0 durable facts. Отдельная configured Gemini research model смогла превратить DuckDuckGo evidence в atomic facts и сохранить их без второго Live mutation.

**Урок:** это хороший **аварийный/опциональный механизм** и доказательство полезности server-side semantic helper, но не доказательство, что Live-first гипотеза провалилась. Сначала исправлять простоту Live choreography; helper оставлять как fallback, если измеренный blocker сохраняется.

### D. Immutable evidence и source versions

PR #109–#111 и #121 дали:
- append-only assertions/observations;
- exact evidence spans;
- frozen source versions/chunks;
- fact-specific evidence binding;
- исправление observation-specific evidence edges.

Это фундаментально полезно и не должно откатываться.

**Урок:** модель решает смысл; сервер хранит доказательства и не позволяет одному URL автоматически означать поддержку конкретного тезиса.

### E. Durable research run + resume

PR #110, #114, #118:
- research run имеет durable state;
- source/chunks не теряются;
- continuation может продолжать плотный chunk;
- уже сохранённые batches переживают поздний failure;
- повтор не должен начинать исследование заново.

Это полезно для Live-first подхода: слабой модели особенно нужен resumable small-step workflow.

### F. Пауза микрофона во время исследования

PR #112: research focus подавляет capture/VAD до terminal progress.

**Урок:** research — фоновая работа Миры внутри той же истории, но не должна конкурировать с пользовательским audio turn.

### G. Owner selection отделена от model review

PR #116 сделал owner selection авторитетным пользовательским состоянием и не позволил новым model observations стереть выбор пользователя.

Сохранять.

### H. Bounded provider phases и fail-soft to evidence

PR #122 и #123:
- native search phase ограничен во времени;
- при provider trouble система быстро переходит к public discovery;
- уже найденное durable evidence не выбрасывается;
- semantic outage не превращается в false success.

Это хороший product-first паттерн: **сохранять полезную работу и продолжать с тем, что уже есть**.

### I. Model-owned dedup/conflict вместо deterministic NLP

PR #113/#124 исправляли реальные semantic duplicate/conflict проблемы через LLM, не regex.

Сохранять принцип. Но full-inventory reconciliation не обязана блокировать выдачу полезных partial facts пользователю, если безопасно сохранить новый assertion как unresolved/potential duplicate.

## 4. Неудачные решения и реальные грабли — не повторять без нового условия

### A. Слишком длинная Live tool choreography

Практически наблюдалось:
- модель 15 раз читала тот же незавершённый core;
- сохранила только один batch;
- затем снова упёрлась в admission.

Это не доказательство слабости Live как semantic extractor. Это доказательство плохого orchestration contract: следующий маленький шаг должен быть однозначным и monotonic.

**Не повторять:** длинные цепочки, где Live должна сама угадывать следующий технический cursor/review stage.

### B. Premature continuation — собственная регрессия

Одна из доработок принимала промежуточный `turn_complete` после tool response за конец model answer и запускала continuation раньше времени. Это ухудшило работу и потребовало отдельного исправления.

**Урок:** provider/session boundary должен быть минимальным и стабильным. Не добавлять новую continuation механику без воспроизводимого перехода, который она исправляет.

### C. Harness bug принимался за проблему продукта

Была подтверждена ошибка доступа к adapter в acceptance harness.

**Урок:** прежде чем менять source из-за failed acceptance, определить, упал ли product transition или сам harness. Тестовая инфраструктура не является истиной о продукте.

### D. RESOURCE_TOKEN_BUDGET ошибочно трактовался как provider outage

Позже было доказано, что конкретный terminal denial произошёл **до отправки в Gemini**: в 60,000-unit lease было занято 53,667, оставалось 6,333 при запросе 6,673.

Это привело к лишней ветке расследования provider reliability.

**Урок:** сначала классифицировать boundary: local admission / provider / semantic / contract. Не чинить Gemini, если request до неё не дошёл.

### E. All-or-nothing completion стал важнее partial product value

После усиления correctness один спорный semantic verdict, incomplete coverage или review state мог удерживать весь product result. При этом уже существовали полезные facts.

Это противоречит продуктовой цели.

**Правило теперь:** плохой candidate withheld; хорошие source-backed facts доступны. Run может быть partial/resumable и всё равно давать пользователю ценность.

### F. Coverage reviewer из quality guard превратился в глобальный gate

PR #119 полезно исправил ложный coverage-positive и ввёл model-owned coverage items. Но дальнейшее использование полного coverage как условия «продукт вообще готов/не готов» стало слишком строгим для MVP.

**Сохранять:** coverage telemetry, explicit requested-aspect checks.
**Не делать:** отсутствие одного аспекта причиной скрыть весь хороший набор фактов.

### G. Full inventory semantic perfection попала в critical path

Полная reconciliation/conflict проверка большого inventory полезна для долговременной базы знаний. Но она стала частью синхронного product completion path и увеличила стоимость/сложность.

**Направление:** incremental/local semantic checks для новых facts; глубокую reconciliation можно продолжать resumably. Не задерживать доступ к уже безопасным evidence-backed facts.

### H. Repair/supersession начал становиться normal path

После broad semantic failures появились:
- bounded evidence repair;
- lineage;
- superseding review;
- independent helper assessment;
- повторные review packets.

Это полезно для **редкого пересмотра уже существующего факта**, но плохая нормальная модель создания нового факта.

Последний commit до этой ретроспективы уже сделал правильный разворот:
`0dda885… Form source-backed atomic claims before saving; reserve recovery for supersession`.

Не откатывать его.

### I. Non-Live helper начал молча менять исходную экономическую гипотезу

Helper с high reasoning улучшал отдельные диагностические decomposition checks, но full product replay всё равно мог дать 18 eligible с semantic errors и без repair lineage.

То есть наличие более сильной модели само по себе не решило продуктовую задачу.

**Не делать helper обязательной normal-path зависимостью без измеренного выигрыша.**

## 5. Что показали эксперименты именно про Live

### Работает лучше, когда

- operation маленькая и имеет один semantic objective;
- input — короткий chunk/passages, а не весь документ;
- model-facing payload компактный;
- evidence refs короткие и server-resolved;
- cursor/checkpoint монотонный;
- уже сделанная работа durable;
- следующий технический шаг однозначен;
- POI identity/location явно остаются в context;
- search и extraction разделены;
- ошибки одного candidate не требуют заново исследовать остальные;
- микрофон остановлен во время research work.

### Работает хуже, когда

- Live должна сама оркестрировать длинный технический workflow;
- ей приходится повторно читать один и тот же контекст;
- tool result слишком большой;
- одновременно активны конкурирующие search modes;
- нужно переписывать длинные IDs/digests;
- semantic и transport state смешаны;
- product asks for “perfect complete review” before exposing any useful result;
- helper/review/recovery stages добавляются один за другим без удаления старой обязательной choreography.

## 6. Что пока НЕ доказано

Не надо придумывать отсутствующие доказательства.

Пока не доказано:

1. что `gemini-3.8-live` действительно экономически безлимитна в абсолютном смысле — это исходная продуктовая гипотеза о доступной/низкой marginal cost модели, которую надо проверять по эксплуатационным ограничениям;
2. что текущий HEAD уже даёт normal real-Internet Live-first flow от запроса до UI/draft;
3. что Live-only normal path стабильно даёт нужный recall на разных POI;
4. что helper обязателен — такого доказательства нет;
5. что старый pre-audit вариант был лучше во всех отношениях — старые PASS receipts имели слабее evidence/atomicity semantics.

## 7. Baseline, относительно которого нельзя регрессировать

Следующая разработка не начинается с нуля.

| Baseline | Что доказано | Чего не делать снова |
|---|---|---|
| PR #95 | compact projection снижает model-facing payload ~65% | не возвращать большие raw structures |
| PR #96 | при пропущенном second-hop можно получить durable semantic facts напрямую из evidence | не делать helper обязательным только потому, что fallback работал |
| PR #97 | один app search mode дешевле/предсказуемее двойного native+app setup | не включать оба одновременно |
| #109–#111/#121 | immutable exact evidence исправляет реальные data-integrity defects | не откатывать к URL==support |
| #110/#114/#118 | resumable chunk/run processing сохраняет уже найденное | не начинать run заново после partial failure |
| #122/#123 | bounded search + cached evidence fail-soft дают быстрый полезный fallback | не сериализовать минуты provider retries |
| targeted 119.59s | Live может довести 3 atomic evidence-backed facts до completed | не заявлять, что Live “ничего не умеет” |
| broad 18 candidates / 140.03s | Live может выдавать объём; precision/atomicity остаются задачей | не принимать все 18 как корректные |
| offline negative replay | final commit умеет withheld плохие decisions; ошибка была выше, в semantic verdict | не переписывать persistence ради semantic ошибки |

## 8. Новый рабочий принцип

Нормальный facts flow:

1. Mira определяет canonical POI и читает уже накопленную POI/history memory.
2. Существующие eligible claims/sources/relations сразу доступны story projection.
3. Mira формулирует goal для **нового/недостающего** исследования.
4. Search/fetch получает новые источники.
5. Источник режется на небольшие стабильные chunks.
6. Live выполняет много маленьких extraction operations.
7. Каждый хороший candidate сразу имеет собственное evidence и, где применимо, entity/relation proposals для person/event/thread.
8. Mechanical validation сохраняет его в общий POI/history graph и обновляет story projection.
9. Сомнительный candidate withheld/unfinished, а не валит run.
10. Progress/partial facts доступны пользователю.
11. Selection → draft работает до достижения идеального recall.
12. Deep dedup/conflict/review может продолжаться resumably и не обязана держать весь пользовательский результат.

Helper допустим только как измеренно полезный fallback.

## 9. Stop rules против нового дрейфа

Перед добавлением нового слоя нужно остановиться и перечитать эту ретроспективу, если случилось любое:

- два последовательных source fixes не улучшили пользовательский outcome;
- появился третий обязательный semantic/recovery stage;
- тесты становятся зеленее, а facts в UI всё ещё нет;
- Live повторно читает тот же chunk/tool response;
- один плохой candidate блокирует десяток хороших;
- предлагается новая модель/service вместо упрощения текущего small-step contract;
- новый acceptance требует больше, чем исходная продуктовая задача;
- обсуждение снова сосредоточено на SHA/packet/digest вместо user outcome.

В такой ситуации сначала сравнить с последним полезным baseline, а не продолжать добавлять механику.

## 10. Что делать дальше

Следующая задача должна начинаться с текущего PR #126 и защищённых требований, а **не с нового архитектурного дизайна**.

Порядок:
- сохранить удачные решения выше;
- сделать normal path Live-first максимально коротким;
- использовать `0dda885…` как направление «good facts before save»;
- не делать repair обязательным;
- получить реальный end-to-end продукт: Internet → facts → UI → selection → draft;
- выпустить рабочий MVP даже с несовершенным recall;
- затем улучшать precision/recall по реальным owner stories.

Главный урок суток: **мы уже получили достаточно информации, чтобы перестать проектировать facts system заново. Теперь ценность даст сокращение critical path и поставка продукта, сохраняя доказанную инфраструктуру.**
