# Street Story: реализация восстановления 10 октября 2026

Реализация находится в существующей ветке `chatgpt/street-story-recall-20261010`, [PR #253](https://github.com/onedayonemasterpiece/street-story/pull/253). Исходная продуктовая база — `e40dba990ff56a989811cdbd43825e146835fa45`; постановка и аудит прочитаны на закреплённом `9e7e511`. **Универсальная продуктовая приёмка пока не пройдена.**

## Реализованное ядро

Принятый upload принадлежит серверной durable задаче и переживает закрытие клиента и редакторские действия. Квалифицированное чтение объединяет extraction и собственную смысловую assessment; story и canonical POI используют одну immutable claim/subject/own-source basis. Compatible additions и recheck сохраняют доступность, выбор и черновик. Новое положительное assessment может адресно обновить ранее withdrawn claim; hydration не получает таких полномочий.

Same-URL merge сохраняет passages/версии. Lossless normalization не придумывает semantic flags. Bounded dispatch/refill и независимый lifecycle maintenance сохраняют полезный partial. G использует общий relation contract с реальными primitives, T получает полный retained body ограниченными порциями. Closed malformed T оставляет acquired REF доступными в исходном бюджете без третьего joint inference. SOURCE, generation/control, subject, Stop, quota и UNKNOWN fences сохранены.

Android сохраняет review status и связь visual bytes с версией, создаёт новый idempotency key для нового checkbox intent и отправляет research control перед обычным voice upload. Shared Live transport остаётся на locked `0.3.7-rc.1`. Requirements manifest не изменён.

## Измеренные результаты

Это сегментированные результаты, **не один cold/E2E прогон конечного SHA**. Все 15 исходных frozen фото проверены с сохранением bytes/EXIF; evaluator IDs и labels не передавались runtime. Исходные итоги: 2 PASS_CANDIDATE, 3 REVIEW_REQUIRED, 8 FAIL, 1 BLOCKED, 1 CLARIFICATION_REQUIRED. Нет общего процента качества и нет автоматического PASS при отсутствующей независимой truth.

- Cold102/104 дали правильные по существующей разметке physical IDs и 14/8 пригодных фактов. Cold107/120/125 дали полезные наборы, требующие независимой проверки identity. Остальные исходы сохранены unresolved, resource-blocked или требующими уточнения.
- Первый warm104 на `46a6aa5` механически дал 14 eligible/reused8, но нарушил continuity: семь исходных canonical facts withheld, один claim перезаписан. Он **не считается успешной warm-приёмкой**. Контроли воспроизвели обе ошибки. Исправление `00ea5d6` записывает subject перед hydration в той же транзакции и сохраняет admitted claim при явной модельной эквивалентности, добавляя incoming observation отдельно.
- Новый затронутый warm104 на `00ea5d6` сохранил **8/8 неизменённых eligible canonical proofs**, reused8, всего 13 eligible. Первый доступный факт — 73,8 с; useful/terminal — 104,1 с. `00c23d8` поправил observer-метрику: старое admission time относится к lineage, новая local projection определяет доступность в новой истории. Первоначальное «0 с» сохранено как ошибочный исторический показатель, не latency успеха.
- Реальное editorial continuation сохранило три выбранных факта, concept, draft, голосовое уточнение подготовленной записью, visual, Stop и reopen. 301/301 ACK для 100 ms PCM через in-process shared relay. Visual просмотрен: правильная башня, вечерний замысел и выбранный исторический subset; небольшие copy notes сохранены. Public WSS, физический микрофон и handset VAD этим не доказаны.
- Реальный assisted после нового ответа владельца не подтверждён: для124 не получено географическое уточнение. Адресные backend controls late owner-wave/Stop/UNKNOWN не называются реальным ответом автора.

На runtime `00ea5d6` [backend CI](https://github.com/onedayonemasterpiece/street-story/actions/runs/38094858939) прошёл: **2832 passed, 49 skipped**; [Android CI](https://github.com/onedayonemasterpiece/street-story/actions/runs/38094859029) прошёл checks и emulator: **29 instrumentation tests**. Warm controls: 131 passed; последний harness timing control: 27 passed. Старые backend contract failures и Pixel Launcher ANR сохранены отдельно; они не выданы за успешные финальные проверки. Точные checks конечного PR head доступны в PR.

## Остающийся дефект и границы

Одна новая bounded попытка130 после исправления malformed T приняла соседний `osm:way:193106140` вместо evaluator `osm:way:193106188`. Identity — 70,6 с; evaluator-scoped Stop — 71,4 с; пригодных фактов0. Это OPERATOR_STOPPED/wrong_physical_object, **не natural success и не owner Stop**. Формально допустимый proof не доказывает правильную привязку описания комплекса к отдельному корпусу. Разрешённые bounded G/T альтернативы исчерпаны; нужен новый отличающий источник/контекст, а не повтор до случайного PASS, третий joint call или подстановка evaluator ID. Сохранённый malformed ответ отдельно replayed в RAM с zero provider calls и неизменённым diagnostic/binding.

Production остаётся на `93a0878a80f0f7c6b0cef286e4b691f2462c0206`. Merge/deploy, внешняя публикация и возобновление ранее отменённого job не выполнены. Инцидент `inc_26cbb5bad820e3e794275118` остаётся открытым. Полная стоимость UNKNOWN: receipt sends являются lower bound, а отсутствующий usage не считается нулём.

Подробный retained отчёт и первичные evidence на devserver: `/home/dev/artifacts/street-story/20261010T213047Z-universal-recovery-20261010/recovery-report.md`. Он содержит source/runtime/readback SHA, по одному исходу на каждый вход, исходные timings, passage/source lineage, provider receipts, mode=ro canonical comparison и финальные CI-ссылки. Оригинальные базы и неуспешные попытки сохранены.

Предлагаемое дополнение `CORE-PHOTO-IDENTITY` для отдельного решения владельца: «После подсказки владельца, включая поздний ответ, продукт в той же истории сохраняет исходные SOURCE/Stop границы и получает правильный физический объект с собственными source-backed facts либо явно сообщает локальную неопределённость». Manifest в этой реализации не изменён.
