# Street Story — довести факты до первого настоящего продуктового результата

Дата: 4 октября 2026.

Это **исполнительная постановка**, а не новый аудит.  
Продолжай существующий Street Story и текущую ветку фактов. Не начинай проект заново.

На момент подготовки этой постановки рабочая ветка PR #126:
- branch: `chatgpt/street-story-facts-review-finalization-20261003`
- observed HEAD: `0dda88537c0d8feabc447b728976b096617523bf`
- PR: https://github.com/onedayonemasterpiece/street-story/pull/126
- backend production ещё оставался на `74c8f71da901a2c1fd9339eb79e0500efcfc0110`

Сначала перечитай фактический текущий HEAD/CI/runtime, потому что ветка могла продвинуться после создания этого файла. Используй существующий worktree `street-story-poi-runtime-clean`, если он всё ещё является checkout этой ветки. **Не создавай новый worktree без необходимости.**

## 0. Почему меняем направление

За почти сутки разработки механизма фактов владелец не увидел нормальной демонстрации продукта. Были десятки запусков, множество тестов, acceptance harness, budget/review/recovery исправления и несколько локальных регрессий, но это нельзя считать поставленным продуктом.

Главная проблема больше не выглядит как «ещё один баг review». Мы слишком усложнили normal path.

Плохая целевая схема:

`research → candidates → review packet → helper assessment → context recovery → repair → новый packet → superseding review → Live finalization → eligibility`

Такая цепочка делает исправление промежуточно плохого результата штатной частью новой системы. Для нового продукта это неверная архитектурная граница.

Последний коммит `0dda885…` уже сделал правильный шаг:
**формировать evidence-backed atomic claims до сохранения, а recovery оставлять для supersession**.
Не откатывай этот шаг. Доведи его до простой продуктовой схемы.

---

# 1. Целевая архитектура — простая и LLM-first

## 1.1. Gemini Live остаётся Мирой

Мы **не уходим с `gemini-3.8-live`** как основной пользовательской модели.

Live отвечает за:
- разговор с владельцем;
- понимание намерения и темы исследования;
- постановку research goal;
- отображение прогресса;
- запрос дополнительного исследования при необходимости;
- представление готовых фактов владельцу;
- выбор фактов в разговоре;
- дальнейший concept / draft / publication flow.

Live **не должна вручную таскать через голосовую сессию полный текст документов, длинные digest/id и выполнять десятки служебных tool-call шагов для каждого факта**.

## 1.2. Отдельная research-модель — это нормальная специализированная роль

Для пакетной работы с источниками используй существующие configured research routes.

Отдельная non-Live модель может выполнять:
- извлечение фактов из сохранённых source chunks;
- атомизацию;
- привязку каждого тезиса к собственному evidence;
- сохранение qualifiers / uncertainty / subset-vs-whole;
- semantic dedup;
- обнаружение конфликтов;
- независимую финальную проверку bounded набора кандидатов.

Это не детерминистический NLP. Семантика остаётся у LLM.

**Не хардкодь модель по названию только потому, что она “lite”.**
Сначала используй текущую конфигурацию. Если текущий configured research model на одном и том же frozen corpus устойчиво проваливает смысловой gate, разрешён один небольшой сопоставимый A/B среди уже доступных configured Gemini research models. Выбери минимальную модель, которая реально проходит качество. Не строй отдельный model-router.

В отчёте обязательно укажи фактически использованную модель.

## 1.3. Нормальный pipeline

Нормальный путь должен быть концептуально таким:

```
Mira Live
  → research goal
  → discovery / fetch / frozen source versions
  → research LLM:
       extract + atomicize + bind own evidence
  → bounded independent semantic verification
  → deterministic validation of references/revisions
  → persist eligible / withheld facts
  → compact research result
  → Mira Live
  → user sees/selects facts
```

Допускается **не более одного bounded semantic correction pass** внутри того же research job, если verifier возвращает:
- `needs_context`;
- `split_required / repair_needed`;
- недостаточное собственное evidence.

Это не отдельная продуктовая сущность “repair”.
Это продолжение того же research attempt до получения корректного результата.

Если после одного correction pass тезис не удалось доказать — **withhold его и продолжить продуктовый результат с остальными хорошими фактами**.

---

# 2. Что НЕ должно быть обязательным normal path

Следующие механизмы могут остаться как recovery/debug/history, но не должны быть обязательной лестницей каждого исследования:

- `assess_review_packet` как отдельный обязательный hop из Live;
- `repair_research_fact` как штатный способ получить нормальный факт;
- несколько generations review packets только чтобы исправить первую формулировку;
- заставлять Live переписывать длинные fact IDs / revision digests / evidence IDs;
- многократное чтение одного и того же chunk из-за отсутствия следующего deterministic шага;
- повторный semantic review уже корректного факта только для формального “финального слова Live”.

**Убери repair из normal-path orchestration.**
Если функции нужны для supersession уже сохранённого/старого факта — сохрани их как recovery path, не удаляя историю и безопасность.

Не делай массовый rewrite ради красивых названий. Минимально измени orchestration и contracts.

---

# 3. Очень важная семантическая граница

## Модель решает смысл

LLM решает:
- что является фактом;
- где границы атомарного тезиса;
- эквивалентны ли два тезиса;
- конфликтуют ли они;
- какое evidence действительно поддерживает тезис;
- нужно ли сохранить uncertainty;
- нужно ли разделить составное утверждение;
- нужно ли дочитать контекст.

## Код решает только механику

Детерминистический код:
- fetch / transport;
- URL safety;
- chunking;
- точные source versions / spans;
- schema validation;
- ID mapping;
- idempotency;
- revision / stale guards;
- persistence;
- pagination;
- resource budget;
- eligibility как механическое следствие валидного model verdict;
- telemetry.

**Не добавляй regex/keyword/person-name/year правила для определения истинности или атомарности.**

---

# 4. Не терять уже полезную инфраструктуру

Не откатывай:
- frozen source versions;
- immutable observations / evidence;
- durable research run;
- checkpoint/resume;
- exact evidence spans;
- stale revision guards;
- idempotent batch receipts;
- conflict/eligibility ledger;
- quarantine;
- selection persistence;
- mic pause during research;
- compact Live context;
- resource admission guard.

Это хорошие части.

Цель — **снять лишнюю tool choreography вокруг них**, а не вернуть старую небезопасную реализацию.

---

# 5. Сначала один минимальный архитектурный срез, затем демонстрация

Не начинай с полного backend regression и не делай ещё один общий аудит.

Сначала добей один normal path на уже существующем frozen corpus:

1. ordinary broad research goal;
2. research LLM читает все необходимые chunks;
3. возвращает atomic candidates + own evidence;
4. semantic verification;
5. один correction pass только при необходимости;
6. хороший fact set сохраняется;
7. run становится completed;
8. факты появляются в обычном product state;
9. Live получает компактный результат без ручного review-choreography.

Проверь, что:
- три фасадные фигуры Королевских ворот представлены тремя отдельными selectable facts, если source их подтверждает;
- нет уверенного расширения `часть` → `все`;
- дата не наследуется из непереданного antecedent;
- запрос/предложение не превращается в состоявшееся решение;
- каждый eligible факт имеет собственное exact evidence;
- составные независимые персоны/роли не склеены.

Только после этого запускай следующий уровень.

---

# 6. Обязательная продуктовая демонстрация

Это главный gate задачи.

До merge/deploy нужно выполнить **одну настоящую демонстрацию normal path**, а не только fixture/unit test.

## Demo A — Royal Gates, обычный пользовательский запрос

Используй обычную Live-сессию `gemini-3.8-live`.

Запрос не должен перечислять ожидаемые ответы и внутренние tool names. Пример уровня намерения:

> «Найди интересные и небанальные факты об этом объекте, которые можно использовать для поста. Покажи мне факты с источниками.»

Условия:
- идентичность объекта уже подтверждена product context;
- **real Internet retrieval**, не подставной discovery fixture;
- normal configured helper/research models доступны;
- не принуждать helper outage;
- cold run или явно зафиксировать warm cache;
- не использовать guided diagnostic prompt.

Результат должен содержать:
- полезный набор отдельных selectable facts;
- source URL для каждого факта;
- exact evidence/span;
- три отдельные фасадные фигуры, если найденные реальные источники это подтверждают;
- никакого `FAIL_SEMANTIC`;
- никаких обязательных manual repair tools в trace;
- completed research run;
- facts действительно видны в обычном product state.

Сохрани **человекочитаемую demo-таблицу**:
`fact text | eligibility | source | evidence excerpt`.

Не считай результатом только JSON status=completed.

## Demo B — holdout без hardcode

После Demo A — один другой POI.

Требования:
- другое название;
- normal query;
- никакие ожидаемые факты/имена не должны присутствовать в prompt;
- та же pipeline;
- проверить object identity, atomicity, own evidence, completion.

Не оптимизируй код под конкретные имена Королевских ворот.

---

# 7. Пользователь должен увидеть продукт, а не отчёт о backend

После двух semantic demos проверь UI route.

Минимальный продуктовый acceptance:
- открыть/создать story;
- увидеть полученные eligible facts в фактическом Android/UI product state;
- у каждого факта доступен источник;
- выбрать минимум два факта;
- создать draft;
- withheld/unreviewed fact не может попасть в draft.

Если физический телефон не нужен для проверки facts UI, достаточно Android emulator + реальный backend candidate.
Физический телефон не должен блокировать merge **только из-за аудио**, если facts pipeline и UI доказаны отдельно.

Но не называй emulator-only проверку physical-phone acceptance.

Сохрани screenshot/artifact или иной существующий UI evidence, если текущий test infrastructure это уже умеет. Не строй новый screenshot framework.

---

# 8. Ограничение времени и количества итераций

Нельзя снова потратить десятки запусков на один acceptance.

Для каждого real-model demo:
- сначала offline/unit/fixture проверка;
- затем **один** real run;
- если fail — определить **первую конкретную причинную границу**;
- сделать одно локальное исправление;
- один повтор.

Не повторять run без изменившегося условия.

Если после двух исправительных итераций normal path всё ещё не проходит, **остановить patching** и вернуть владельцу:
- точный первый failing transition;
- сохранённый receipt;
- почему выбранная модель/контракт не справились;
- минимальное архитектурное решение.

Не добавлять третий recovery mechanism.

---

# 9. Что делать с текущим helper / high reasoning

В текущей ветке уже есть `assess_fact_candidates()` через configured `research_routes`, и он явно не должен самостоятельно выдавать eligibility.

Используй это как материал для нормального semantic verification, **но не заставляй Live обязательно вызывать его отдельным tool hop**.

Предпочтение:
- backend research orchestration вызывает extraction/verification как часть одного research job;
- модель возвращает structured result;
- deterministic code валидирует references;
- Live получает компактный итог.

Если текущий Lite/Flash с high thinking стабильно даёт плохие semantic verdicts:
1. на одном frozen corpus сделай один сравнимый probe другой уже доступной configured Gemini model;
2. выбери реально проходящую;
3. зафиксируй модель в config/telemetry;
4. не строй новый routing subsystem.

---

# 10. Не смешивать extraction candidates и продуктовые facts

Желательно, чтобы пользовательский ledger не выглядел как кладбище плохих промежуточных формулировок.

Если текущая схема уже сохраняет candidates для durability:
- они могут оставаться durable internal observations/candidates;
- **не показывай их пользователю как eligible facts до semantic verification**;
- не считай последующий correction “ремонтом продукта”;
- сохраняй lineage только если уже опубликованный/выбранный факт действительно superseded.

Для normal extraction предпочтительнее сохранить сразу финальную atomic wording + own evidence после bounded verification.

---

# 11. Acceptance критерии

## P1 — Simple normal path
Обычное исследование проходит без обязательной цепочки repair/supersession.

## P2 — LLM-first
Никакой deterministic semantic classification.

## P3 — Live preserved
Пользователь продолжает работать с `gemini-3.8-live` как с Мирой.

## P4 — Research specialization
Batch semantics выполняет configured non-Live research model; фактическая модель записана в receipt.

## P5 — Evidence quality
100% eligible facts имеют собственное exact evidence, поддерживающее весь тезис с qualifiers.

## P6 — Atomicity
Независимые персоны/роли/события представлены независимо.

## P7 — Honest withholding
Недоказанное не уничтожает весь research result; оно withheld, хорошие факты остаются доступны.

## P8 — Real demo
Royal Gates normal real-Internet demo завершён содержательно успешно.

## P9 — Holdout
Один другой POI проходит без hardcode.

## P10 — UI
Факты реально видны/выбираются и доходят до draft.

## P11 — No loop
Не более двух real-run corrective iterations на один demo.

## P12 — Delivery
Только после P1–P11:
- полная backend regression;
- Android checks/emulator;
- merge PR #126;
- deploy backend exact merge SHA;
- canonical signed APK/release, если Android source изменён;
- health/readback;
- обновить существующий acceptance report.

---

# 12. Что не считать успехом

Не принимать как product result:
- «500 tests passed» без демонстрации;
- fixture-only PASS;
- guided prompt с перечисленными tool calls;
- вручную исправленный receipt;
- `completed` при плохих facts;
- три нужных имени, найденные substring evaluator;
- факт без собственного evidence;
- факт, исправленный отдельным ручным recovery шагом, если этот шаг обязателен в normal path;
- новый architecture document без working demo;
- очередной новый слой retries/continuations/review packets.

---

# 13. Отчёт владельцу

Обновляй существующий:
`docs/acceptance/facts-research-finish-2026-10-03.md`

Не создавай ещё одну серию отчётов.

В конце верни коротко:

1. **NORMAL PATH:** какой фактический pipeline теперь работает.
2. **MODELS:** Live model + research/verification model.
3. **ROYAL GATES DEMO:** факт-таблица и measured time.
4. **HOLDOUT DEMO:** краткий результат.
5. **UI:** что фактически увидит владелец.
6. **DELIVERY:** merge SHA / backend SHA / APK, если поставлено.
7. **BLOCKER:** только если продукт всё ещё не работает — один конкретный blocker, не список гипотез.

---

# 14. Принцип остановки

Главный критерий этой задачи:

> **Владелец впервые должен увидеть нормальную демонстрацию работающего механизма фактов.**

Не продолжай улучшать архитектуру после того, как нормальный product path проходит semantic + UI acceptance.

Не строй космолёт.
