# Street Story — завершить исходное восстановление продукта

## Актуальное продолжение: 11 октября 2026, 19:46 UTC

**Это обновление существующей задачи и PR #253, а не запуск второго coding-агента.** Владелец прямо попросил перенести результаты анализа в постановку. Полный исходный scope и DoD ниже сохраняются. Ближайший приоритет — общий путь SOURCE → правильное физическое тело → собственные полезные факты → существующая POI-память, согласно [временному приоритету](https://github.com/onedayonemasterpiece/street-story/blob/62f08b4f02db4d42111374029cd6da02544a27a5/docs/prompts/street-story-temporary-core-priority-20261011.md). Его прежние указания про terminal 180/480 и локальные cumulative caps заменены поздним согласованным уточнением ниже; остальные требования не отменяются.

GitHub-readback перед этой записью: PR #253 открыт; head `chatgpt/street-story-recall-20261010@b46559aab260cb20dff6062837dc3c5eac938f43`, base `work/street-story-mvp-20260908@93a0878a80f0f7c6b0cef286e4b691f2462c0206`. Этот файл обновляется именно на head-ветке PR, без изменения product code. Последний подтверждённый финал существующего native thread `01a127b7-b004-76e1-8cc4-bf3fceea2e21` — около 18:49. Новый status-read отклонён; текущую активность не считать проверенной. Не запускать новый turn только ради доставки документа: сначала безопасно сверить состояние существующего исполнителя и не конкурировать с ним.

### 1. Что действительно изменилось и что ещё не доказано

Не повторяй старый аудит как список отсутствующего кода. В текущем кандидате есть системные изменения: durable backend owner, immutable accepted basis, source-version/chunk union, локальная изоляция плохих rows с сохранением dependency closure, параллельные review groups, ready REF без обязательного selector, retained T, исходное разрешение новых G/T SOURCE, durable literal hints/owner waves, load-aware fact route selection. Гипотеза «почти все системные исправления остались невыполненными» кодом не подтверждена. Но это не доказывает конечный продукт.

Свежая closed-client continuation-серия15 выполнялась на `ce190fdfc9ec60080da446097b1941d06e965abb`, затем harness-only `8d68e8cda8afd27ef10492097bd09a569cd6ba21`; не на конечном b46559a. Все15 SOURCE сохранились. Запрошенные reopen600/1200 завершили GET всех15 через728.6/1295.1с. На границе эксперимента:13pending,124clarification после настоящей подсказки,120failed на старом retained-REF ceiling, **zero eligible facts**. Shutdown эксперимента не равен пользовательскому Stop или штатному terminal. Это цензурированное доказательство lifetime, не0/15 accuracy и не final acceptance. Последняя правка b46559a заменила terminal retained-address guard на четыре одновременных слота без суммарного потолка; фактическая очередь120 проверена offline, не новым provider run.

Описание PR сообщает b46559a backend CI2977passed/52skipped и Android30 emulator tests; это technical checks, не независимая проверка смысла всех фактов. Production по последнему подтверждению остаётся93a0878; deployment кандидата не выполнен. Сохрани прежние wrong-body и semantic negatives:108/130, Kester attribution125, composite/unsupported claims102/104. Не превращай новые labels107/120/124/125, найденные агентом в web, в независимую истину без проверки.

### 2. G должен измеряться по итоговому набору, а G→T — по exact IDs

Владелец явно определил проверяемую метрику: сколько физических кандидатов было до G, сколько осталось после него, сохранилось ли правильное тело; T не должен молча получать более широкий набор. Считать это обязанность исполнителя, а не владельца.

Сохраняй раздельно:
- полный received physical universe G;
- пространственные гипотезы и полный retained output G/initial joint decision;
- active research priority, UI shortlist и host-side unexamined reserve;
- фактически переданные T detailed/context-only IDs и разрешённые positive IDs;
- добавленные ID, конкретную причину/receipt/relation каждого расширения и точное множество T-minus-G;
- justified exclusions с условиями отдельно от unexamined. Uncertain, отсутствие статьи и отсутствие в active list не означают rejected.

На сохранённой дневной серии `final-8a4d3a9` uploaded 13 из плановых 15. Первичный источник: `/home/dev/artifacts/street-story/20261011T082451Z-product-recovery-finish-20261011/final-8a4d3a9/data/street-story.sqlite3`, records `identity_physical_hypothesis.initial_decision`, `identity_joint_followup.prepared_request`, закрытые T replies и events. Ночная серия — отдельная cohort; её числа нельзя подменять дневными.

| Фото | G physical input | Retained initial IDs, count | Фактический T input | Состояние и граница вывода |
|---|---:|---:|---|---|
|102|91|2|не измерен|Сохранены 133035111/133035107; evaluator 133035113 не среди них. Не считать отсутствие T нулевым набором.|
|104|102|3|3 detailed +99 reserve =102|response_closed; positive schema 3.|
|105|117|4|не измерен|Spatial hypotheses 3 и retained 4 различаются; independent truth не установлена.|
|106|148|3|не измерен|Active priority 2; evaluator 150596899 в retained 3.|
|107|171|2|не измерен|T not_sent; нового доказанного truth нет.|
|108|106|4|4 detailed +102 reserve =106|response_closed; spatial hypotheses 3, но initial retained уже4.|
|111|54|2|не измерен|Evaluator 95290265 отсутствует в retained 2; не объявлять reserve отброшенным.|
|119|120|3|подготовлен 3+117=120|not_sent; это структура запроса, не завершённая T-работа.|
|120|не измерен|не измерен|не измерен|Initial not_sent.|
|124|не измерен|не измерен|не измерен|Clarification/hint-сценарий не даёт этого G измерения.|
|125|не измерен|не измерен|не измерен|Initial not_sent.|
|130|206|3|3 detailed +201 reserve =204|response_closed; positive schema 3.|
|132|212|1|подготовлен 1+211=212|UNKNOWN; singleton не доказывает автономное уникальное решение.|
|126/129|не запускались в этой cohort|—|—|Использовать отдельно их прежние сохранённые series, не переносить сюда.|

Для 130 два G input ID — `osm:relation:5646860` и `osm:relation:5646861` — отсутствуют в T 204; причинное объяснение их отсутствия в этой проекции не установлено. Это не доказанные геометрические исключения. Во всех девяти извлечённых непустых G decisions — uncertain; на 104 зарегистрирован invalid G proof. Автономное принятое G-only решение этой cohort не установлено.

Exact closed T:
- **104:** S=`osm:relation:3665416, osm:way:1079242380, osm:way:105154981`. T получает 102 физических ID, хотя positive schema имеет3.
- **108:** spatial 3 расширены до retained S 4 ещё initial research_priority: `osm:way:1292667952, osm:way:1292667953, osm:way:1292667951, osm:relation:1383564`. Поэтому неверно говорить, что T сам необъяснимо добавил четвёртый ID. T получает 106 ID, затем явно отвергает evaluator body 1383564 и выбирает 1292667952. Удаление лишнего контекста само по себе не доказывает исправления этой ошибки.
- **130:** S=`osm:way:193106140, osm:way:193106162, osm:way:193106166`. Evaluator body 193106188 был только в reserve, вне positive schema; T честно возвращает uncertain и продвигает его как material alternative/research priority. В первичном frozen packet уже есть **R1:140↔188** и **R2:188↔162**. Следовательно, правильное тело можно передать до T через адресную существующую связь, без всего 201-body reserve. Близость/side pair — причина сравнения, не proof identity.

Ночная saved cohort подтверждала лишь priority partition 1717=17 active+1700 reserve в 12 наблюдениях, не 1717→17 геометрических исключений. Полные G payload доступны для семи фото; часть UI shortlist менялась после REF. Существовавшие labels 108/111/132 давали 1/3 retention в узких G nominations и 3/3 с reserve; это ограниченная evaluator-проверка, не универсальная accuracy.

Upload→retained / upload→T prepared, секунды: фото 104 — 74.014/77.014; фото 108 — 59.677/80.695; фото 119 — 60.710/71.363; фото 130 — 46.095/48.951; фото 132 — 48.164/52.301. Это event latency совместного pipeline, не чистое время G и не доказанная экономия. Нет matched G-off/G-on baseline. В итоговой метрике показывай coverage, reduction factor, correct-body retention, wrong exclusion, no reduction и unknown раздельно; missing нельзя записывать как 0.

### 3. Не переписывать исправление заново: использовать уже подготовленный handoff

**Кандидат, не production:** `handoff:src_d7ac15fd31acab9b73edffb0`, exact base b46559a. Исходный код и проверки лежат в существующем isolated worktree; не создавать ещё один clone/venv и второго writer. Актуальна handoff-копия после последнего форматирования; ранний локальный standalone.patch промежуточный и не пригоден как final handoff.

Изменённые файлы:
1. `backend/street_story/identity_architectural_comparison.py`
2. `backend/street_story/identity_discovery.py`
3. `backend/street_story/identity_proof.py`
4. `backend/street_story/identity_source_selection.py`
5. `backend/tests/test_architectural_t_supply.py`
6. `backend/tests/test_targeted_g_t_handoff.py`

Механизм: S=retained hypotheses ∪ actual article nominations; E=прямые issued observed_side_pair counterpoints к исходному S. Передавать S∪E с exact relation IDs и причинами. **Не обходить граф транзитивно и не ставить Top-N.** Остальной reserve остаётся в прежнем host owner как unexamined, с count/digest вместо rows в prompt. Полный каталог также убран из model-facing enums. Positive scope остаётся S; E явно context-only, может стать следующей адресной гипотезой, но не положительной identity без собственной опоры.

Модель может запросить конкретный received reserve через exact ID/@MAP label, reason и next_question; host проверяет membership, допускает такое расширение только при uncertain. Existing physical_research_priority → promote_observed_candidates проверен как исполнимый consumer, не только сохранённый флаг. Зафиксированы label→ID context и membership исходного запроса; новый UNKNOWN не декодируется по переставленным labels. Для старого text-portion receipt читается original frozen host_contract; exact body/cursor/revision возвращается к исходному operation role, без нового send. Scoped proof bound согласован с issued alternatives, включая >8; рост непрочитанного host reserve сам по себе не отзывает accepted evidence.

**Actual full-package offline reconstruction из трёх сохранённых реальных records:**

| Фото | T physical IDs до→после | Host reserve после | Positive scope | Prompt UTF-8 до→после | JSON schema bytes до→после |
|---|---:|---:|---:|---:|---:|
|104|102→5|97|3|57048→42224|14718→9678|
|108|106→6|100|4|56921→45150|16034→10805|
|130|204→6|198|3|63983→37285|21289→9976|

Schema bytes измерены одинаковым JSON serializer в offline check; это не provider token usage, не полный wire envelope и не latency/cost saving. Сокращение T comparison inventory — 20.4×/17.7×/34×. Новые наборы:
- 104: S 3 плюс `osm:way:999524939` через R4 и `osm:way:999524943` через R3.
- 108: S 4 плюс `osm:way:103106259` через R5 и `osm:way:103106262` через R1.
- 130: S 3 плюс `osm:way:193106156` через R3/R4, **`osm:way:193106188` через R1/R2**, `osm:way:837368916` через R5.

Проверено: **132 passed**, 2 warnings, 46.68 с в пяти адресных модулях; durable job `job_367945bcc43a68ff20aec09b`, exit 0. Это production modules + saved-input reconstruction + regression fixtures, **без новых model/provider inference**. Проверены actual request builder, exact IDs, candidate promotion, неизвестные pointers, отсутствие транзитивного/Top-N expansion, >8 proof coverage, frozen labels и original text-portion role. Два старых fixture ожидания разобраны: global unrelated relation не становится addressed comparison; unknown primitive остаётся в original receipt, но не выдаётся T как рассмотренный кандидат. Правильный 130 остаётся достижим; улучшение распознавания 108/130 не измерено.

Окружение этой проверки — **Python 3.14.4**, не deployed 3.12. Использован только project-relative handoff/backend/.venv; внешний runtime не привязывался. Public runtime dependencies ограничены установленными версиями через `backend/.audit-public-constraints.txt`; dev tools: pytest 9.1.1, pytest-asyncio 1.4.0, ruff 0.17.0. Vendored live-interaction 0.3.11rc3 установлен штатным script с lock SHA256 `9bae84c454fcbeae05980e8985f097b42384c2077398414315424999f619ed29`.

**Текущая готовность и блокеры:** 132 теста прошли до финального форматирования. Ruff ранее нашёл 9 E701/E702 только в новом testfile; другие пять изменённых файлов прошли lint. Владелец разрешил ровно один повтор форматирования; он выполнен успешно 11 октября, 19:40 UTC, `op_24c2d8b86f111fd016e62d41`, «1 file reformatted». Следующий read-only lint и финальный git-state readback отклонены; не повторять эти отклонённые действия и не обходить другим маршрутом без отдельного разрешения. **Final lint/AST/readback после format не подтверждены; merge-ready не заявляется.** При интеграции использовать имеющееся окружение исходного coding-агента, сверить base/current HEAD без reset чужих изменений, проверить применимость и Python 3.12 compatibility. Не объявлять эту правку deployed или accepted corpus.

Экология: выделенное место handoff-файлов около 328 MB, из них 312.4 MB venv и 15.5 MB source worktree; дополнительный прирост shared pip-cache не измерен. Не создавать новых больших копий. После подтверждённой передачи и сохранения необходимых small diff/evidence отдельно согласовать cleanup временной копии; уникальные изменения и реальные corpus/UNKNOWN receipts не удалять.

### 4. Admission: готова диагностическая правка, причина ещё неизвестна

RESOURCE_CONTROL_UNAVAILABLE в сохранённых попытках нельзя переводить в «квота исчерпана». Он сводил в один code HTTP>=300, HTTP transport/timeout и malformed JSON. Сохранены отдельные control-plane стадии capabilities/reserve/finalize и некоторые latency: Native not_sent attempt около 10827 ms, reserve около 4011 ms, capabilities около 11107 ms; точный HTTP status/exception исторически не сохранён. Это доказанный недостаток диагностики, не доказательство нехватки ресурса.

Отдельный SDK-кандидат: `handoff:src_8d43fb6ded0bd5a720e9375f`, проект ai-resource-control, base `cf2792ee99a4adbcd01055205597fbecd161d9e0`. Файлы `src/ai_resource_control/client.py` и `tests/test_control_diagnostics.py`. Семь focused tests прошли на полном пакете с MockTransport. Добавлены безопасные failure_kind/http_status/diagnostic и boundary warning без секретов; quota, retry count, payload idempotency, admission и provider sends не менялись. Не установлен в production.

Следующий минимальный диагностический шаг — разрешённый authenticated capabilities-only запрос с HTTP status/exception class/request correlation и latency, **без reserve/acquire/inference**. Зарегистрированного такого probe не найдено; чтение настроек было отклонено allowlist, обход запрещён. Успешные DNS/TLS и неавторизованный HTTP 401 за 178 ms доказывают лишь этот неавторизованный transport path. Исправление диагностической классификации не выдавать за исправление причины outage. Штатное ожидание admission, отказ реальной квоты и недоступность RPC/БД показывать отдельно.

### 5. Ресурсная политика, ближайшая проверка и сохранённый scope

- Согласованное удаление terminal 180/480 и искусственных cumulative/local Native ceilings сохраняется. 180/300/480 остаются контрольными метриками скорости с upload; отдельные request timeouts, настоящий admission/accounting, Stop, generation fences и exact UNKNOWN остаются защитой.
- `NativeQuotaPermission.MIN_REMAINING_PERCENT` изменён с 3 на 0. Это **отдельное изменение ресурсной политики**, не автоматическое следствие удаления таймеров. Явного согласия расходовать Native до исчерпания не установлено; владелец бережёт Codex для разработки. Порог строго >10% в DevCoveer/status — другой gate, не прежнее значение этого StreetStory constant. Не откатывать и не расширять политику молча; установить происхождение/разрешение и сохранить требуемый владельцу резерв до нового expensive run. Google quota и Native local 60000 не смешивать; optional-NULL механизм local limit уже существовал.
- Не добавлять новый произвольный attempt cap вместо удалённого. Использовать существующие reasoned exhaustion, finite distinct work, progress-aware backoff, authoritative retry_at и дедупликацию закрытых операций. Отсутствие deadline не разрешает бесконечно повторять один и тот же бесполезный send.
- Никакого нового 15-photo mass run только ради проверки данного документа. Сначала принять/довести существующий handoff и control-plane диагностику. Если уже разрешённый ресурс и работоспособный путь подтверждены, различить один ранее неуспешный cold-вход и соседний negative control; lifetime/reopen проверять отдельной небольшой cohort, не маскировать correctness пятнадцатью одновременно висящими задачами. Затем вернуться к исходной final 15 на стабильном runtime; без full 15 после каждой малой правки.
- Остаток полного задания не исчезает: wrong physical body, own-source semantic support/composite facts, source ecology, claimed-but-not-useful work, прогресс и сохранность partials, распределение независимой работы и простаивающие разрешённые ресурсы, чрезмерные receipt/map copies/scans, recovery fairness, Android lost/repeated actions, persistent POI/RKB references и safe legacy removal. Свяжи каждый оставшийся пункт с исходной матрицей, кодом, real corpus evidence и минимальным coherent fix; не превращай P2/85 строк в новый предварительный gate основного результата.
- Code/tests coverage, saved real data replay и real end-to-end product proof всегда отдельны. Успешный formatter, 132 tests или уменьшенный prompt не подтверждают правильную identity, собственные факты, их UI/readback/POI persistence или конечную приёмку.
- Полномочие этой записи — **обновить документ в GitHub**. Оно не разрешает code commit/push/merge/deploy, запуск нового Codex turn или новые paid provider runs. Продолжать существующую задачу по её действующим полномочиям; при отсутствующем разрешении назвать конкретный blocker.

---

## Что требуется довести

Продолжи [PR #253](https://github.com/onedayonemasterpiece/street-story/pull/253). Аудит проверял runtime `af2cd7b2bdbdde30db9429edf033cb52f55b2dc9`; сверь актуальные HEAD/рабочее дерево, сохрани изменения и evidence. Нужно завершить **всю исходную обязательную задачу**, включая прежние незакрытые требования и выявленные дефекты. Список ночных ошибок не заменяет исходный объём.

Первичны [исходная постановка на 9e7e511](https://github.com/onedayonemasterpiece/street-story/blob/9e7e511d0901a799c14ac6ab82b17dd41efa2d1b/docs/prompts/street-story-universal-product-recovery-20261010.md), [11 critical requirements и 30 acceptance clauses](https://github.com/onedayonemasterpiece/street-story/blob/af2cd7b2bdbdde30db9429edf033cb52f55b2dc9/.devcoveer/requirements.json) и [ПМИ](https://github.com/onedayonemasterpiece/street-story/blob/af2cd7b2bdbdde30db9429edf033cb52f55b2dc9/docs/testing/street-story-product-acceptance-20261010.md). Этот entrypoint задаёт порядок продолжения и исправляет ошибки интерпретации, не отменяя их.

Рабочие приложения:
- [Полная матрица исходных требований](../reports/street-story-requirements-traceability-20261011.md): обязательность, реализация, доказательства, остаток, проверка закрытия.
- [Причины семи deadline](../reports/street-story-deadline-root-causes-20261011.md) и [255 событий](../reports/street-story-deadline-events-20261011.csv).
- [Действующие метрики и границы](../reports/street-story-product-metrics-audit-20261011.md).
- REF и достижимость источников: строки B07–B19 основной матрицы; факты и интерфейс: D01–D43.
- [Ночной отчёт реализации](https://github.com/onedayonemasterpiece/street-story/blob/af2cd7b2bdbdde30db9429edf033cb52f55b2dc9/docs/reports/product-recovery-implementation-20261010.md). Raw evidence: `/home/dev/artifacts/street-story/20261010T213047Z-universal-recovery-20261010/`.

Сохрани полезное ядро: durable backend ownership, immutable accepted basis, собственные source spans, локальное withholding противоречий, warm POI reuse и Android-исправления. Предпочтительны полезные правильные факты при честной неполноте, а не ожидание идеального знания. Исходное «80%» **не означает recognition rate или PASS=12/15**; утверждённого процента успеха нет. Не возвращай обязательного второго model reviewer: прежние 5/26 отклонений не измеряют false-fact rate.

## Порядок работы и приоритеты

Сначала используй historical evidence и offline-контрпримеры. Полный baseline15 до правок не нужен: новый адресный baseline допустим там, где сохранённые данные не различают причины. Старый корпус смешивал SHA; он не является приёмкой конечного runtime. Тесты, прочитанные в аудите, не считать заново выполненными.

1. **Убрать доказанные ожидания с критического пути.** На 132 готовая gallery ожидала initial REF selector; последовательные retry задержали его примерно на 80 с. На 129 добавились 39 с, на 105 первая REF-пара появилась лишь около +132 с. Дополнительное ранжирование должно менять обратимый приоритет, а не блокировать уже пригодные REF. Используй существующие inventory/active-reserve/queue и bounded dispatch; готовая независимая работа идёт при доборе. Не снимай обязательный semantic physical-binding verdict и safety/URL/Stop/UNKNOWN guards.
2. **Разобрать общий admission owner и добычу альтернатив.** T followup129/132 и fallback не отправились из-за resource-control RPC/registry, поэтому смена provider name сама по себе не независимый путь. Диагностируй существующий control plane, отдельные request timeouts и реальные admission waits, не добавляй очередной wrapper. Task-wide terminal180/480 больше не являются действующим способом остановки. Нижележащая причина timeout пока не установлена. На 126 acquisition альтернатив должен начинаться после явного T contradiction без ожидания следующих сравнений старой gallery. Для111 различи `parse_failed/address_total_missing` от настоящего empty lookup; сохрани raw response для ремонта. Его нужное содержимое аудитом не доказано. На108/119 конфликт physical binding реален: confidence/match не разрешает снять guard.
3. **Закрыть остаток G/T/REF исходной задачи по матрице.** Для G обеспечь различающий SOURCE/MAP-контекст тела и соседей, вычисленные адресуемые relations там, где они нужны; модель отвечает за семантику, host за расчёт и привязку. Прежний cumulative cap=2 уже снят в текущем кандидате: не назначай его удаление заново и не возвращай его. Проверь retained-text delivery и адресованное продолжение initial→T→tail с сохранением исходных UNKNOWN receipts, реального admission и журнала работы. Проверь tail-only evidence, early-T и отсутствие MAP. Для REF обязательны reachable reserve при omission selector, bounded original fallback после технической ошибки preview/download, сохранение active physical choice при позднем sort, достижимость Wiki IDs после третьего и хвоста inventory >16000. Выполненный старый 2-URL fix не делай заново. Остальные исходные требования к transport/cooldown и diversification закрывай по матрице.
4. **Исправить два общих механических контракта.** В общем merge нормализуй effective source revision/locator до union: одинаковый URL+текст разных revisions/chunks не должен схлопываться или переатрибутироваться. Проверь идемпотентность и равенство in-batch/durable. Согласуй `drain()`/observer: None под iterable contract дал четыре TypeError. Normal/failed child/Stop/cancellation сохраняют удачные partial bodies. Эти дефекты и T cap не доказаны причинами всех семи deadline.
5. **Закрыть неправильное физическое тело и оставшиеся продуктовые обязательства.** Wrong130→193106140 вместо193106188 повторялся на0b792c2 и46a6aa5. Различай конкретный member body и общий адрес/комплекс по SOURCE, собственному тексту и карте. Expected IDs остаются только у evaluator. Проверь107/120/125 и saved negative G/T/REF controls; при нехватке evidence задай конкретный вопрос. Доведи durable accepted hint→job-owned wave: сохранить literal hint/provenance в нужных T/REF inputs, прежние outcome/metrics и Stop/generation fences; direct Live-await с тихим reset недостаточен. Дальше закрой все обязательные строки матрицы: D02/D06–D08 — отрицательные контроли, изоляция плохой строки и bounded repair/review; D35–D38 — accepted basis, fences, provenance и observer; C03–C08 — Android, owner waves и подсказки. Проверь выбор/редактуру/голос/visual и persistence/reopen. Условные и отложенные строки остаются видимым отдельным scope. Не прекращай работу после исправления одной identity-ветки.

Реализацию выбирай по evidence: минимальные общие изменения, без нового framework, адресных исключений, каскада retries и перезапусков до случайного PASS. Каждая связанная правка получает различающий regression control; сохраняй защитные утверждения тестов. Скриптовые замены проверяют совпадение и результат.

## Приёмка и сохранность доказательств

До real-provider серии зафиксируй evaluator/config/manifest/truth revision. Изменения harness отдели и проверь на сохранённых positive/negative controls до нового score; continuity должна быть воспроизводимой. Сохрани исходные фото, hashes, raw outcomes и источники. Независимая проверка истины не использует вывод исправляемой модели как oracle.

- **Факты:** сохранённые реальные negatives 1853/1859, соседний subject, composite claim, устаревшая «нынешняя» функция, чужая цитата. Проверяй claim→own exact span→body/version/locator→subject и смысловую поддержку. Literal enforcement уже есть в `review_packets.py:513–531`: normalized substring в evidence этого факта; сохрани его, не возвращай enum-only барьер. Hash/count не доказывают смысловую истинность. Все принятые результаты конечного корпуса независимо проверь; unknown обозначай явно.
- **Ownership:** детерминированные Live/headless interleavings одного chunk, lease/fence, Stop, restart, late completion, bounded concurrency и idempotent sends. Joined followup после success/failure/restart уже тестируется. Отдельно проверь принятый второй запрос, ожидающий исчерпания первой волны: исполнимое разрешённое продолжение либо видимое нужное подтверждение. Не ломай защиту от тихого reset/implicit spend.
- **Живой путь:** research начинается в приложении, клиент закрывается до окончания, backend продолжает; затем backend restart и reopen без дублей/потерь. Подтверди собственные факты, all-source UI, выбор, concept/draft, голосовую редактуру и visual revision по исходному контракту. Проверь правильную и ошибочную поздние подсказки и progressive Android UI; Late hint/124 и script-assisted помечай отдельно, scripted input не заменяет требуемый реальный сценарий. Готовая quiescent-БД не заменяет этот E2E. Warm сравнивает сами canonical claims/proofs/lineage: saved104 сохраняет8/8, новый cold→warm весь свой accepted set, включая другую фотографию того же точного POI, а не только повтор того же файла.
- **Конечная серия:** все 15 — 102,104,105,106,107,108,111,119,120,124,125,126,129,130,132 — на одном стабильном runtime после общих fixes/targeted controls; warm/assisted/lifecycle и доступный независимый holdout отдельно; historical26 applicability и оставшиеся11 NOT_RUN не смешивай с final15. Восемь исходно неизвестных truth не превращай в успех или исключение из знаменателя; семь имеющихся labels тоже требуют независимой проверки. Если нашёл исправимый общий дефект, исправь; итоговая приёмка должна относиться к конечному SHA, без full15 после каждого маленького patch.

Контрольные метрики с upload: identity180с, first useful eligible fact300с, completion480с; исходный минимум3 самостоятельных подтверждённых содержательных факта сохраняется. По согласованному позднему уточнению 180/480 больше не являются terminal kill принятой backend-задачи. Это метрики скорости, **не обещание хорошей пользовательской скорости**; примерно30с после SOURCE — engineering target. Показывай отдельно identity/first useful/useful set/terminal/commit→UI, технические отказы, wrong/unknown, numerator/denominator, actual sends/UNKNOWN/usage/cost либо честную нижнюю границу. 1–2 полезных факта сохраняй и показывай, не называя их выполнением минимума3; 3 не потолок исследования. Новые p90/p95, success-rate и денежные gates не изобретай, manifest без полномочий не меняй.

## Завершение и пределы

Используй реальные provider quota/accounting, необходимые concurrency constraints и действующие разрешения; сохрани ресурс владельца на разработку и согласованную приёмку. Не возвращай снятые искусственные cumulative/local ceilings. Изменение MIN_REMAINING_PERCENT=0 проверь отдельно, как указано ниже; снятие terminal180/480 само его не разрешает. Не совмещай приёмку с full pytest, editorial генерацией и конкурирующим provider flow. Спрашивай, когда следующий шаг действительно требует нового разрешения или платного обязательства. Commit/push/merge/deploy — по действующим полномочиям.

В финале обнови каждую строку матрицы: закрыто с доказательством, конкретно блокировано либо исходно optional/deferred. Полнота источников, RKB ingestion, журнал, все P2 и 40–50 карточек не становятся новыми условиями полезного выпуска; конкретную обязательность бери из исходной строки, ничего не исчезает молча. Передай краткий итог, измерения/матрицу всех15, SHA и ссылки на raw evidence, lifecycle/warm/assisted и остаток.

Завершай после закрытия согласованных исправимых обязательств и фактической конечной приёмки, либо на конкретном внешнем blocker/отсутствии полномочия/исчерпанном разрешённом ресурсе с проверенными альтернативами. Укажи, что требуется для продолжения. Локальный timeout/один неразрешимый объект не заканчивает независимую разрешённую работу; честный отчёт о провалах не заменяет её выполнение.
