# Street Story — исполнительная постановка R3: быстро определить объект, отдельно отладить этапы, поставить продукт

Дата: 06.10.2026. Это продолжение существующей реализации, не новый общий аудит и не создание новой архитектуры.

## 0. Основание и актуальность

Прочитай [аудит по трём свежим голосовым владельца](../audits/visual-fast-path-owner-voice-20261006.md). Владелец НЕ передавал предыдущую R2 агенту. R3 заменяет её как единая следующая постановка, сохраняет критические гарантии и добавляет проектирование короткого visual critical path. Не запускать одновременно R2/R3 и не утверждать, что агент уже выполнил R2.

Подтверждённый read-only baseline 06.10 05:47 UTC: canonical `/home/dev/projects/street-story`, ветка `work/street-story-mvp-20260908`, чистый HEAD `b2ca7f1e391ee4a183d13d2d8845f5f4a34e8c8a` (PR179). Work `work_858b8ffd2a0f44a0425aa4ae`, последний известный rev79. Сверь свежий state один раз; не откатывай новое состояние к SHA документа и не переключай source на документационную ветку.

Сохранённая остановленная story: `story_6f4b0f6e9495ec91187758dd`, run37410074630. У неё pending точечные статьи и большая gallery queue, explicit identity Stop; imagegen/publication в этом run не начинались. Найди уже подготовленный source-priority PATCH с34проверками и projection receipt в известном work/artifact handoff. Пустой managed source_handoff_list не означает отсутствия unmanaged artifact. Если patch отсутствует, это не повод восстанавливать весь проект: воспроизведи узкий дефект.

Исторические retained посты37503/37506 и отдельный MORE PASS уже есть. Это действительные segment/resumed результаты, не свежий полный PASS. Не добывать заново «самый первый пост» и не требовать бесконечно новые факты на том же объекте.

Голосовые: IdeaHub commit `ea83b680eabca0ceabae9bd42bb6794d03409649`, packets `voice-20261006-072815-b9a77546`, `voice-20261006-073457-d984a653`, `voice-20261006-073712-b617b2f1`. Полные расшифровки и их ссылки в аудите. «До5изображений» — предложение малого batch, не API-лимит. Вопросы о раннем выходе/регулярках — проверяемые гипотезы, не основание переписать всё.

## 1. Продуктовая цель и неснижаемые гарантии

Нормальный маршрут: настоящее фото → автоматическое установление физического объекта без обязательного имени от пользователя → общая POI-memory/новые evidence-backed facts → выбор → концепция → видимый draft → generated visual → просмотр → отдельное подтверждение → retained test Telegram post/native readback → подписанный update APK.

Неполнота фактов допустима. Нельзя ради неё блокировать работу с хорошими фактами. Нельзя ради скорости принять другой объект, скопировать чужую selection/draft, придумать evidence, молча потерять owner intent, обойти Stop/consent или повторить possibly-sent генерацию/публикацию.

Сохрани все11 требований `.devcoveer/requirements.json`, LLM-first семантику, Live-first малые fact operations, existing shared POI-memory, device token/auth/WSS/signing. OpenCode/Giga/другие модели — уже разрешённые целевые дополнительные исполнители, не новая обязательная платная лестница. Не менять требования или провайдерный бюджет молча.

## 2. Исправить алгоритм, а не только счётчик или очередь

### A. Использовать достаточное доказательство и остановиться

Ранний выход УЖЕ есть в `_record_place_comparison` и `HeadlessIdentity.run`. Сохрани его и добавь регрессию отсутствия последующих send после accepted MATCH. Не внедряй набор баллов, обязательное второе/третье подтверждение, просмотр всех картинок или правило «минимум Nсовпадений».

Одного доказанного визуального совпадения с корректно связанным физическим POI обычно достаточно. Когда есть реальная неоднозначность, одно точечное уточнение; не переключать модели до удобного «да». Self-reported confidence не считать измеренной вероятностью. После MATCH новая работа по этой identity останавливается; источники/эталоны/receipts сохраняются, неиспользованный decoded media чистится существующей retention вне критического пути.

### B. Reuse-first, не search-first

Три разные операции:

- Exact SOURCE/REF с действительным сохранённым verdict: reuse без нового inference, затем текущие scope/Stop/identity guards. Не переносить старое разрешение или создавать owner_confirmed.
- Новое фото возможного уже известного POI: сначала ранее удачные связанные REF, НОВОЕ сравнение с этим SOURCE; новый web search пока не нужен.
- Есть только статьи/частичные gallery: использовать сохранённые bytes/descriptors/cursors; text-complete не означает gallery-complete.

`candidate_article_sources` сейчас возвращает все URL по last_seen_at. Добавь минимальный lookup на уже принятые reference_evidence/изображения и их принадлежность POI. Не новый POI-layer, не перенос таблиц, не embeddings/векторная БД и не пересканирование всего архива на каждом фото. Ссылки на existing evidence предпочтительнее новых копий.

### C. Устранить блокирующие барьеры

- В `live_visual_comparison.py` убрать зависимость чтения полезных pending источников от полного опустошения queue. Использовать structural source→candidate/POI provenance и ограниченный квант/чередование. Просто дописать полезные кадры в хвост не считается исправлением.
- В `article_media.article_candidates` вернуть пригодные static media до ненужного JS gallery ожидания; partial/cursor сохраняются. В recovery не удерживать первую полезную страницу общим ожиданием всех страниц. Использовать существующие readers/queue, небольшой prefetch и выдачу готовых порций, не новый pipeline server.
- В `headless_identity.py` completed mismatch — normal progress, не error/retry c обязательной3секундной паузой перед каждым кадром. Продолжать готовую порцию в bounded unit с yield/Stop/admission; истинный backoff только по причине. Не отключать лимиты и не запускать бесконечную нагрузку.

В первую очередь обслуживать доказанные POI REF и точечные sources shortlist, но не навсегда исключать менее вероятные гипотезы. Одна большая галерея не монополизирует critical path. Не создавать тематический blacklist доменов, очередной hardcap50/100изображений или regex-рейтинг «истинности».

### D. Небольшие группы вместо обязательного попарного обхода

Добавь совместимый grouped вариант к существующему vision adapter. Начальная проверяемая настройка — SOURCE плюс до4REF; если готов1полезныйREF, не ждать остальные ради полного batch. В группе желательно разнообразие источников/ракурсов, не четыре соседних почти одинаковых кадра.

Для direct Gemini использовать отдельные маркированные image parts, если установленный SDK/adapter это поддерживает. Не мигрировать на другой API по примеру документации. В Live не считать длинный collage доказательством доставки достаточного разрешения: проверить настоящие model-visible pixels/labels. Pair-mode оставить рабочим резервом. Размер batch уменьшается при resource/image-size ограничении, а не отправляется тот же чрезмерный payload повторно.

Одним запросом решать, какие REF относятся к каким физическим гипотезам и дают ли они достаточное совпадение с SOURCE. DOM, alt, h1, JSON-LD — контекст, а не verdict. Для смешанной статьи передавать короткий адресуемый figcaption/heading/context, когда он есть. Если принадлежность REF сомнительна, не публиковать guessed identity; выбрать более надёжный REF/точечное уточнение.

**Не добавить обязательные classifier→matcher→reviewer для каждого кадра.** Grouped ответ может сразу дать accepted MATCH. Высокое разрешение/второй вид запрашиваются только при недостаточности конкретного результата. Не классифицировать сначала все172изображения: это снова длинный критический путь.

Для нескольких кадров одной статьи нужны стабильные `reference_id`, отдельные от candidate_id/web:URLhash. Verdict обязан ссылаться на конкретный переданный REF. Добавь mapping label→ref→imagehash→source→subject. Не подменять/склеивать images, не принимать только название статьи. `_record_place_comparison` не должен помечать все элементы batch просмотренными, если ответ оценил только часть. Malformed один элемент изолируется, корректные результаты не теряются; неоднозначные разные POI не разрешаются правилом fastest-wins.

### E. Пределы semantic authority

Сохранить source-validated/model-decided gate. Из R2 остаётся узкая регрессия `_explicit_page_alias`/`merge_candidates`→`subject_aliases`: два разных здания с взаимным упоминанием/общей фотографией без QID не должны становиться authoritative aliases. Слабая связь может остаться retrieval hint. Без доказательства кандидаты раздельны; не добавлять обязательную новую модель и не мигрировать всю POI-memory.

Проверить normal fact trace в `mvp_research.py`/`research_adapter.py`: отсутствие/отказ helper не блокирует Live и уже пригодные facts. Не выключать доказанный полезный optional worker, но и наличие ключа/qualification само по себе не делает его обязательным default. Не начинать новый общий фактологический аудит.

## 3. Отдельно отладить стадии до fullrun

Используй existing `LiveGoldenInstrumentedTest`, workflow и runtime probes, не создавай второй harness. Сейчас `identityOnly + resumeStoryId` запрещены настроке135: добавь безопасный scoped resume для unresolved saved story с текущим photo/control fence, сохрани запрет replay публикации. Стадийный тест не вправе выполнять незаказанный хвост.

Нужны адресуемые границы проверки: object search, visual compare, fact research/MORE, fact selection, concept, draft, visual generation/review, publication. Используй уже сохранённые реальные входы и PASS, не повторяй каждый сегмент заново без изменения кода/контракта.

У каждого segment-owner: допустимый вход, ожидаемый durable выход и UI, reproduction, patch, scoped tests, фактические durations/provider/refs и первый blocker. «Написал код/pytest green» без runtime результата либо точного внешнего блока недостаточно. Seeded/frozen input допускается для component test, но не доказывает предыдущую стадию и не считается fresh E2E. Production verified state не создаётся raw SQL.

Параллельны непересекающиеся source patches/offline проверки и подготовка отдельных состояний. Не нужно8одновременно работающих агентов. Один integrator/deployer; shared-file writer один. На одном runtime/story/аккаунтном слоте не запускать конкурирующие live tests. Root не гоняет full_social одновременно с каждым незавершённым segment fix.

Порядок: local reproduction → targeted tests → segment runtime → единая интеграция/обязательная release CI → один fresh full_social. Внешнее окно Android/текстовый WSS/эмулятор не выдаются за доказательство физического микрофона.

## 4. Минимальные проверки нового visual path

Без реальных provider затрат: early exit без следующих send;172gallery+2pendingисточника без starvation в обе стороны; partial JS не удерживает готовый static REF; медленная страница не удерживает другую; completed mismatch не получает failure backoff; поздний источник; repeatedbytes; label/ref permutation; invalid/partial batch; fetched≠reviewed; Stop→restart остаётся stopped; explicit Resume использует новую актуальную revision; смена фото отсекает поздний результат; unknown attempt не повторяется.

Маленький реальный comparative test на одном уже доступном qualified vision route: pair и SOURCE+до4REF на сохранённых контрольных материалах. Достаточно success, unrelated negative, правильный последнийREF, mixed-page/wrongtitle, сложныйракурс/uncertain. Проверить реальные pixels, correctsubject и tokens/latency; другие провайдеры не участвуют в турнире. Если групповой формат не проходит контроли, уменьшитьbatch/точечно повыситьdetail либо выпустить уже ускоренный checked pair fallback — не зависнуть на идеальном batching. Полученный default должен быть реально испытан.

Метрики: time_to_first_useful_ref, identity latency, acquisition/admission/inference отдельно, counts search/fetch/model, tokens, reusedref/exactverdict отдельно, falseaccepts controls. Стартовый ориентир20–30секунд на обычном известном тестовом объекте при доступных ресурсах; это не измеренныйSLA и не разрешение по таймеру принять недоказанное. При долгой работе — понятные partial/waiting/Stop/продолжение, не вечный spinner. Проверка эффективности обязана показать, что полезная конкретная статья достигается без опустошения172кадров.

После stagePASS продолжить6f4историю обычным Resume, отметить exact-cache hit честно. Затем один новый полный Android/Mira путь: автоidentity → факты → выбор → concept → draft → новаяgeneration → visualreview → отдельныйconfirm → retained `street_story_e2e_20260928_tg` post/native readback. Для new-photo recognition использовать реальное фото без exact-pair cache, не очищая POI-memory; оно может входить в fullrun или короткий identity-test. Пригодные факты из памяти разрешены. MORE не ставить перед публикацией и не требовать нового факта от каждого повторного запроса.

## 5. Ресурсная готовность и поставка

До дорогого полного теста проверить текущий read-only admission coding executor, vision/search и отдельно VibePublish imagegen. Прежняя native Codex quota была исчерпана; ordinary Vibe imagegen может использовать тот же account scope. Не выводить текущий outage из старого receipt, не покупать кредиты/не менять аккаунт/не добавлять платныйAPI молча. При реальном внешнем стопе закончить доступные patches/регрессии/segments, сохранить точный checkpoint. Новую картинку нельзя подменить старой ради freshPASS.

Сохранённые готовые stages не переигрывать для отладки хвоста. Unknown generation/publication сначала наблюдать по исходномуID, не повторять под новым. Никаких manual Telegram tail или production destination. Финальную картинку проверить на fullframe/читаемые выбранные facts; отдельное согласие не заменяет текст «готово» от Миры.

Поставка: рабочий проверенныйbackend, retained новый полный testpost сreadback, подписанныйAPK/update поверх установленного без потери данных, короткие segment receipts и first-useful-result timings. Не перестраивать зависимости только ради SHA: совместимость сервисов по версиям/capabilities, SHA для provenance. Не выдаватьv838 как финальное исправление при известной starvation.

## 6. Критерий остановки работы

Нормальный путь короткий и измеренный, подтверждает правильный объект и доходит до публикации; память/выбор/Stop/consent сохраняются при типовых сбоях; signed update проверен. Частично ненайденные факты, нерассмотренный хвост ненужных картинок и недоступность одного резервного провайдера не запрещают результат. Известная потеря текста, ложное отождествлениеPOI, повторныйunknownsend или неподтверждённый финальныйгенератор запрещают заявление о готовности.

Отчитайся: изменения по F1–F7, реальный выбранный pair/grouped профиль и сравнение затрат, что reused/что проверено заново, стадии и полныйstory/run, новаяvisualoperation, отдельныйconsent, retaineditem/readback, APKupdate. Укажи только фактически оставшиеся ограничения. Не завершай словами «ещё немного архитектуры», «только новый аудит» или «готово, потому что тесты зелёные».
