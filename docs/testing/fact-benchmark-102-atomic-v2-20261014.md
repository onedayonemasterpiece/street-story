# Фото102: точный атомарный реестр SPO-v2

Дата14.10.2026. Запуск cold-6f5b7eb. Это замена прежнего ошибочного подсчёта укрупнёнными пакетами, не новый модельный прогон.

## Готовая строка

| Фото / запуск | Проверенные уникальные факты о доме | Квалифицированные, не включённые в строгий счёт | Независимый baseline после атомизации | Объединённый проверенный эталон v2 | Полные совпадения / частичные | Покрытие полных совпадений |
|---|---:|---:|---:|---:|---|---:|
|102 / cold-6f5b7eb|18|3|26|40|16 полных,2 частичных|16/40=40%|

Дополнительно исключены: один исторический адрес как identity metadata; одно утверждение об уничтожении района как окружающий scope. Эквивалентных дублей после атомизации нет. Не18/40: два проверенных утверждения старой выдачи имеют меньше временной/пространственной конкретики, чем соответствующий узелэталона, и помеченыpartial. Никаких условных диапазонов в этой строке нет.

Проверенный означает: утверждение прочитано в релевантном источнике и согласуется с имеющимися независимыми опорами; это не гарантия первичного архивного подтверждения.14 новых единиц основаны на вторичномPrussia39; provenance не скрыт. Квалифицированные3 — альтернативное уточнение≈1910 и два недатированных«сейчас». Непроверенных помимо этих3 и доказанно ложных объектных фактов в этом конкретномoutput не установлено.

## Правило фиксированной гранулярности

Один предикат о свойствах/событии одного субъекта. Дата/место/источник — квалификаторы события, не отдельные факты. Этажность, наличие мансарды, форма крыши и разные конструкции карнизов/эркера — различные проверяемые свойства. Сохранность крыши1945 и её последующая утрата — разные события; восстановление здания и форма восстановленнойкрыши — событие и свойство. Пятьэтажей не пятьфактов; каждая из двух разныхформ эркера не разбивается науглы/грани.

Замороженный baseline13 тематических пунктов сохранён, его26атомарных единиц имеют crosswalk. Добавленные14 имеют discovered_from_agent_then_reread и включены вunion40, поэтому знаменатель не игнорирует правильные находки агента. Смешивать этотv2 с прежними пакетными13/21/инымиN нельзя. Насыщенностьunion40 не объявляется: поздняяпроверка дала новый материал.

Источники baseline — [первоначальный независимый аудит](https://github.com/onedayonemasterpiece/street-story/commit/b4f728e0bd54a1300bfa87cc9effa1d1e565564f), раздел102; полныеURLs сохранены в основномbenchark. Новый полнотекстовый источник: [Prussia39,sid3875](https://www.prussia39.ru/sight/index.php?sid=3875), прочитан14.10.2026 прямымHTMLread сWindows-1251. Все дополнительные утверждения ниже происходят изэтой страницы, не издоверияmodeloutput.

## Объединённый реестр

|ID|Субъект|Предикат|Объект утверждения|Время / область|Статус|Происхождение / старыйID|
|---|---|---|---|---|---|---|
|102A01|дом Житомирская22–24|construction|начало XX века|строительство|verified|independent_baseline 102-01|
|102A02|дом Житомирская22–24|survived|боевые действия|1945|verified|independent_baseline 102-02|
|102A03|дом Житомирская22–24|historic_uniqueness|последний сохранившийся дом бывшего Штайндамма|по историческому очерку|verified|independent_baseline 102-02|
|102A04|дом Житомирская22–24|storeys|пять|описанный исторический объём|verified|independent_baseline 102-03|
|102A05|дом Житомирская22–24|building_form|узкий вытянутый объём|историческое описание|verified|independent_baseline 102-03|
|102A06|дом Житомирская22–24|window_form|вытянутые окна|историческое описание|verified|independent_baseline 102-03|
|102A07|дом Житомирская22–24|occupant|страховое общество Нордштерн|довоенное; первый этаж|verified|independent_baseline 102-04|
|102A08|дом Житомирская22–24|use|бакалея|советский период|verified|independent_baseline 102-05|
|102A09|дом Житомирская22–24|use|торговля по талонам/спецмагазин|поздний советский этап|verified|independent_baseline 102-05|
|102A10|дом Житомирская22–24|occupant|Унция|поздний этап, первый этаж|verified|independent_baseline 102-06|
|102A11|дом Житомирская22–24|occupant|кафе Штайндамм99|2017; первый этаж|verified|independent_baseline 102-07|
|102A12|дом Житомирская22–24|use|жилые квартиры|2017; верхние этажи|verified|independent_baseline 102-07|
|102A13|дом Житомирская22–24|interior_work|раскрыта старая кладка|обустройство кафе, описано2017|verified|independent_baseline 102-08|
|102A14|дом Житомирская22–24|brick_feature|клейма на раскрытых кирпичах|обустройство кафе, описано2017|verified|independent_baseline 102-08|
|102A15|дом Житомирская22–24|interior_work|раскрыты опоры|обустройство кафе, описано2017|verified|independent_baseline 102-08|
|102A16|дом Житомирская22–24|interior_work|снятие поздней обшивки увеличило видимую высоту потолка|обустройство кафе, описано2017|verified|independent_baseline 102-08|
|102A17|дом Житомирская22–24|repair_plan|включение фасадного ремонта в план|январь2021|verified|independent_baseline 102-09|
|102A18|дом Житомирская22–24|repair_start|начало фасадного ремонта|февраль2022|verified|independent_baseline 102-10|
|102A19|дом Житомирская22–24|repair_obstacle|при первой подрядной стадии обнаружены проблемы основной стены|2022|verified|independent_baseline 102-10|
|102A20|дом Житомирская22–24|inspection|дополнительное обследование перед возобновлением ремонта|до июня2025|verified|independent_baseline 102-11|
|102A21|дом Житомирская22–24|contractor_change|сменён подрядчик ремонта|до возобновления июня2025|verified|independent_baseline 102-11|
|102A22|дом Житомирская22–24|repair_restart|возобновлён ремонт|июнь2025|verified|independent_baseline 102-11|
|102A23|дом Житомирская22–24|scaffolding|леса сняты|к17.12.2025|verified|independent_baseline 102-12|
|102A24|дом Житомирская22–24|facade_colour|бежево-коричневая окраска|результат ремонта2025|verified|independent_baseline 102-12|
|102A25|дом Житомирская22–24|decoration_restoration|восстановлен орнамент|результат ремонта2025|verified|independent_baseline 102-12|
|102A26|дом Житомирская22–24|repair_plan|возвращение башенки со шпилем|май2022|verified|independent_baseline 102-13|
|102A27|дом Житомирская22–24|attic|мансарда|исторический объём|verified_secondary_source|discovered_from_agent_then_reread |
|102A28|дом Житомирская22–24|roof_form|двускатная|первоначальная крыша|verified_secondary_source|discovered_from_agent_then_reread |
|102A29|дом Житомирская22–24|roof_state|крыша сохранилась после штурма|1945|verified_secondary_source|discovered_from_agent_then_reread |
|102A30|дом Житомирская22–24|roof_loss|крыша утрачена|первые послевоенные годы|verified_secondary_source|discovered_from_agent_then_reread |
|102A31|дом Житомирская22–24|reconstruction|здание восстановлено|конец1950-х|verified_secondary_source|discovered_from_agent_then_reread |
|102A32|дом Житомирская22–24|roof_form|вальмовая|после восстановления конца1950-х|verified_secondary_source|discovered_from_agent_then_reread |
|102A33|дом Житомирская22–24|window_axes|три оси окон|восточный фасад|verified_secondary_source|discovered_from_agent_then_reread |
|102A34|дом Житомирская22–24|cornice|венчающий профилированный карниз на консолях|восточный фасад|verified_secondary_source|discovered_from_agent_then_reread |
|102A35|дом Житомирская22–24|cornice|профилированный карниз между4и5этажами|восточный фасад|verified_secondary_source|discovered_from_agent_then_reread |
|102A36|дом Житомирская22–24|bay_location|эркер по центральной оси со2по5этаж|восточный фасад|verified_secondary_source|discovered_from_agent_then_reread |
|102A37|дом Житомирская22–24|bay_plan|прямоугольный план|уровни2–4этажей|verified_secondary_source|discovered_from_agent_then_reread |
|102A38|дом Житомирская22–24|bay_pediment|полуциркульный щипец|эркер уровней2–4|verified_secondary_source|discovered_from_agent_then_reread |
|102A39|дом Житомирская22–24|bay_plan|пятигранный план|уровень5этажа|verified_secondary_source|discovered_from_agent_then_reread |
|102A40|дом Житомирская22–24|facade_repair|фасады отремонтированы|1980|verified_secondary_source|discovered_from_agent_then_reread |
|102Q01|дом Житомирская22–24|construction|около1910|конфликт уточнения с≈1905, раннийXXвек устойчив|qualified_not_scored|agent_claim_checked |
|102Q02|дом Житомирская22–24|occupant|кафе Штайндамм99|сейчас в недатированной статье; дата запуска не подтверждена|qualified_not_scored|agent_claim_checked |
|102Q03|дом Житомирская22–24|use|квартиры на верхних этажах|сейчас в недатированной статье; дата запуска не подтверждена|qualified_not_scored|agent_claim_checked |

## Полное соответствие выдачи

|Фактвыдачи|Локатор исходного текста|IDэталона|Статус|Совпадение|
|---|---|---|---|---|
|O01|1|102A04|verified|full|
|O02|1|102A27|verified|full|
|O03|1|102A28|verified|full|
|O04|3|102Q01|qualified|construction-date refinement|
|O05|4|102A07|verified|partial: floor qualifier omitted|
|O06|4|102A08|verified|partial: Soviet time omitted|
|O07|5|102A02|verified|full|
|O08|6|102A29|verified|full|
|O09|6|102A30|verified|full|
|O10|7|102A31|verified|full|
|O11|7|102A32|verified|full|
|O12|8|102Q02|qualified|undated now|
|O13|8|102Q03|qualified|undated now|
|O14|9|102A33|verified|full|
|O15|10|102A34|verified|full|
|O16|10|102A35|verified|full|
|O17|11|102A36|verified|full|
|O18|12|102A37|verified|full|
|O19|12|102A38|verified|full|
|O20|13|102A39|verified|full|
|O21|14|102A40|verified|full|

## Точные источники baseline и проверка эркера

-102-01/04/05/06: [исторический обзор Калининград.Ru](https://kgd.ru/news/livekenig/item/54662-kjonigsberg-v-detalyah-oberrolberg--ulica-kopernika).
-102-05/07/08: [интервью владельца кафе,08.08.2017](https://www.tvoybro.com/exclusive/598978559607000000_kakovo-zhiviotsia-v-starieishikh-zdaniiakh-ghoroda).
-102-02/03/04/09: [план ремонта,29.01.2021](https://kgd.ru/news/society/item/93470-nezauryadnyj-no-oblezlyj-v-kaliningrade-otremontiruyut-poslednij-dom-shtajndamma).
-102-10/11/12: [репортаж17.12.2025](https://kgd.ru/news/society/item/115854-v-kaliningrade-zavershayut-remont-fasada-poslednego-doma-shtajndamma).
-102-13: [проект26.05.2022](https://kgd.ru/news/society/item/100253-s-zeljonoj-bashenkoj-i-cherepicej-kak-otremontiruyut-poslednij-dom-shtajndamma-v-kaliningrade).

В полном Prussia39 субъект прямоугольного плана/полуциркульного щипца — именно эркер в уровнях2–4; пятигранный план отнесён отдельно к его уровню5. Это не свойства всего дома и не пятиугольный щипец.102A36–39 сохраняют это различие.102A31 — событие восстановления конца1950-х;102A32 — отдельно описанный тип его крыши. Если протокол решит учитывать только событие вместе с его конструктивным результатом, нужно одновременно выпустить новую версию обеих сторон счёта; вSPO-v2 свойство и событие разделены явно.
