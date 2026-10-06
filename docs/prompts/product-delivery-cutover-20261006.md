# Street Story — product cutover: выдать владельцу тестируемый результат

Дата: 06.10.2026.

Это **исполнительная постановка на завершение поставки**, а не новый аудит. Она заменяет R3 как ближайший execution plan. R3 остаётся источником идей и regression/backlog; не реализовывать его дополнительные улучшения до owner-testable результата, если fresh full run не докажет конкретный blocker.

## Текущее состояние, не перепроверять с нуля

- PR #180 открыт, head `a01035428272bab137386bd35c0c0688d6db3fec`.
- Обязательные GitHub backend + Android checks + emulator на этом head уже green.
- PR mergeable/clean.
- Есть штатный retained Telegram post **37506** с separate confirmation/native readback и отдельно доказанный MORE.
- Они сохраняются как segment evidence, но не заменяют fresh полный pass на новом коде.
- Неполный recall фактов допустим. Уже пригодные source-backed facts из общей POI-memory можно и нужно использовать для публикации.
- Kimi K3 consultation `dvt_ff816bd2a18d4f5490281dee8aa84c5b` независимо рекомендует тот же cutover: saved6f не делать pre-gate; merge → deploy → один fresh full_social → signed APK + post/readback.

## Единственный ближайший DoD

На **одной новой story** после merge/deploy:

`photo → automatic identity → evidence-backed usable facts → owner selection → concept → visible draft → NEW visual → owner review → separate publication confirmation → retained street_story_e2e_20260928_tg post → native/provider readback`

Затем владельцу переданы:
- подписанный update APK этого release;
- story/run ID;
- ссылка/item ref свежего test-post;
- коротко: что прошло и максимум 1–3 реально оставшихся неблокирующих ограничения.

Это owner-test candidate. После выдачи — ждать owner feedback, а не продолжать polishing автоматически.

## Сделать сейчас — максимум пять шагов

1. **Merge PR #180 сейчас**, если head не изменился и обязательные checks всё ещё green. Не запускать дополнительный local full suite и не добавлять новый review gate.
2. **Один deploy merged SHA.** Signed APK build/update можно запускать параллельно, не блокируя full_social.
3. **Один fresh `full_social keep=true`.** Не выполнять перед ним saved6f identity Resume и не делать MORE. Не подсказывать название объекта/internal tools. Использовать существующую POI-memory.
4. **Если PASS:** визуально проверить final asset, separate confirmation и native readback; выдать владельцу APK + свежий post/story refs и STOP.
5. **Если FAIL:** назвать ровно первый продуктовый blocker. Исправить только его; проверить затронутую стадию на **той же fresh story**, сохранив состояние. После segment PASS сделать один новый fresh full_social. Не открывать параллельно общий аудит/рефакторинг.

## Что НЕ является release gate до owner test

Не блокировать поставку ради:
- saved6f Resume component;
- дополнительного grouped-vision polishing, если текущий verified pair path работает;
- дальнейшего legacy/hashless reconciliation без свежего blocker;
- новых incident-note/checkpoint циклов для успешных рутинных шагов;
- повторных local/full suites сверх обязательных green branch checks;
- дополнительных модельных сравнений/турниров;
- полного покрытия всех источников или обязательного нового факта;
- улучшения метрик/SLA/telemetry, если текущего evidence достаточно диагностировать fresh run;
- cleanup исторических research stories/work records, если они не мешают текущему run.

`request_key`, `expected_head`, idempotency и receipts остаются внутренними safety-механизмами инструмента. Они не создают отдельные продуктовые этапы и не требуют длинного human-facing отчёта.

## Пять гарантий, которые нельзя убрать ради скорости

1. **Autoidentity:** normal path не требует, чтобы владелец назвал объект. Uncertain честно остаётся uncertain.
2. **Evidence:** публикационные факты source-backed; semantic truth/identity не решаются regex/счётчиком.
3. **Owner control:** selection/concept/draft не меняются скрытно от background work.
4. **Side-effect safety:** unknown/possibly-sent generation или publication не повторяются вслепую; отдельный publish consent обязателен.
5. **Release integrity:** signed update, auth/device token/WSS и test-only destination сохраняются.

Если одна из этих гарантий реально нарушена в fresh run — это blocker. Неполное количество фактов — нет.

## Как трактовать external/provider failure

Один реальный provider/quota outage не является поводом строить новый orchestrator.

- Сохранить story и уже завершённые stages.
- Использовать уже существующий квалифицированный fallback, если он действительно доступен и подходит по capability.
- Если все разрешённые маршруты конкретной обязательной стадии действительно недоступны — зафиксировать точный resumable blocker и всё равно закончить доступные release/build шаги. Не объявлять ложный PASS и не менять аккаунты/бюджеты молча.
- При восстановлении продолжать эту stage/story, не начинать весь путь заново.

## STOP rule

Как только fresh run дал retained test-post + native readback и signed APK готов к обновлению — **прекратить внутреннюю доработку и выдать результат владельцу**.

Следующий цикл начинается только из owner feedback либо конкретного production blocker.

Не писать новую архитектурную постановку после PASS.
