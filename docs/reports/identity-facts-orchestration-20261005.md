# Street Story: идентификация и накопительное исследование, 5 октября 2026

Интеграционный владелец один. База — проверенный релиз `57df972617494ca93a808b7c6a5b783969886655`, ветка поставки `work/street-story-mvp-20260908`. Интеграционный релиз `95a8f6bd090e3b32fd7b737d4ed8d49a6f9e2dce` выпущен и подтверждён обоими health endpoints; сохранность выбранных фактов, draft/source SHA исходной истории проверена. Компонентные проверки не являются сквозной Android-приёмкой.

Уточнения владельца имеют приоритет над прежними пилотными ограничениями: нет лимита «два поиска» и общего потолка источников. Ограничены ресурсные порции, сохраняются все найденные URL, unfinished cursors и завершённые scopes. История помогает модели расширять поиск, а не повторять обработанное. При Stop/Resume сохраняются очереди и результаты. Остановка микрофона управляет разговором, явная остановка исследования — отдельными назначениями identity/facts.

Проверенный native `gpt-6-luna` резерв получает разрешение на 15 минут по authoritative account quota с остатком не менее 3%; запросы внутри окна не повторяют quota RPC. Это отдельно от admission каждой модельной операции. Используется существующий OpenCode на 4097 и установленный native transport; второго OpenCode сервера или дерева зависимостей нет.

## Приёмка

| ID | Статус этого среза | Доказательство и оставшаяся проверка |
|---|---|---|
| D1 | verified | Текущая база и истории сохранены; один интеграционный владелец, exact95a8 runtime health и readback исходных историй. |
| D2 | partial | Реальный независимый OpenCode search: 4 вызова, 26 URL; та же завершённая сессия прочитана без нового inference. Natural Android flow ещё требуется. |
| D3 | partial | Реальные SOURCE/REF Luna-контроли: парк mismatch, полезный поздний кадр match; недоставленные pixels отклоняются регрессиями. Требуется новый runtime readback счётчиков. |
| D4 | partial | Общий acceptance проверяет альтернативы всего shortlist, доказанные aliases и article→physical subject. Native enriched-catalog replay прошёл без нового inference; natural canonical POI требуется. |
| D5 | partial | Принудительного owner_confirmed, снижения порога или seed URL нет. Natural Android automatic match ещё не подтверждён. |
| D6 | partial | Image-capable Luna реально проверена; quota permission и durable native attempt встроены. Runtime fallback той же единицы после deploy требуется. |
| D7 | partial | Durable waiting, route/account health и общий admission интегрированы; exhausted provider retries не уничтожают исследование. Требуется runtime recovery. |
| D8 | partial | Scoped Stop/Resume, epochs, stale-worker fencing, unknown readback, Android outbox/polling внедрены. Android build/unit/lint/smoke прошли; scoped runtime readback ещё требуется. |
| D9 | partial | Общий frozen acquisition и single-flight text/media fetch; scopes сохраняют complete/partial раздельно. Component regression проходит; реальная cross-purpose трасса требуется. |
| D10 | partial | GigaChat прошёл реальные modality/subject/known-claim контроли; 71-фактный POI-memory regression сохраняет выбор, концепцию и draft. Natural «ещё» с Android-проекцией требуется. |
| D11 | partial | SDK 0.1.14 и additive migrations 009–010 поставлены в общую authority; private wheel зафиксирован digest. Provider/source/usage receipts сохраняются. Принятые runtime batches ещё требуются. |
| D12 | partial | VibePublish74960c выпущен; оригинальная operation авторитетно закрыта failed/imagegen_not_dispatched, retry_safe=true, generation_dispatch=not_sent; исходный receipt неизменён, fence sealed. Signed APK exact95a8 CI прошёл. Повторная генерация и тестовая публикация ещё не выполнены. |

## Проверки и доказательства

- Shared OpenCode guard разрешает только `websearch` до исполнения; количества вызовов не ограничивает. Проверены effective loaded hook, native registry и directory binding. Реальный MiMo результат: 26 source URLs, `cost=0` по provider receipt, 52 719 наблюдённых суммарных token units за три assistant steps. JSON-аннотация optional; URL берутся из фактического tool output.
- GigaChat-2 REST v1 с TLS: положительный контроль сохраняет дату и модальность плана, отрицательный помечает чужой POI `source_matches_poi=false`, перефразированный известный факт возвращает прежний `fact_id`. Это синтетические controls без изменения production stories; стоимость unknown. Quote/passage binding проверяется независимо от семантического verdict.
- Private SDK [PR26](https://github.com/onedayonemasterpiece/ai-resource-control/pull/26), release `v0.1.14`, source `a82a97147d697c3fbf0ba0748d6e49be196d0a7a`, wheel SHA256 `186273b4d49c1fb8b6060f885b4c7edbe8a7eaf2f76e23eafbaddb6d90597662`. 90 Python tests, 63 PostgreSQL assertions/24 concurrent acquisitions, 32 provider-limit assertions; все 10 migrations. CI прошёл. Исправлена гонка тестового Docker PostgreSQL initialization с временным Unix listener.
- Новые узкие slices: identity continuation/history 82 passed; article acquisition/fact continuation 71 passed; provider/installer/control integration 71 passed. Полная интеграционная backend-suite: **864 passed**, один dependency deprecation warning, 86.55s; Ruff и Node syntax check прошли.
- После этого среза исправлены две связи автоматического pipeline: первоначальное сравнение всегда поступает в общую квалифицированную visual-очередь; после фонового подтверждения POI факты автоматически ставятся в существующую очередь без микрофона. Последующие targeted regressions: **47 passed**, installer integrity: **37 passed**. Android identity-only checker принимает headless receipt и отвергает 7 нарушений; Kotlin build и natural emulator-run ещё ожидаются.

Retained evidence находится в `/home/dev/artifacts/street-story/20261005T065415Z-identity-facts-orchestration-20261005` и `/home/dev/artifacts/ai-resource-control/20261005T074633Z-research-workload-admission-20261005`. Ключевые receipts: `shared-unlimited-search-reconciled.json`, `native-luna-enriched-catalog-replay.json`, `gigachat-shared-canary-receipt-v3.json`, `gigachat-negative-poi-canary-receipt.json`, `gigachat-known-fact-canary-receipt.json`, `shared-capacity-migration-receipt.json`, `private-wheel-0.1.14/receipt.json`.

Natural time-to-identity / first-accepted-fact / more пока **не измерены для нового интегрированного релиза**. Ни стоимость по каталогу, ни тестовый seeded snapshot не подменяют эти измерения.

Original operation `op_0e1b0da62a134dd5af541b0e2fdf8ae8`, executor `visual_e84325ad257646a3848db8c5f81f2771`, story `story_32e30819bb9d933c496688e1`: original-principal reconcile_dispatch завершён на той же operation/job: revision2, visual_revision2, failed/imagegen_not_dispatched, retry_safe=true, generation_dispatch=not_sent. `thread_start_pending` с null native IDs сам по себе не является разрешением повторить generation. Сохранены выбранные факты, source/draft hashes и исходный operation binding. Единственное разрешённое назначение публикации — `street_story_e2e_20260928_tg`.

## Узкие runtime исправления после первой поставки

- Backend CI exact7f759d7: **866 passed, 3 skipped**; Android unit/lint/build, Android15 smoke и signed release exact95a8 успешны.
- Natural identity-only run37303563480 exact95a8 остановился до createStory из-за guest DNS/EAI_NODATA; моделей и генерации не касался. Подготовлены штатный emulator `-dns-server` и проверка DNS до любых эффектов. Новый natural run ещё требуется.
- Runtime доказал starvation: первые20 eligible-for-state историй все пропущены, нужная история на позиции75. Scheduler теперь применяет размер порции20 после проверки пригодности и существующих задач; skipped/completed истории не задерживают следующие. Это размер порции, не общий лимит исследования.
- Новый запрос visual в существующей истории сначала читает исходную VibePublish operation. Unknown/running или unsafe failure сохраняют прежний context и блокируют повтор. Подтверждённый исход допускает отдельный авторский запрос: новый durable attempt, новая content revision и история предыдущих receipts; поздний старый worker не может записать новый context. Факты и draft сохраняются. Эти изменения ещё ожидают нового integration deploy.
- VibePublish PR37 (тот же tested commit87a1799, CI37302519375) merged74960c; существующие server/worker обновлены без второй среды. Последующий preflight выявил installed Codex0.160 versus исторический hard pin0.153 до app-server/thread start; совместимость проверяется до повторной генерации.
