# Street Story — аудит параллельной доводки до первого полного продуктового результата

**Дата:** 6 октября 2026  
**Тип:** исполнительное ревью текущей реализации. Это не новый аудит архитектуры и не предложение начать Street Story заново.

## 0. Решение

Street Story уже достаточно продвинут, чтобы перестать делать очередную большую серию общих прогонов «с фото с нуля» после каждого локального исправления.

Ближайшая продуктовая цель одна:

> **получить retained-публикацию в тестовой Telegram-группе штатным Android/Mira-путём и native readback, используя уже накопленные пригодные факты.**

Дополнительное исследование «Найди ещё» остаётся обязательной отдельной приёмкой накопления знаний, но **не блокирует первую публикацию из уже пригодных фактов**.

Основной недостаток текущего процесса — параллельность существует как одновременная правка одного checkout, а не как независимые продуктовые участки с собственным runtime acceptance. Это ускоряет написание кода, но плохо ускоряет доказательство продукта и оставляет root-агента единственным исполнителем последнего километра.

## 1. Проверенный текущий срез

На момент ревью:

- рабочий проект: /home/dev/projects/street-story;
- единственный Git worktree;
- deployed backend health HTTP 200;
- runtime/source SHA: **b8464da773b305bb3803df8c812d26a4f277103c**, PR #171;
- свободно около **5.8 GB**, прежний disk-full блокер устранён;
- main work: work_858b8ffd2a0f44a0425aa4ae, revision 64, active;
- retained test destination остаётся street_story_e2e_20260928_tg.

Последний содержательно важный fresh full_social прогон дошёл значительно дальше прежних:

1. реальный JPEG автоматически определён через квалифицированный direct Google gemini-3.5-flash-lite;
2. результат прошёл общий visual gate;
3. из общей POI-memory получены **15 eligible facts**;
4. выбор двух фактов сохранён;
5. концепция и текст сохранены;
6. перед созданием изображения голосовая реплика была классифицирована как шум;
7. следующая корректная реплика привела к лишнему capability/stage transition;
8. Google Live закрыл соединение 1011;
9. **image generation не была отправлена**, поэтому неизвестного внешнего эффекта на этом шаге нет;
10. retained Telegram-публикации из этого прохода нет.

Это важный сдвиг: базовые identity/reuse/editorial стадии уже не являются главным неизвестным. Первый незакрытый продуктовый участок теперь находится **между готовым текстом и фактической генерацией/публикацией**.

## 2. Что уже не нужно заново доказывать перед каждым исправлением

Сохранить как работающие основания:

- POI-memory действительно переиспользуется: свежая история получила 15 фактов;
- имя объекта не требуется от пользователя в нормальном path;
- direct Gemini 3.5 Flash-Lite уже дал реальный visual MATCH по исходному JPEG;
- Luna и другие visual routes имеют отдельные qualification controls; не устраивать новый model tournament;
- выбор фактов, концепция и draft могут существовать в одной story;
- Stop/Resume, durable queues и сохранение story уже имеют отдельные проверки;
- ранее исправлены crop/fact-label проблемы итогового изображения; их нужно проверять на финальном asset, а не перепроектировать renderer заново;
- publication shortcut вручную запрещён: результатом считается только штатный путь с отдельным confirmation и native Telegram readback.

## 3. Главные проблемы текущей параллельности

### P1. Все writers меняют один и тот же checkout

Сейчас единственный worktree одновременно содержит незакоммиченные изменения как минимум в:

- live.py;
- headless_facts.py;
- headless_vision.py;
- research_adapter.py;
- opencode_research.py;
- shared_devcoveer_research.py;
- poi_memory.py;
- installer;
- соответствующих tests;
- плюс два новых test-файла.

Это не безопасная независимая поставка. Нельзя достоверно сказать:

- какой агент отвечает за конкретный diff;
- какой набор runtime-проверок относится именно к этому diff;
- какой кусок можно откатить независимо;
- не переписал ли один агент изменение другого;
- что именно root реально принял.

**Не выбрасывать текущий dirty diff.** Сначала root фиксирует его как интеграционный checkpoint/patch и заканчивает уже начатую проверку. Но после этого субагенты не должны одновременно писать в один checkout.

Новая дисциплина:

- один root/integrator владеет canonical checkout, merge и deploy;
- scoped agent либо работает в отдельном worktree/branch, либо остаётся runtime/read-only tester и возвращает exact patch/handoff;
- shared critical files нельзя одновременно мутировать нескольким агентам;
- root принимает patch только вместе с runtime evidence участка.

### P2. Субагенты первоначально получили задачу «починить», а не «довести свой кусок до продукта»

По свежему журналу это уже было замечено владельцем: первый быстрый ответ fact_scopes свёлся к коду и локальным тестам. После замечания задания были расширены до real runtime segment acceptance, но на момент ревью нет трёх независимых завершённых segment receipts.

Для каждого агента результатом должен быть не «51 test passed», а:

- конкретная production/test story;
- конкретная owner-like команда;
- фактический provider/model/transport;
- state before/after;
- duration;
- retained evidence;
- если был дефект — минимальный patch;
- повторная проверка того же сегмента после patch.

### P3. Самый важный поздний участок вообще оставлен root-агенту

Владелец предлагал отдельно тестировать:

1. идентификацию;
2. поиск фактов;
3. reuse фактов;
4. выбор → концепция → текст → картинка → публикация.

Фактически root оставил пункт 4 себе, а три субагента занимаются identity/facts/reuse.

Это замедляет именно то, чего ещё нет: **retained публикацию**.

Нужен четвёртый scoped product agent: editorial_publish. Он не исследует POI заново. Он стартует с уже готовой подтверждённой story и доводит только позднюю часть до Telegram.

### P4. Root остаётся integration + debugging + acceptance bottleneck

Root сейчас одновременно:

- принимает изменения агентов;
- меняет live.py;
- чинит installer/model qualification;
- расследует Live 1011;
- запускает full Android;
- следит за facts/reuse;
- должен визуально проверять asset;
- должен проверять Telegram readback.

Это слишком много последовательной работы для одного исполнителя.

Root должен делать только три вещи:

1. сохранять единый контракт и разруливать пересечения patches;
2. выполнять единый deploy интегрированного SHA;
3. постоянно гонять один общий full path и отдавать первый найденный blocker соответствующему segment-owner.

## 4. Текущие технические блокеры, которые нельзя маскировать очередным full rerun

### B1. Late-stage Live routing: готовый текст не доходит до image operation

Последний свежий сценарий уже имел identity, факты, выбор, concept и draft. Image request не был dispatched из-за двух независимых событий:

- voice turn был заблокирован как suspected noise;
- последующий текст вызвал лишний переход capability/stage, после чего Live завершился 1011.

Текущий diff уже исправляет инструкцию: если generate_visual доступен в текущем research/editor context, не надо переключать stage.

**Приёмка исправления:** продолжить сохранённую готовую story после reconnect и добиться фактического generate_visual operation. Не начинать ради этого снова с фото.

Live disconnect до tool dispatch — известный закрытый исход, поэтому допустимо восстановить тот же story context и повторить только owner image intent.

### B2. Background research создаёт retry storm и съедает ресурсы/наблюдаемость

Живые логи во время ревью показывают старые research jobs с сотнями попыток (attempt > 500). Повторяются:

- research_attempt_binding_changed;
- gemini:article_url_discovery_unavailable;
- RESOURCE_DAILY_BUDGET;
- gemini.all_keys_unavailable;
- gigachat_attempt_outcome_unknown;
- research_chunk_busy.

Это не означает сотни полезных provider calls. Но это означает, что scheduler регулярно будит заведомо неготовую работу.

Исправить **без новой queue architecture**:

- RESOURCE_DAILY_BUDGET ждать до реального policy reset/изменения доступности, а не делать минутный hot loop;
- all_keys_unavailable ждать изменения route health/cooldown;
- research_attempt_binding_changed не повторять неизменный binding бесконечно — требуется reconciliation/new binding event;
- gigachat_attempt_outcome_unknown только reconcile исходного addressed attempt, без нового inference;
- research_chunk_busy будить по lease/cursor event либо с bounded backoff;
- старые test stories, которые владельцу больше не нужны для исследования, останавливать штатным Stop/control API, **не raw SQL и не удалением знания**.

Interactive owner story/publication имеет более высокий scheduling priority, чем backfill старых тестовых research jobs.

### B3. Google/OpenCode/Giga unavailable не должны блокировать публикацию уже известных 15 фактов

Первый publication pass не должен запускать новый web search.

Если POI подтверждён и в памяти есть пригодные факты:

hydrate → select → concept → draft → visual → confirm → publish

должен работать при полностью недоступном web search.

Поиск нужен только для explicit MORE / gap research.

### B4. Прямой Gemini 3.5 visual route уже работает, но его qualification persistence пока находится в dirty integration diff

Текущий diff расширяет сохранение qualification metadata для gemini-3.5-flash-lite и разрешает использовать verified web-search route как direct visual executor.

Задача — закончить этот узкий change и regression, а не снова проводить исследование «какая vision-модель лучше».

### B5. Reuse improvements продолжают затрагивать canonical POI review state

Текущий dirty diff в poi_memory.py переносит review state между доказанными aliases, не даёт старым story projections воскресить withheld assertion и объединяет evidence одного URL по alias-family.

Это полезно, но не должно блокировать retained publication из уже загруженных 15 eligible facts.

reuse-агент должен закончить runtime acceptance отдельно и отдать patch root.

## 5. Правильная параллельная схема с этого момента

### Agent A — identity_vision

**Область:** только object identity / visual evidence / route fallback.

**Не владеет:** facts semantics, concept, text, publication.

**Runtime acceptance:**

- owner-like Sackheim photo без подсказанного названия;
- OSM/Wiki shortlist;
- реальный SOURCE/REF;
- direct Google 3.5 Lite actual request;
- общий gate;
- проверить один квалифицированный независимый fallback без повторения уже известного successful call;
- сохранить story_id, candidate/POI, model, refs, reviewed-image count, duration.

**Готово**, когда новый или сохранённый owner-like story автоматически получает match и canonical POI без ручного имени, а primary/fallback не теряют pixels/provenance.

### Agent B — facts_more

**Область:** поиск и извлечение фактов, explicit MORE.

**Не владеет:** POI canonicalization, image generation, publication.

**Runtime acceptance:**

1. взять подтверждённую story с уже гидратированными фактами;
2. explicit owner command «найди ещё»;
3. сначала использовать saved unread/partial sources/checkpoints;
4. если cached scope исчерпан — выполнить фактический новый discovery;
5. сохранить хотя бы один **новый eligible fact или новый eligible supporting evidence**;
6. Android/story projection обновилась;
7. selection/concept/draft не изменились.

Провайдерный ответ в логе не считается результатом.

Если providers реально недоступны, агент должен довести корректный durable waiting/backoff, а не объявлять MORE успешным.

### Agent C — poi_reuse

**Область:** existing POI memory, aliases, dedup, review propagation.

**Runtime acceptance:**

- новая story того же физического объекта через другой доказанный alias;
- без нового web research получает тот же canonical POI;
- получает полный пригодный reusable fact set;
- withheld/disputed fact не воскресает из старой story;
- один URL/evidence не размножается из-за alias;
- owner selection/draft из другой story не копируются.

Этот агент не должен несколько часов искать новые факты: его цель — доказать reuse.

### Agent D — editorial_publish

**Это теперь самый приоритетный агент.**

**Область:** выбор → concept → draft → visual → separate confirmation → Telegram.

Он стартует **не с нового фото**, а с существующей подтверждённой story, где уже есть 15 eligible facts. Использовать свежую story, дошедшую до concept/text, если её revisions/fences позволяют безопасно продолжить.

**Runtime acceptance:**

1. два evidence-backed fact выбраны;
2. concept сохранён;
3. draft сохранён;
4. reconnect не теряет story context;
5. owner image intent вызывает generate_visual без лишнего stage switch;
6. operation имеет известный authoritative outcome;
7. финальный asset просмотрен: нужный объект, нет crop/white bars, выбранные факты читаемы и не сокращены до бессмысленных дат;
8. prepare publication;
9. отдельная owner confirmation;
10. retained Telegram post в street_story_e2e_20260928_tg;
11. native/provider readback подтверждает именно этот item и asset/text.

**Не делать MORE. Не искать новые источники.** Web search outage не должен влиять на этот сегмент.

## 6. Как агентам реально работать параллельно

### Сейчас, с уже dirty checkout

Не терять текущую работу и не затевать часовую реорганизацию Git.

1. Root делает **один snapshot/capture текущего diff** как recovery point.
2. Останавливает новые параллельные записи в canonical checkout.
3. Три уже работающих агента заканчивают текущие анализы и возвращают:
   - files touched;
   - patch/diff;
   - tests;
   - runtime receipt;
   - unresolved blocker.
4. Root интегрирует текущий готовый diff в минимальное число commits и deploy.

### После этого checkpoint

- каждый writing agent получает отдельный branch/worktree **или** возвращает patch/handoff, не меняя canonical checkout;
- runtime testing может идти параллельно против одного deployed SHA;
- deploy выполняет только root;
- shared files (live.py, research_adapter.py, scheduler/installer) имеют одного writer на конкретную итерацию;
- если агенту нужен shared-file change, он отдаёт root точный patch и reproduction вместо прямой конкурентной записи.

Это достаточная изоляция. Не нужно создавать новую orchestration platform для агентов.

## 7. Root/integrator loop

Root не ждёт окончания всех агентов.

Цикл:

1. deploy один известный integration SHA;
2. параллельно запустить A/B/C/D segment acceptance;
3. одновременно root запускает один full_social;
4. первый новый blocker классифицировать и передать владельцу соответствующего сегмента;
5. segment patch + evidence → root review → merge;
6. новый deploy только если blocker реально изменён;
7. не повторять full flow при неизменной причине;
8. как только D получил retained Telegram post — зафиксировать первый продуктовый PASS;
9. после этого закончить B (MORE) и один fresh end-to-end pass на окончательном интегрированном SHA.

## 8. Что считается первым продуктовым PASS

Первый PASS не требует идеального MORE и не требует полного исследования всех источников.

Нужно в **одной story**:

- фото;
- автоматический verified identity;
- reusable evidence-backed facts;
- owner selection;
- concept;
- draft;
- generated visual;
- owner review;
- separate publication confirmation;
- retained test Telegram post;
- native/provider readback.

Допускается использовать уже накопленные факты POI.

После этого отдельно закрывается **MORE acceptance**:

- новый eligible fact или новое eligible evidence;
- сохранено в POI memory;
- видно в Android/story projection;
- прежний selection/concept/draft не изменены.

Финальный fresh pass после интеграции подтверждает, что сегментные исправления не требуют ручных внутренних tool names или seed URLs.

## 9. Запрещённые способы «ускорения»

Не делать:

- manual/API tail publication;
- raw SQL для принудительного identity/facts;
- новый обязательный reviewer/helper chain;
- повторный model tournament;
- новый POI storage;
- ожидание идеального facts coverage перед публикацией;
- обязательный MORE перед visual;
- новый full run после каждого unit patch;
- одновременную запись нескольких agents в один shared checkout;
- сброс unknown/possibly-sent operations;
- новый inference для reconciliation, если addressed result можно прочитать без send;
- считать CI или модельный JSON продуктовым PASS.

## 10. Приоритет ближайших действий

**P0 — сейчас:**

1. сохранить текущий dirty diff;
2. интегрировать уже готовый image-tool/direct-Google qualification fix;
3. запустить editorial_publish на сохранённой story и довести до retained Telegram post;
4. параллельно Agents A/B/C выполняют свои реальные segment acceptance;
5. остановить hot-loop старых test research jobs штатными controls / правильным durable backoff.

**P1 после первого retained post:**

6. принять segment patches A/B/C;
7. доказать MORE;
8. один fresh full_social на итоговом SHA;
9. только после этого обновить D1–D12 report и owner APK.

## 11. Финальный отчёт root

Кратко, без новой архитектурной главы:

- deployed SHA;
- story ID полного PASS;
- identity model/route + evidence;
- initial reused facts count;
- selected fact IDs;
- concept/draft revisions;
- visual operation + asset SHA;
- Telegram destination + retained item/publication ref;
- native readback;
- MORE story/run + new fact/evidence IDs;
- результаты A/B/C/D;
- один список реально оставшихся неблокирующих ограничений.

Если retained post ещё отсутствует — не писать «реализация завершена».
