# Street Story: почему визуал 37802 заметно исказил фотографию

14 октября 2026. Ретроспективный read-only разбор двух последовательных визуалов одного дома. Генерации, изменения кода и deployment не выполнялись.

## Вывод для исправления

Найдено реальное ухудшение относительно ближайшего предыдущего результата 37796. В 37802 появились выдуманная длинная улица слева, деревья и другая геометрия ракурса. Предыдущий результат сохранял глухую левую стену и направление наклона крыши заметно лучше. При этом он уже немного фронтализовал фасад: объявлять его полностью неизменённой фотографией тоже нельзя.

**Доказанное изменение входа:** между версиями 9d853a7 и 3855383 к image brief добавлена обязательная инструкция точного портретного кадра 4:5, запрещающая обрезку и предписывающая расширять периферийный фон. Дополнительно новый тестовый диалог повторял требование расширить края. Основной image-v2 шаблон, source asset и тип операции tune не изменились. Поэтому прежнее объяснение «промпт разрешал достраивать края» само по себе недостаточно: такое разрешение было и до дефектного результата.

**Причинность ещё не установлена полностью.** Смена финального кадрирования и усиление инструкции достройки являются конкретным проверяемым кандидатом причины. По двум разным генерациям нельзя отделить её вклад от вариативности модели и изменённого текста подписей. Реальная image model/version, seed и полный executor request не восстановлены; смена модели не доказана.

Минимальный следующий шаг для Claude: сопоставить два сохранённых executor request по указанным operation/job IDs; затем исправлять именно конфликт сохранения исходной геометрии с обязательным расширением кадра. Не предлагать новый оркестратор или массовый прогон. Для проверки результата использовать эту исходную фотографию и оба сохранённых визуала; геометрия крыши, глухая боковая стена, положение корпуса и отсутствие выдуманной улицы должны быть отдельными критериями. Одного размера 1080×1350 и читаемых подписей недостаточно.

## 1. Сопоставимая пара

| Поле | Ранее: Telegram 37796 | Дефект: Telegram 37802 |
|---|---|---|
| Дата публикации | 13 октября 05:21:01 UTC / 07:21 Калининград | 13 октября 11:49 UTC / 13:49 Калининград |
| SHA генерационного кода | 9d853a7f0e952f6a7cc09c6628e316f66895e483 | 3855383472ae464062e0112e18e4cb05dba0becc |
| История | story_038bbcacdbc7aefd14a48354 | story_a36ff4712940eca6cd7268df |
| Visual operation | op_2049f7c3fa0749548b38adf716e536cb | op_08d12f57c50743439586886b21fc8363 |
| Visual job | visual_f6cffae43d6c436e94c5723ca916854d | В прочитанной сводке не восстановлен |
| Source asset | asset_873a389fd17148fdb469c68aba4ab43c | Тот же asset_873a389fd17148fdb469c68aba4ab43c |
| Selected result | asset_b4787475ebec4326b4770e5bf168be8a; SHA 38e50a59462a4bae2ac6f21e4d3374885757ec77310a9c53c8a362eda21e71ed | asset_d16cbb6aab584d0992a65e216e25acec; SHA 157e7a138f6c0966c89fae0ecfcf29debaa1f9233df61acd06af5452ae4875e5 |
| Реальные размеры | 1080×810, 4:3 | 1080×1350, 4:5 |
| Выбранные карточки | 5, другой сохранённый набор | 3, включая отдельный контекст улицы |
| Операция по коду | tune, один source, candidates=1, formats=[post_4_5], selection=human, copy={} | Те же поля |
| Recompose / regenerate | Одна новая генерация; восстановление исходника продолжило тот же запрос | Новая генерация для другой story; не механическое изменение размера старого visual |
| Реальная image model, version, seed | UNKNOWN | UNKNOWN |
| Полный финальный executor prompt/hash | UNKNOWN; известны шаблон, код сборки и сообщение тестового драйвера | UNKNOWN; известны шаблон, код сборки, frame suffix и инструкция драйвера |

В обоих случаях formats уже был post_4_5. Нельзя описывать изменение как первое появление запроса 4:5 или как доказанное переключение edit→generate. Изменилось подкрепление формата в самом brief и механическая проверка выходного кадра.

Generation SHA 3855383 не смешивать с последующим SHA 056931e, на котором восстановили публикационную карточку с точностью времени. Отдельная публикационная корректировка не доказывает повторной генерации картинки.

## 2. Что показало чтение пикселей

Исходный сохранённый JPEG: SHA 57922f5caea080943fe8263ef502d8d2edd463f0bbf496f2b69ec2af28f192f5. Сырые размеры 1824×1368, EXIF Orientation=6: нормальное отображение 1368×1824, портрет 3:4. Нельзя выводить «на вход подали landscape» только из сырых размеров JPEG. Этот файл восстановлен из ранее сохранённого просмотра, а не объявлен байт-в-байт оригиналом фото из реестра.

37796: направление наклона крыши соответствует исходнику; глухая левая боковая стена сохраняется; длинной новой улицы с деревьями нет. Вместе с тем крыша выглядит более выпрямленной, фасад более фронтальным, нижняя часть обрезана. Это относительно более точный baseline, не геометрически безупречный PASS.

37802: направление уклона крыши меняется, вместо глухой стены появляется длинная соседняя фасадная линия и деревья; нижний/правый контекст расширен вне фотографического доказательства. Это содержательное изменение сцены, а не подписи поверх исходной фотографии.

Источник baseline-пикселей: сохранённый native view_image, вызов1459, output1460–1663 (204 части), от 13 октября 04:50:31.140 UTC; SHA изображения совпал с selected SHA в записях1456/1460 основной native-сессии. Источник дефектных пикселей: view_image3431–3432 и receipt3430; хеш совпадает с выбранным Vibe asset.

## 3. Точный кодовый diff

Основной шаблон в [9d853a7](https://github.com/onedayonemasterpiece/street-story/blob/9d853a7f0e952f6a7cc09c6628e316f66895e483/backend/prompts/street-story-image-v2.txt) и [3855383](https://github.com/onedayonemasterpiece/street-story/blob/3855383472ae464062e0112e18e4cb05dba0becc/backend/prompts/street-story-image-v2.txt) идентичен:
- Git blob: 55a0df73fdadf0b858ea5c181e12ff1b8e3bbec7
- UTF-8 SHA-256: 92496e7fd70419af40312865f486907fecea9ab84fdb35edc0fbef427faec424
- Длина: 3369 символов

Шаблон уже раньше требовал сохранять perspective/rooflines/entrances, одновременно разрешая slightly recompose и extending/reconstructing edges. Это исторический конфликт пожеланий, не новая регрессия текста шаблона.

Раньше [mvp.py _visual_brief](https://github.com/onedayonemasterpiece/street-story/blob/9d853a7f0e952f6a7cc09c6628e316f66895e483/backend/street_story/mvp.py#L42-L113) завершался template + themes + language_rule. Теперь [mvp.py85–107](https://github.com/onedayonemasterpiece/street-story/blob/3855383472ae464062e0112e18e4cb05dba0becc/backend/street_story/mvp.py#L85-L107) резервирует место в лимите5000 и добавляет POST_4_5_FRAME_INSTRUCTION. Точный новый suffix из [product.py26–32](https://github.com/onedayonemasterpiece/street-story/blob/3855383472ae464062e0112e18e4cb05dba0becc/backend/street_story/product.py#L26-L32):

```text
OUTPUT FRAME: exact portrait 4:5 (1080x1350), full bleed; no white bars, blank padding or borders. Preserve defining architecture and every selected fact annotation. Extend peripheral background to fit; do not crop the subject, stretch it or clip labels. Verify the final image is 4:5.
```

SHA-256 suffix, включая два начальных LF: b63fd14c64f54b4b6e71b450b2f9d11cd83dfed034749e7dc78d550829fb6f8f, 287 символов.

[Command construction, новый код](https://github.com/onedayonemasterpiece/street-story/blob/3855383472ae464062e0112e18e4cb05dba0becc/backend/street_story/mvp.py#L410-L425) сохраняет tune с исходным asset. Соответствующий блок старого mvp.py имеет те же поля. Проверка frame_contract доказывает формат и lineage, но не измеряет сохранение геометрии.

## 4. Точные сохранённые инструкции тестового диалога

Это сообщения драйвера/visual_instruction, а не утверждение о полностью восстановленном executor prompt.

### До: сообщение Live-драйвера, native1353

```text
Создай по исходной фотографии финальную инфографику для Telegram о доме на Житомирской,22–24 в Калининграде. Только текущие выбранные пригодные факты: пятиэтажный дом с мансардой, началоXXвека по статьеКлопс25.06.2025, прежнее Нордштерн и торговое использование, уцелевшее строение разрушенного квартала, восстановление в конце1950-х. Для читаемости выбери небольшой набор из этих пяти. Сохрани исходное здание и его пропорции, текст внутри изображения без белых полей. Не утверждай нынешний ремонт или нынешнее расселение. Черновик и авторский выбор оставь. Вызови generate_visual; публикацию пока не готовь и не отправляй.
```

SHA-256 UTF-8: 2ffc006b78c9956e43bbef04bea04757260375dfab8c6a458af935d61047822f; 624 символа. Live мог сформировать собственный visual_instruction; его точный frozen текст ещё не прочитан.

### После: visual_instruction драйвера, native3403

```text
Строго портретный формат 4:5 на весь кадр без полос и полей. Используй исходную фотографию: сохрани выбранный дом, его архитектуру и пропорции; при необходимости расширь окружение по краям. Покажи все три текущих выбранных подтверждённых факта с читаемыми подписями и существенными оговорками. Не добавляй новых фактов и не меняй сохранённый текст.
```

SHA-256 UTF-8: b6e4d9db046e6ec81c541e2019c4280b6834e8c24588c1ba27d910e407d82bcf; 348 символов. Драйвер просит передать эту строку в generate_visual. Конечный запрос нижележащему executor отдельно не восстановлен.

## 5. Почему дефект пропустили

13 октября11:18 исполнитель ещё до публикации записал: ракурс изменён, нижняя/правая сцена достроена, точная геометрия входа не проверена. При этом visual-review.json поставил architecture_preserved=true и сузил review_scope до узнаваемости основных деталей, исключив точную геометрию. Поэтому дефект не был скрытым исключительно downstream эффектом Telegram: его признаки наблюдали до отправки, но критерий приёмки позволил пройти.

Механические проверки размера, хеша, актуального выбора фактов и читаемости полезны, но не заменяют сравнение фотографии и результата. Причину возникновения надо искать в request diff; причину допуска уже показывает сохранённый review.

## 6. Охват и недостающие доказательства

- Независимо просмотрены ближайший baseline37796, дефект37802 и сохранённый исходник.
- Более ранние многочисленные прогоны не восстановлены попарно. Слова владельца о первой заметной проблеме сохранены как свидетельство владельца; ретроспектива не превращает их в просмотр всей истории.
- Текущий Vibe connector не показывает тестовый alias. Его точное feed-чтение вернуло access_denied, Destination is not available; повтор и обход не выполнялись. Read-only status точного defect operation вернул not_found.
- Полный executor request/model/version/seed для обеих операций остаётся UNKNOWN. Из названия Vibe/imagegen нельзя выводить конкретную фактически вызванную модель.
- Для точного причинного заключения нужны сохранённые executor receipt двух известных операций и frozen visual_context brief. Уже доступные source asset, template и tune-поля исключают только версии о доказанном отсутствии фото, смене основного шаблона и явном переключении kind на стороне StreetStory.
- Новое платное изображение этим аудитом не создавалось. Контролируемая проверка изменения после исправления требует отдельного разрешённого исполнения.

## Приложение: полный неизменный шаблон image-v2

```text
Use the uploaded street photo as the main visual reference and the factual city-note themes below as editorial context.

Make an art-direction decision that fits this specific photograph. Do not apply a rigid preset. The result should feel like a finished Street Story editorial image: a real city photograph intelligently transformed with a graphic/sketch layer, not a generic filter and not a replacement scene.

Preserve the identity of the photographed place. Keep the important architecture, street geometry, perspective, building placement, entrances, rooflines, trees, signs and other major landmarks consistent with the source. You may simplify minor clutter, rebalance light, extend peripheral background, or slightly recompose for a stronger layout, but do not distort or crop away the key subject. If the output aspect ratio differs from the source, prefer extending/reconstructing the edges over cutting important architecture.

Create a sophisticated hybrid of realistic urban photography and expressive editorial drawing. You choose the amount and placement of sketch contours, loose lines, arrows, underlines, highlights, small illustrative details, graphic accents and handwritten notes. Some areas may remain almost photographic while others can be more interpretive. The graphic layer should feel designed for this place, not mechanically spread over everything.

Handwritten notes should be about the city: history, atmosphere, memory, local character, urban life, contrasts, details worth noticing, or the author's observation. Use only claims supported by the supplied themes. Do not invent dates, names, events or historical assertions. Prefer a few concise, legible phrases over many weak ones.

CRITICAL LAYOUT SAFETY:
- Keep every meaningful word, label, underline, callout box, arrowhead and essential graphic element fully inside a safe area about 8% in from every edge.
- Nothing important may touch, cross or be clipped by the top, bottom, left or right edge.
- Never let a text block continue beyond the frame.
- Leave breathing room around annotations. If space is tight, use fewer/shorter notes instead of shrinking or clipping them.
- Keep the defining architectural subject fully readable; do not crop towers, gates or facade edges just to make room for graphics.
- Before finalizing, inspect all four edges and move or remove anything at risk of clipping.

Choose one restrained accent color that suits the photo and story. Use it selectively to guide attention, not to tint the whole scene.

Aim for a premium editorial balance: recognizable real photograph underneath; expressive but controlled hand-drawn intervention; factual human city notes; clear visual hierarchy; enough quiet space to breathe. The mood may be poetic, documentary, energetic, reflective, rough or elegant depending on the photograph. Preserve photographic depth, light and material texture.

Avoid generic architecture, radical skyline changes, dense annotation noise, tiny unreadable text, clipped graphics, blueprint-style dimensions, invented facts, corporate logos, watermarks, UI chrome or branding.

All visible annotation text should follow the language of the city-note themes. If the themes are in Russian, write the visible notes in Russian.

City-note themes and author context to interpret, compress and distribute naturally:
{{CITY_NOTE_THEMES}}
```
