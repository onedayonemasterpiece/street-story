# Street Story: проверка продуктовых метрик и критериев
Дата: 2026-10-11. Только чтение GitHub, без запуска моделей/приёмки и без изменения репозитория.

## Короткий ответ
Да, coding-agent может и обязан сверяться с уже записанными требованиями и ПМИ. Но это ещё не полный численный продуктовый scorecard: конкретные ограничения, проверяемые исходы и методика существуют, а утверждённого процента автоматического успеха, p90/p95 скорости и денежного потолка на полезный результат в проверенных документах нет.

Число 80% в прежней версии новой постановки (8db5867) не следует превращать в PASS=12/15. Исходный документ явно говорит, что 80% — иллюстрация полезной неполноты, не recall. Историческая формулировка на 8db5867 «примерно 80% полезных правильных результатов» меняет смысл. Даже называя её ориентиром, она создаёт риск несанкционированного KPI; лучше убрать численный score до явного согласования владельцем.

## Проверенные источники и их приоритет
- [11 critical requirements, 30 acceptance clauses](https://github.com/onedayonemasterpiece/street-story/blob/af2cd7b2bdbdde30db9429edf033cb52f55b2dc9/.devcoveer/requirements.json). Файл побайтно одинаков на исходном 9e7e511 и конечном af2cd7b, blob 8e572ab6821d160a5d2e952f6d38fd3cd6473169.
- [Исходная уточняющая постановка](https://github.com/onedayonemasterpiece/street-story/blob/9e7e511d0901a799c14ac6ab82b17dd41efa2d1b/docs/prompts/street-story-universal-product-recovery-20261010.md), особенно строки 7, 540–547, 574–600.
- [Базовая постановка на 484a9bf, сохраняющая исходные метрики](https://github.com/onedayonemasterpiece/street-story/blob/484a9bf7d85b4e7a31da04a7118a8e1d2e1af8da/docs/prompts/street-story-recall-and-useful-result-20261010.md#L87-L91).
- [ПМИ конечной ночной версии](https://github.com/onedayonemasterpiece/street-story/blob/af2cd7b2bdbdde30db9429edf033cb52f55b2dc9/docs/testing/street-story-product-acceptance-20261010.md).
- [Photo search methods](https://github.com/onedayonemasterpiece/street-story/blob/af2cd7b2bdbdde30db9429edf033cb52f55b2dc9/docs/photo-search-methods.md#L260-L300).
- [Предыдущая версия новой постановки](https://github.com/onedayonemasterpiece/street-story/blob/8db5867e72132ee016f2e0a85a0e64681cdfe8fb/docs/prompts/street-story-next-agent-task-20261011.md#L3-L11).
Также прочитаны backend-runtime.md и исторический facts-research-finish-2026-10-03.md; исторические измерения не приняты за новые нормативные SLO.

## Что уже записано и проверяемо

| Область | Существующий контракт | Чего он НЕ устанавливает |
|---|---|---|
| Распознавание | CORE-PHOTO-IDENTITY: автоматическое установление объекта нормальным путём; uncertainty явно, optional correction. Проверять точное физическое тело, а не похожий фасад/комплекс | Нет фиксированного процента accuracy/recall. Дополнение critical clause «hint → правильный объект → собственные факты» в исходном prompt только предлагается владельцу, не записано в manifest |
| Полезный результат | Правильный объект → несколько самостоятельных собственных подтверждённых содержательных фактов → доступный выбор → concept/draft/Мира/visual/reopen. Исходный минимум 3 сохранён как метрика приёмки | Три строки в БД сами по себе не доказывают полезность; 3 не потолок исследования; 40–50 не квота/минимум |
| Время | С upload: identity 180 с, первый полезный eligible fact 300 с, completion 480 с. Примерно 30 с после готового SOURCE — engineering target без доказанной гарантии | Это hard caps/ограничители, не SLA/SLO качества скорости. Нет установленного p50/p90/p95, отдельного численного useful-set или commit→UI SLO |
| Первый/полезный/терминальный | Первый содержательный принятый факт показывается сразу; измерять отдельно identity, first fact, useful set, terminal. Address-only/статус/общие сведения о городе не useful fact | Terminal после useful может быть хвостом обогащения. Нельзя выдавать три факта за четыре минуты за хорошую скорость |
| Corpus | Frozen final15: 102,104,105,106,107,108,111,119,120,124,125,126,129,130,132; стабильные input hashes, runtime/config/harness SHA. Все попытаны/отражены | 15 не статистическое доказательство универсальности. Исторические 26 и final15 не один знаменатель; NOT_RUN не испытание |
| Auto/assisted | Отдельный auto end-to-end знаменатель со всеми техническими отказами; отдельно conditional quality закрытых model decisions и assisted после вопроса/ответа | Assisted не исправляет задним числом auto; scripted hint не настоящий ответ владельца |
| Truth | Независимая physical truth; evaluator IDs только у 7 из 15, у 8 исходно отсутствуют; сами 7 тоже требуют независимой проверки. Unknown → unresolved/REVIEW_REQUIRED | PASS_CANDIDATE не correctness; число facts и terminal не accuracy. Нельзя runtime использовать oracle |
| False accept / факты | Неверный корпус и неподдержанный факт не успех; negative controls own span/subject/temporal qualifiers/quote, независимое semantic чтение | Нет утверждённого допустимого процента false accept или ложных фактов. 5/26 reviewer rejects = 19% прежних решений, не измеренный FPR |
| Источники | Собственные passage/source/version/locator, subject scope, all-source UI/readback; поддержанные partial остаются доступны | Не требуется вычитать все источники до первого результата, повторный paid reviewer не обязательная production-ступень |
| Фон | Accepted upload продолжает работать при закрытии клиента; reopen возвращает результат, выбор/черновик. Проверить backend restart и отсутствие duplicates/loss; замерить commit→видимость | Нет обязательного нового WSS и нет установленного численного background/reopen latency SLO |
| Warm | Другая история того же точного POI переиспользует принятые canonical claims/proofs/source lineage, без обязательной новой extraction/review. Cold изолирован | Счётчик reused сам по себе не доказательство. Новая постановка явно требует контроль 104 8/8 proofs и весь свой accepted set у нового cold→warm |
| Цена/квоты | Существующие configured provider envelopes/quota/concurrency и действующие разрешения; actual sends/UNKNOWN/usage/cost или честная нижняя граница, резерв на final15/lifecycle; без тихого reset/расширения | «Разумная стоимость» не заданный рублёвый/долларовый потолок. Admission units ≠ billed tokens; sum parallel calls ≠ wall time |

### Точные ссылки для численных границ
- [180/300/480, 30 секунд — target](https://github.com/onedayonemasterpiece/street-story/blob/af2cd7b2bdbdde30db9429edf033cb52f55b2dc9/docs/photo-search-methods.md#L260-L263).
- [Исходные 180/300/480 и минимум 3, не повышать deadline](https://github.com/onedayonemasterpiece/street-story/blob/484a9bf7d85b4e7a31da04a7118a8e1d2e1af8da/docs/prompts/street-story-recall-and-useful-result-20261010.md#L87-L91).
- [Observer может сам прервать запуск: не путать с backend outcome](https://github.com/onedayonemasterpiece/street-story/blob/af2cd7b2bdbdde30db9429edf033cb52f55b2dc9/docs/testing/street-story-product-acceptance-20261010.md#L55-L67).
- [Auto/assisted, E2E знаменатель, latency, independent truth](https://github.com/onedayonemasterpiece/street-story/blob/9e7e511d0901a799c14ac6ab82b17dd41efa2d1b/docs/prompts/street-story-universal-product-recovery-20261010.md#L574-L585).
- [Нет утверждённых 90/95/26 из 26](https://github.com/onedayonemasterpiece/street-story/blob/9e7e511d0901a799c14ac6ab82b17dd41efa2d1b/docs/prompts/street-story-universal-product-recovery-20261010.md#L540-L547).
- [80% в исходном документе не recall](https://github.com/onedayonemasterpiece/street-story/blob/9e7e511d0901a799c14ac6ab82b17dd41efa2d1b/docs/prompts/street-story-universal-product-recovery-20261010.md#L5-L11).
- [Смена смысла 80% в новом тексте](https://github.com/onedayonemasterpiece/street-story/blob/8db5867e72132ee016f2e0a85a0e64681cdfe8fb/docs/prompts/street-story-next-agent-task-20261011.md#L5-L9).
- [Новая фактическая проверка facts/background/warm/final15 и бюджет](https://github.com/onedayonemasterpiece/street-story/blob/8db5867e72132ee016f2e0a85a0e64681cdfe8fb/docs/prompts/street-story-next-agent-task-20261011.md#L31-L47).

## Непротиворечивое применение coding-agent
1. Заморозить применимую evaluation-ревизию до платной серии; truth labels и rule changes не подгонять под score.
2. Отделять технические проверки, механический PASS_CANDIDATE и независимо подтверждённый продуктовый результат.
3. Выдавать numerator/denominator и перечень unknown/not-run рядом с любым процентом. Auto end-to-end denominator включает технические отказы; assisted и warm отдельно.
4. Проверять минимум 3 там, где задача его сохранила, но не скрывать реально пригодные 1–2 факта и не называть отсутствие третьего потерей POI. Новый prompt не отменяет минимум 3 молча.
5. Не считать runner timeout самостоятельным доказательством backend deadline/остановки.
6. Readback после закрытия клиента и сохранение owner selection/draft обязательны; идеальная полнота источников, RKB ingestion, журнал, все P2 и 40–50 карточек не общая церемония до полезного выпуска.
7. Большой final15 — после связных исправлений на стабильном кандидате; не обязательно запускать full baseline до правок и не повторять corpus после каждого patch.
8. CORE manifest не изменять без разрешения; proposed acceptance extension не представлять уже утверждённым.

## Выявленные неоднозначности
- **80%:** в оригинале предпочтение полезной неполноты, в предыдущей версии 8db5867 — success-rate ориентир. Исправить формулировку; никаких 12/15 без отдельного решения.
- **Hard cap против пользовательской скорости:** 180/300/480 ограничивают работу, а не задают желаемый p95. Слова «разумная стоимость» тоже без численного порога.
- **Метрики first/useful:** документы требуют разделять first/useful/terminal, однако не закрепляют полный единый формальный predicate полезного набора и отдельный UI SLO. Основной смысл ясен через доступный выбор/черновик и подтверждённость; scorecard требует точной формализации, не нового барьера.
- **Historical reviewer prose:** photo-search-methods строки 267–305 ещё описывают extract→review и qualification старого e40; строки 295–300 явно ограничивают этот текст as-is e40. Нельзя применять его как вечное требование второго model call после реализованного combined semantic path.
- **Truth coverage:** исходные labels 7/15 не равны независимо подтверждённой truth coverage конечной серии; 8 missing нельзя исключить из product denominator.
- **Итоговые docs vs результаты:** присутствие требований и тестов не доказывает их прохождения конечной версией; ПМИ сама говорит, что универсальная приёмка не пройдена.

## Только предложения, не ранее согласованные новые пороги
Для компактного будущего scorecard можно явно назвать: auto useful-result rate /15; отдельно assisted recovery /attempted assisted; independently verified physical outcomes /15; accepted facts checked, supported, wrong, unknown; wall-time identity/first useful/useful set/commit→UI отдельно для успешных и неуспешных; actual cost per attempted photo и per verified useful result при известном billing. Это формализация отчётности, не разрешение установить численные release gates. Целевой процент успеха, желаемые latency percentiles, допустимый risk и денежный ceiling требуют решения владельца.


Редакторское исправление этой поставки: действующий entrypoint убирает трактовку 80% как success-rate. Ссылки на 8db5867 выше сохранены как точное основание обнаруженного расхождения, а не описание уже исправленного текста.
