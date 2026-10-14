# Фото102: проверенный реестр и полный учёт доступных outputs, v4

Объект: дом Житомирская22–24. Архив неполон: v10 имеет4видимых утверждения, два из них только в смысловом извлечении; два известных nativeSol/Luna standalone-эксперимента без первичных текстов. Эти ограничения не превращены внули.

|Продукт нашёл, все сохранённые outputs|Отдельные модельные эксперименты|dot независимо проверил|Общий уникальный verified union|
|---:|---:|---:|---:|
|51|42|45|83|

## Продукт по сериям

|Серия|Дата / модель|Уникальные verified|Qualified|Охват|
|---|---|---:|---:|---|
|cold-6f5b7eb|10.10.2026, Gemini3.8Live|19|3|полныйcold|
|fresh-v10-visible|13.10.2026, GigaChat2/Google|6|0|4видимыхтекста,частичныйархив|
|d99263e|13.10.2026, gpt-6-luna|27|1|3исследования, восстановленныесохранённыеassessments;recoveryненовыйпоиск|
|9ea804e|13.10.2026, gpt-6-luna|24|0|3связанныхисследования, накопленныйфинал|
|85dff57|13.10.2026, gpt-6-luna|20|2|3исследования+отдельныеrepair;18verifiedвeligible,20вовсехсохранённыхoutputs|

Межпрогонные повторы не складываются. Частичные совпадения с более точным эталоном не являются полным покрытием его квалификаторов. Текущий Luna-результат не заменён историческим Gemini или свободными адресными экспериментами.

## Машинная и ручная проверка

- Продукт: 129 атомарных вхождений = 51 verified + 16 excluded + 4 qualified + 58 dedup.
- Эксперименты:144 вхождения =59 verified +18 qualified +9 unverified +6 false +31 excluded +21 dedup. Это вхождения по моделям; отдельный union42 вычислен из IDs.
- Проверки:35/35 структурных условийPASS, плюс14/14productchecks. Каждый доступный исходный claim имеет ручной пофразовый mapping; контроль файла/ID/дублей и арифметика воспроизводимы.
- СтруктурныйPASS не доказывает истинность источника и не восстанавливает усечённый архив.

## Исправления прошлых версий

- Возвращён пропущенный датированныйclaim59d309 о кафе/квартирах25.06.2025.
- Возвращено повреждение здания после1945 из coldclaim6; ранее было ошибочно пропущено.
- A21/A45 — одна смена подрядчика. X02/A16 — одно приспособление интерьера с участником и результатом.
- Наличие исторической фотографии не содержательный факт о доме. S04/S05 исключены из знаменателя.
- LongCatinterrupted содержал3утверждения вprogress:5этажей/кирпичverified,1909qualified. Это не завершённыйфинал.

## Независимое насыщение

Осмысленное веб-плато: последние три адресных/ремонтных контрольных прохода не дали новых принятых building-фактов. Остаются архивные исследования: поэтажный план100–101, реализация проекта1905, исходная газета1914, актприёмки2025. Немецкие записи N01–N05 относятся к адресному комплексу, конкретный фасадный корпус не доказан; вчислаонинепопали.

Новые официальные FP01–07 дают площади, документированные значения жителей, объявленную подвально-фундаментную стадию и два нормативных плана. Кирпичные стеныFP05 дублируютD01. Планфинансирования не контракт и не исполнение.

## Полный содержательный реестр

|ID|Субъект / предикат / значение|Время|Происхождение|Источник|
|---|---|---|---|---|
|102A01|дом Житомирская22–24 → construction → начало XX века|строительство|product, dot, experiments|https://kgd.ru/news/livekenig/item/54662-kjonigsberg-v-detalyah-oberrolberg--ulica-kopernika|
|102A02|дом Житомирская22–24 → survived → боевые действия|1945|product, dot, experiments|https://kgd.ru/news/society/item/93470-nezauryadnyj-no-oblezlyj-v-kaliningrade-otremontiruyut-poslednij-dom-shtajndamma|
|102A03|дом Житомирская22–24 → historic_uniqueness → последний сохранившийся дом бывшего Штайндамма|по историческому очерку|product, dot, experiments|https://kgd.ru/news/society/item/93470-nezauryadnyj-no-oblezlyj-v-kaliningrade-otremontiruyut-poslednij-dom-shtajndamma|
|102A04|дом Житомирская22–24 → storeys → пять|описанный исторический объём|product, dot, experiments|https://kgd.ru/news/society/item/93470-nezauryadnyj-no-oblezlyj-v-kaliningrade-otremontiruyut-poslednij-dom-shtajndamma|
|102A05|дом Житомирская22–24 → building_form → узкий вытянутый объём|историческое описание|product, dot, experiments|https://kgd.ru/news/society/item/93470-nezauryadnyj-no-oblezlyj-v-kaliningrade-otremontiruyut-poslednij-dom-shtajndamma|
|102A06|дом Житомирская22–24 → window_form → вытянутые окна|историческое описание|product, dot, experiments|https://kgd.ru/news/society/item/93470-nezauryadnyj-no-oblezlyj-v-kaliningrade-otremontiruyut-poslednij-dom-shtajndamma|
|102A07|дом Житомирская22–24 → occupant → страховое общество Нордштерн|довоенное; первый этаж|product, dot, experiments|https://kgd.ru/news/livekenig/item/54662-kjonigsberg-v-detalyah-oberrolberg--ulica-kopernika|
|102A08|дом Житомирская22–24 → use → бакалея|советский период|product, dot, experiments|https://www.tvoybro.com/exclusive/598978559607000000_kakovo-zhiviotsia-v-starieishikh-zdaniiakh-ghoroda|
|102A09|дом Житомирская22–24 → use → торговля по талонам/спецмагазин|поздний советский этап|dot, experiments|https://www.tvoybro.com/exclusive/598978559607000000_kakovo-zhiviotsia-v-starieishikh-zdaniiakh-ghoroda|
|102A10|дом Житомирская22–24 → occupant → Унция|поздний этап, первый этаж|dot, experiments|https://kgd.ru/news/livekenig/item/54662-kjonigsberg-v-detalyah-oberrolberg--ulica-kopernika|
|102A11|дом Житомирская22–24 → occupant → кафе Штайндамм99|2017; первый этаж|product, dot|https://www.tvoybro.com/exclusive/598978559607000000_kakovo-zhiviotsia-v-starieishikh-zdaniiakh-ghoroda|
|102A12|дом Житомирская22–24 → use → жилые квартиры|2017; верхние этажи|product, dot|https://www.tvoybro.com/exclusive/598978559607000000_kakovo-zhiviotsia-v-starieishikh-zdaniiakh-ghoroda|
|102A13|дом Житомирская22–24 → interior_wall_finish → открытаяисторическаякирпичнаякладка|обустройство кафе, описано2017|dot, experiments|https://www.tvoybro.com/exclusive/598978559607000000_kakovo-zhiviotsia-v-starieishikh-zdaniiakh-ghoroda|
|102A14|дом Житомирская22–24 → brick_feature → клейма на раскрытых кирпичах|обустройство кафе, описано2017|dot, experiments|https://www.tvoybro.com/exclusive/598978559607000000_kakovo-zhiviotsia-v-starieishikh-zdaniiakh-ghoroda|
|102A15|дом Житомирская22–24 → interior_visible_structure → видимыеопорысисторическойтехникойкрепления|обустройство кафе, описано2017|dot, experiments|https://www.tvoybro.com/exclusive/598978559607000000_kakovo-zhiviotsia-v-starieishikh-zdaniiakh-ghoroda|
|102A16|дом Житомирская22–24 → interior_work → снятие поздней обшивки увеличило видимую высоту потолка|обустройство кафе, описано2017|dot, experiments|https://www.tvoybro.com/exclusive/598978559607000000_kakovo-zhiviotsia-v-starieishikh-zdaniiakh-ghoroda|
|102A17|дом Житомирская22–24 → repair_plan → включение фасадного ремонта в план|январь2021|product, dot|https://kgd.ru/news/society/item/93470-nezauryadnyj-no-oblezlyj-v-kaliningrade-otremontiruyut-poslednij-dom-shtajndamma|
|102A18|дом Житомирская22–24 → repair_start → начало фасадного ремонта|февраль2022|product, dot, experiments|https://kgd.ru/news/society/item/115854-v-kaliningrade-zavershayut-remont-fasada-poslednego-doma-shtajndamma|
|102A19|дом Житомирская22–24 → repair_obstacle → при первой подрядной стадии обнаружены проблемы основной стены|2022|dot|https://kgd.ru/news/society/item/115854-v-kaliningrade-zavershayut-remont-fasada-poslednego-doma-shtajndamma|
|102A20|дом Житомирская22–24 → inspection → дополнительное обследование перед возобновлением ремонта|до июня2025|dot|https://kgd.ru/news/society/item/115854-v-kaliningrade-zavershayut-remont-fasada-poslednego-doma-shtajndamma|
|102A22|дом Житомирская22–24 → repair_restart → возобновлён ремонт|июнь2025|product, dot, experiments|https://kgd.ru/news/society/item/115854-v-kaliningrade-zavershayut-remont-fasada-poslednego-doma-shtajndamma|
|102A23|дом Житомирская22–24 → scaffolding → леса сняты|к17.12.2025|product, dot, experiments|https://kgd.ru/news/society/item/115854-v-kaliningrade-zavershayut-remont-fasada-poslednego-doma-shtajndamma|
|102A24|дом Житомирская22–24 → facade_colour → бежево-коричневая окраска|результат ремонта2025|product, dot, experiments|https://kgd.ru/news/society/item/115854-v-kaliningrade-zavershayut-remont-fasada-poslednego-doma-shtajndamma|
|102A25|дом Житомирская22–24 → decoration_restoration → восстановлен орнамент|результат ремонта2025|product, dot, experiments|https://kgd.ru/news/society/item/115854-v-kaliningrade-zavershayut-remont-fasada-poslednego-doma-shtajndamma|
|102A26|дом Житомирская22–24 → repair_plan → возвращение башенки со шпилем|май2022|product, dot, experiments|https://kgd.ru/news/society/item/100253-s-zeljonoj-bashenkoj-i-cherepicej-kak-otremontiruyut-poslednij-dom-shtajndamma-v-kaliningrade|
|102A27|дом Житомирская22–24 → attic → мансарда|исторический объём|product, experiments|https://www.prussia39.ru/sight/index.php?sid=3875|
|102A28|дом Житомирская22–24 → roof_form → двускатная|первоначальная крыша|product|https://www.prussia39.ru/sight/index.php?sid=3875|
|102A29|дом Житомирская22–24 → roof_state → крыша сохранилась после штурма|1945|product|https://www.prussia39.ru/sight/index.php?sid=3875|
|102A30|дом Житомирская22–24 → roof_loss → крыша утрачена|первые послевоенные годы|product|https://www.prussia39.ru/sight/index.php?sid=3875|
|102A31|дом Житомирская22–24 → reconstruction → здание восстановлено|конец1950-х|product, experiments|https://www.prussia39.ru/sight/index.php?sid=3875|
|102A32|дом Житомирская22–24 → roof_form → вальмовая|после восстановления конца1950-х|product|https://www.prussia39.ru/sight/index.php?sid=3875|
|102A33|дом Житомирская22–24 → window_axes → три оси окон|восточный фасад|product|https://www.prussia39.ru/sight/index.php?sid=3875|
|102A34|дом Житомирская22–24 → cornice → венчающий профилированный карниз на консолях|восточный фасад|product|https://www.prussia39.ru/sight/index.php?sid=3875|
|102A35|дом Житомирская22–24 → cornice → профилированный карниз между4и5этажами|восточный фасад|product|https://www.prussia39.ru/sight/index.php?sid=3875|
|102A36|дом Житомирская22–24 → bay_location → эркер по центральной оси со2по5этаж|восточный фасад|product|https://www.prussia39.ru/sight/index.php?sid=3875|
|102A37|дом Житомирская22–24 → bay_plan → прямоугольный план|уровни2–4этажей|product|https://www.prussia39.ru/sight/index.php?sid=3875|
|102A38|дом Житомирская22–24 → bay_pediment → полуциркульный щипец|эркер уровней2–4|product|https://www.prussia39.ru/sight/index.php?sid=3875|
|102A39|дом Житомирская22–24 → bay_plan → пятигранный план|уровень5этажа|product|https://www.prussia39.ru/sight/index.php?sid=3875|
|102A40|дом Житомирская22–24 → facade_repair → фасады отремонтированы|1980|product|https://www.prussia39.ru/sight/index.php?sid=3875|
|102A41|дом Житомирская22–24 → heritage_status → не числится в реестре памятников|29.01.2021|product, dot, experiments|https://kgd.ru/news/society/item/93470-nezauryadnyj-no-oblezlyj-v-kaliningrade-otremontiruyut-poslednij-dom-shtajndamma|
|102A42|дом Житомирская22–24 → repair_start_schedule → точный срок начала планируемых работ не определён|29.01.2021|product|https://kgd.ru/news/society/item/93470-nezauryadnyj-no-oblezlyj-v-kaliningrade-otremontiruyut-poslednij-dom-shtajndamma|
|102A43|дом Житомирская22–24 → scaffolding_installation → на лицевом фасаде установлены леса|24.06.2025|product|https://kgd.ru/news/society/item/113683-podryadchik-vozobnovil-remont-glavnogo-fasada-poslednego-doma-shtajndamma-v-kaliningrade|
|102A44|дом Житомирская22–24 → repair_contract → первый контракт: СК Стандарт,15.7млнруб,план сдачи до декабря2022|2022|product, dot, experiments|https://kgd.ru/news/society/item/113683-podryadchik-vozobnovil-remont-glavnogo-fasada-poslednego-doma-shtajndamma-v-kaliningrade|
|102A45|дом Житомирская22–24 → repair_contract → контракт Алант19.7млнруб на завершение ремонта|лето2024|product, dot, experiments|https://kgd.ru/news/society/item/113683-podryadchik-vozobnovil-remont-glavnogo-fasada-poslednego-doma-shtajndamma-v-kaliningrade|
|102A46|дом Житомирская22–24 → repair_restart_plan → возобновление планировали на середину октября2024|план2024|product, dot|https://kgd.ru/news/society/item/109703-remont-poslednego-doma-shtajndamma-v-kaliningrade-vozobnovyat-v-oktyabre|
|102A47|дом Житомирская22–24 → structural_requirement → требуется усиление несущих стен|при продолжении ремонта2024–2025|product, experiments|https://kgd.ru/news/society/item/113683-podryadchik-vozobnovil-remont-glavnogo-fasada-poslednego-doma-shtajndamma-v-kaliningrade|
|102A48|дом Житомирская22–24 → repair_delay → сроки возобновления переносились из-за усиления стен|2024–2025|product, experiments|https://kgd.ru/news/society/item/113683-podryadchik-vozobnovil-remont-glavnogo-fasada-poslednego-doma-shtajndamma-v-kaliningrade|
|102A49|дом Житомирская22–24 → repair_state → фасадный ремонт находится на завершающей стадии, акт окончания не заявлен|17.12.2025|product|https://kgd.ru/news/society/item/115854-v-kaliningrade-zavershayut-remont-fasada-poslednego-doma-shtajndamma|
|102A50|дом Житомирская22–24 → roof_plan → проектом предусмотрена скатная крыша|26.05.2022|product, experiments|https://kgd.ru/news/society/item/100253-s-zeljonoj-bashenkoj-i-cherepicej-kak-otremontiruyut-poslednij-dom-shtajndamma-v-kaliningrade|
|102A51|дом Житомирская22–24 → decoration_plan → проектом предусмотрен картуш под двумя окнами|26.05.2022|product, dot, experiments|https://kgd.ru/news/society/item/100253-s-zeljonoj-bashenkoj-i-cherepicej-kak-otremontiruyut-poslednij-dom-shtajndamma-v-kaliningrade|
|102A52|дом Житомирская22–24 → decoration_plan → проектом предусмотрено восстановление декоративного фасадного оформления|26.05.2022|product|https://kgd.ru/news/society/item/100253-s-zeljonoj-bashenkoj-i-cherepicej-kak-otremontiruyut-poslednij-dom-shtajndamma-v-kaliningrade|
|102D01|дом Житомирская22–24 → wall_material → кирпич|справочник2ГИС, дата состояния не указана|product, dot, experiments|https://2gis.ru/kaliningrad/geo/5630135189386496?m=20.511113%2C54.710611%2F15|
|102D02|дом Житомирская22–24 → restoration_concept_authorship → собственники ресторана Штайндамм99 разработали частную концепцию исторического восстановления|сообщение Генне18.03.2021|product|https://kgd.ru/news/society/item/94214-v-kaliningrade-hotyat-vosstanovit-poslednij-dom-shtajndamma-v-istoricheskom-vide|
|102D03|дом Житомирская22–24 → restoration_constraint → реализация эскизов ограничена предполагаемым сокращением площади квартир правой части|оценка18.03.2021|product|https://kgd.ru/news/society/item/94214-v-kaliningrade-hotyat-vosstanovit-poslednij-dom-shtajndamma-v-istoricheskom-vide|
|102D04|дом Житомирская22–24 → design_documentation_plan → подготовка документации фасада запланирована на2021|план18.03.2021|product|https://kgd.ru/news/society/item/94214-v-kaliningrade-hotyat-vosstanovit-poslednij-dom-shtajndamma-v-istoricheskom-vide|
|102D05|дом Житомирская22–24 → repair_completion_plan → завершение ремонта запланировано на сентябрь2025|план25.06.2025|product|https://klops.ru/kaliningrad/2025-06-25/359794-v-poslednem-dome-shtayndamma-na-zhitomirskoy-nachali-remontirovat-fasad-foto|
|102D06|дом Житомирская22–24 → project_revision_requirement → потребовалась корректировка проекта ремонта|2024–2025, сообщение17.12.2025|product|https://kgd.ru/news/society/item/115854-v-kaliningrade-zavershayut-remont-fasada-poslednego-doma-shtajndamma|
|102E01|интерьер кафе Штайндамм 99 в доме Житомирская 22–24 → ceiling_structure → потолочные балки|21.09.2021 description|experiments|https://kgd.ru/news/biz/item/96981-kakie-restorany-kaliningrada-stoit-posetit|
|102E02|интерьер кафе Штайндамм 99 в доме Житомирская 22–24 → floor_material → деревянные полы|21.09.2021 description|experiments|https://kgd.ru/news/biz/item/96981-kakie-restorany-kaliningrada-stoit-posetit|
|102FP01|дом Житомирская,22–24 → recorded_total_building_area → 943,20 кв.м общей площади МКД|документные редакции 05.10.2023 и 04.08.2025; не новый обмер|dot|https://fkr39.ru/upload/iblock/13b/ffabwpyollbdgymk4gksot6xa2bp4hdq/%D0%9F%D1%80%D0%B8%D0%BA%D0%B0%D0%B7%20%D0%9C%D0%B8%D0%BD%D1%81%D1%82%D1%80%D0%BE%D1%8F%20%D0%9A%D0%9F%202024-2026%20%D0%BE%D1%82%2004.08.2025%20%E2%84%96%2068%20%28%D0%B2%D1%81%D1%82%D1%83%D0%BF.%20%D0%B2%20%D1%81%D0%B8%D0%BB%D1%83%2007.08.2025%29.pdf; https://www.grazdanin-gazeta.ru/upload/uf/f74/j9dhc3dzi2zt5vl22b4356bxkvbwosz9.pdf|
|102FP02|помещения дома Житомирская,22–24 → recorded_premises_area → 877,18 кв.м площади помещений МКД|документные редакции 05.10.2023 и 04.08.2025|dot|https://fkr39.ru/upload/iblock/13b/ffabwpyollbdgymk4gksot6xa2bp4hdq/%D0%9F%D1%80%D0%B8%D0%BA%D0%B0%D0%B7%20%D0%9C%D0%B8%D0%BD%D1%81%D1%82%D1%80%D0%BE%D1%8F%20%D0%9A%D0%9F%202024-2026%20%D0%BE%D1%82%2004.08.2025%20%E2%84%96%2068%20%28%D0%B2%D1%81%D1%82%D1%83%D0%BF.%20%D0%B2%20%D1%81%D0%B8%D0%BB%D1%83%2007.08.2025%29.pdf; https://www.grazdanin-gazeta.ru/upload/uf/f74/j9dhc3dzi2zt5vl22b4356bxkvbwosz9.pdf|
|102FP03|дом Житомирская,22–24 → recorded_resident_count → 35 жителей в региональной редакции 2025;38 зарегистрированных в муниципальной редакции 2023|35: редакция 04.08.2025;38: редакция 05.10.2023, публикация 12.10.2023, таблица 2022|dot|https://fkr39.ru/upload/iblock/13b/ffabwpyollbdgymk4gksot6xa2bp4hdq/%D0%9F%D1%80%D0%B8%D0%BA%D0%B0%D0%B7%20%D0%9C%D0%B8%D0%BD%D1%81%D1%82%D1%80%D0%BE%D1%8F%20%D0%9A%D0%9F%202024-2026%20%D0%BE%D1%82%2004.08.2025%20%E2%84%96%2068%20%28%D0%B2%D1%81%D1%82%D1%83%D0%BF.%20%D0%B2%20%D1%81%D0%B8%D0%BB%D1%83%2007.08.2025%29.pdf; https://www.grazdanin-gazeta.ru/upload/uf/f74/j9dhc3dzi2zt5vl22b4356bxkvbwosz9.pdf|
|102FP04|работы ФКР по подвалу и фундаменту дома Житомирская,22–24 → announced_work_commencement → ФКР объявил, что приступает к работам по подвалу и фундаменту|2025-04-22|dot|https://t.me/minstroy39/2741; https://t.me/fkr_39/9418|
|102FP06|план проектирования ремонта крыши и фундамента дома Житомирская,22–24 → documented_design_budget_plan → Проектирование крыши и фундамента предусмотрено в таблице плана 2024 года|Таблица 2024 года в редакции приказа от 04.08.2025 № 68|dot|https://fkr39.ru/upload/iblock/13b/ffabwpyollbdgymk4gksot6xa2bp4hdq/%D0%9F%D1%80%D0%B8%D0%BA%D0%B0%D0%B7%20%D0%9C%D0%B8%D0%BD%D1%81%D1%82%D1%80%D0%BE%D1%8F%20%D0%9A%D0%9F%202024-2026%20%D0%BE%D1%82%2004.08.2025%20%E2%84%96%2068%20%28%D0%B2%D1%81%D1%82%D1%83%D0%BF.%20%D0%B2%20%D1%81%D0%B8%D0%BB%D1%83%2007.08.2025%29.pdf|
|102FP07|региональный план капремонта дома Житомирская,22–24 на 2025 → documented_annual_repair_budget_plan → подвал, фасад, фундамент и строительный контроль,14932278,79руб|план 2025 в редакции 04.08.2025№ 68|dot|https://fkr39.ru/upload/iblock/13b/ffabwpyollbdgymk4gksot6xa2bp4hdq/%D0%9F%D1%80%D0%B8%D0%BA%D0%B0%D0%B7%20%D0%9C%D0%B8%D0%BD%D1%81%D1%82%D1%80%D0%BE%D1%8F%20%D0%9A%D0%9F%202024-2026%20%D0%BE%D1%82%2004.08.2025%20%E2%84%96%2068%20%28%D0%B2%D1%81%D1%82%D1%83%D0%BF.%20%D0%B2%20%D1%81%D0%B8%D0%BB%D1%83%2007.08.2025%29.pdf|
|102P01|дом Житомирская22–24 → war_damage → сохранился с повреждениями|после штурма1945|product|https://www.prussia39.ru/sight/index.php?sid=3875|
|102S01|Дом Житомирская,22–24 → построен на месте → предшествовавшего здания|началоXX века|experiments|https://kgd.ru/news/livekenig/item/54662-kjonigsberg-v-detalyah-oberrolberg--ulica-kopernika|
|102S08|Фасад дома → планировалось окрасить в → бежевый цвет|проект, опубликованный07.03.2023|experiments|https://srv.kgd.ru/news/society/item/103923-remont-poslednego-doma-shtajndamma-planiruyut-vozobnovit-v-aprele|
|102S09|Верхний этаж дома → планировалось выделить → более тёмным оттенком относительно остального фасада|проект07.03.2023|experiments|https://srv.kgd.ru/news/society/item/103923-remont-poslednego-doma-shtajndamma-planiruyut-vozobnovit-v-aprele|
|102S10|Проектируемая башенка дома → должна получить цвет → зелёный|проект07.03.2023|experiments|https://srv.kgd.ru/news/society/item/103923-remont-poslednego-doma-shtajndamma-planiruyut-vozobnovit-v-aprele|
|102S11|Ремонт главного фасада дома → repair_suspension → Ремонт приостановлен из-за необходимости восстановления исторической башенки|ноябрь2022|experiments|https://kgd.ru/news/society/item/102436-tyazhjolaya-bashenka-v-kaliningrade-priostanovili-remont-poslednego-doma-shtajndamma; https://www.newkaliningrad.ru/news/briefs/community/24019525-turkin-obyasnil-iz-za-chego-ostanovilsya-remont-poslednego-doma-shtayndamma.html; https://klops.ru/kaliningrad/2025-06-25/359794-v-poslednem-dome-shtayndamma-na-zhitomirskoy-nachali-remontirovat-fasad-foto|
|102S13|Подвал дома → требовал → усиления для принятия нагрузки от башенки|ноябрь2022|experiments|https://kgd.ru/news/society/item/102436-tyazhjolaya-bashenka-v-kaliningrade-priostanovili-remont-poslednego-doma-shtajndamma; https://www.newkaliningrad.ru/news/briefs/community/24019525-turkin-obyasnil-iz-za-chego-ostanovilsya-remont-poslednego-doma-shtayndamma.html|
|102S14|Восстановление башенки дома → получило дополнительное финансирование из → областного бюджета отдельной статьёй|к07.11.2022|experiments|https://kgd.ru/news/society/item/102436-tyazhjolaya-bashenka-v-kaliningrade-priostanovili-remont-poslednego-doma-shtajndamma; https://www.newkaliningrad.ru/news/briefs/community/24019525-turkin-obyasnil-iz-za-chego-ostanovilsya-remont-poslednego-doma-shtayndamma.html|
|102S15|Мартовский конкурс2024 на проектирование и ремонт дома → procurement_result → Не состоялся:0заявок; НМЦ19,7млн; предмет включает ПСД крыши/фундамента и ремонт крыши/фундамента/фасада/подвала|март2024|experiments|https://www.newkaliningrad.ru/news/briefs/community/24084358-novyy-konkurs-na-remont-poslednego-doma-shtayndamma-provalilsya.html|
|102S23|Несущая способность грунтов под домом → вызывала вопросы при → обследовании для продолжения ремонта|до10.09.2023|experiments|https://www.newkaliningrad.ru/news/briefs/community/24060897-v-fonde-kapremonta-obyasnili-pochemu-remont-poslednego-doma-shtayndamma-perenesli-na-2024-god.html; https://www.newkaliningrad.ru/news/briefs/community/24084358-novyy-konkurs-na-remont-poslednego-doma-shtayndamma-provalilsya.html|
|102X01|первый этаж дома Житомирская 22–24, открытие кафе «Штайндамм 99» → opening → Открытие кафе «Штайндамм 99» на первом этаже|конец августа 2016|dot|https://www.newkaliningrad.ru/afisha/restaurants/news/10545570-v-kaliningrade-otkrylos-kafe-shtayndamm-99.html|
|102X03|двор дома Житомирская 22–24 → courtyard_form → Двор-полуколодец|описание 14.03.2022|dot|https://strana39.ru/news/2022-03-14/346824-gde-hranitsya-oskolok-shtayndamma|
|102X04|советская бакалея на первом этаже дома Житомирская 22–24 → popular_name → Народное название советской бакалеи: Марьин утёс|советский период|dot|https://kgd.ru/news/livekenig/item/54662-kjonigsberg-v-detalyah-oberrolberg--ulica-kopernika|
|102X08|три дворовые стороны дома Житомирская 22–24, этап ремонта до 13.10.2023 → partial_repair_completed → Три стороны здания / стены со стороны двора отремонтированы|к 13.10.2023|product, dot|https://klops.ru/kaliningrad/2023-10-13/281296-v-kaliningrade-kapremont-istoricheskogo-doma-prervalsya-iz-za-togo-chto-prezhnie-zhiltsy-snesli-nesuschie-steny|
|102X09|несущие стены внутри дома Житомирская 22–24, предшествующий демонтаж → prior_demolition → По заявлению главы ФКР, прежние собственники помещения разобрали несущие стены|обнаружено до 13.10.2023|dot, experiments|https://klops.ru/kaliningrad/2023-10-13/281296-v-kaliningrade-kapremont-istoricheskogo-doma-prervalsya-iz-za-togo-chto-prezhnie-zhiltsy-snesli-nesuschie-steny|
|102X10|основной фасад дома Житомирская 22–24, консервация к 13.10.2023 → conservation → Основной фасад законсервирован временным защитным составом после удаления штукатурки|к 13.10.2023|dot|https://klops.ru/kaliningrad/2023-10-13/281296-v-kaliningrade-kapremont-istoricheskogo-doma-prervalsya-iz-za-togo-chto-prezhnie-zhiltsy-snesli-nesuschie-steny|
|102X11|объём проекта ремонта дома Житомирская 22–24 после изысканий к 13.10.2023 → scope_expansion_needed → В проект потребовалось добавить ремонт подвалов и фундамента|по результатам изысканий, сообщено 13.10.2023|dot|https://klops.ru/kaliningrad/2023-10-13/281296-v-kaliningrade-kapremont-istoricheskogo-doma-prervalsya-iz-za-togo-chto-prezhnie-zhiltsy-snesli-nesuschie-steny|
|102X13|пересмотренный план возобновления ремонта дома Житомирская 22–24, опубликован 25.11.2024 → revised_planned_restart → Новый план возобновления работ в марте 2025 после дополнительного обследования и с учётом погоды|план 25.11.2024|dot|https://kgd.ru/news/society/item/111316-ryhlyj-kirpich-zavershenie-remonta-poslednego-doma-shtajndamma-v-kaliningrade-perenesli-na-vesnu|

[Полный JSON: raw outputs, статусы, aliases, множества и validation](fact-benchmark-102-v4-20261014.json).
