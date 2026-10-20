# Street Story: аудит физического теста Samsung S21 Ultra, 20.10.2026

Статус: **физический сценарий не принят; аудит клиентских логов завершён, серверная первопричина не установлена**. Это диагностическая передача для доработок, не отчёт об исправлении или deploy.

Пакет: `com.onedayonemasterpiece.streetstory`, версия `0.1.1584`, versionCode `1584`. Точный APK SHA и backend source в этом сборе не подтверждались. Projects Hub не тестировался. Сборщик PID 9700 отсутствует; оба исходных файла стабильны. Телефон для продолжения анализа не требуется.

Время logcat — UTC+02; хронология ниже приведена в UTC. Хвост второй записи после остановки пользовательского теста около 13:00 UTC не считается продолжением сценария.

[Исходный тред и материалы владельца](https://mira-projects.slack.com/archives/C0C7XQU60CX/p1792404963061539).


### Итог

Сценарий полностью не пройден: готовый текст и готовое изображение текущего прогона пользователю не показаны.

- Crash/ANR Street Story не обнаружены.
- За две пачки зафиксировано 129 запусков `SyncWorker` и 127 результатов `SUCCESS`; `RETRY` и `FAILURE` отсутствуют.
- `SUCCESS` worker не доказывает успешное получение, сохранение или показ текста/изображения.
- Во второй пачке Android 48 раз подавлял уведомления Street Story по пользовательской настройке.
- Зафиксировано 64 предупреждения `Job didn't exist in JobStore`.
- Сеть нельзя считать единственной причиной: WorkManager многократно видел validated Wi‑Fi, хотя встречались краткие начальные состояния `isConnected=false`.

### Источники

| Пачка | Диапазон UTC | Строк | Размер | PID |
|---|---:|---:|---:|---|
| 1 | 12:05:00.014–12:19:04.012 | 46 450 | 7 020 814 B | 22691, 21986 |
| 2 | 12:34:07.004–13:24:47.303 | 171 987 | 25 749 564 B | 21986, 28328 |

Исходные SHA256:

- Блок 1: `C77261616A99E927573EA03B877FCED876BD4FF052232F204C886E950B56FDA1`
- Блок 2: `A2C70CF579BD358AB2C9C6BE96F3815A080D859F669E93A77A8B71957DF74D3C`

### Хронология

- `12:05:25 UTC`: Street Story PID `22691` вышел на foreground.
- `12:07:29`: PID `22691` завершён с причиной `remove task`, не crash.
- `12:09:36`: приложение запущено заново, PID `21986`.
- `12:34–13:04`: повторяющиеся циклы `SyncWorker`, преимущественно с `SUCCESS`.
- `12:36`, UI: «Университетский колледж БФУ», выбрано 6/27 фактов.
- `12:47`: длительное отсутствие результата генерации текста через Миру.
- `12:49`: взаимодействие восстановлено, готового текста нет.
- `12:52`: предложение позже сравнить независимые модели, например MiMo 2.6 через OpenCode. Модель не запускалась.
- `12:53`, UI: повторные сообщения о подготовке текста, затем баннер «Связь с Мирой прервалась. Результат сохранён — включите Live снова». Сам текст не показан; надпись не доказывает сохранение.
- `12:56`: По сообщению владельца, Мира предложила публикацию без сгенерированной картинки. Фактическая публикация не подтверждена.
- `12:57`: запрос картинки долго не отвечал, затем появилась ошибка связи. Terminal-состояние image job неизвестно.
- `12:59`: отдельно отмечено длительное установление live-соединения.
- `13:00:48`: activity получила `appStopped=true`.
- `13:04:10`: PID `21986` завершён с `remove task`; PID `28328` поднят только для `SystemJobService`.
- `13:05:34`: PID `28328` завершён системой как пустой процесс (`empty #33`).

[Скриншоты интерфейса в Slack](https://mira-projects.slack.com/archives/C0C7XQU60CX/p1792502763511689). Скриншот 07:47 относится к другому объекту и исключён из выводов текущего прогона.

### Подтверждённые ошибки и сигналы

1. Фоновая синхронизация активно выполнялась:

| Метрика | Блок 1 | Блок 2 | Всего |
|---|---:|---:|---:|
| Started | 32 | 97 | 129 |
| SUCCESS | 31 | 96 | 127 |
| RETRY | 0 | 0 | 0 |
| FAILURE | 0 | 0 | 0 |

Разница между starts и SUCCESS требует сопоставления отдельных запусков. Граница записи или остановка процесса — возможные объяснения, но не установленная причина.

2. Предупреждения WorkManager/JobScheduler (самостоятельно не доказывают причину продуктового сбоя):

- `Job didn't exist in JobStore`: 11 + 53 события.
- Есть `Sending onNetworkChanged for a job that isn't started`.
- Есть `onNetworkChanged() not implemented`.

3. Уведомления подавлялись:

- 48 событий `Suppressing notification ... by user request`.
- Поэтому notification нельзя считать надёжным единственным каналом возврата результата.

4. Сеть меняла состояние:

- встречались `isConnected=false`;
- многократно подтверждалось `isConnected=true, isValidated=true`.

Следовательно, «полностью отсутствовал интернет» не подтверждается. Возможен разрыв live/WebSocket или прикладная ошибка при доступной базовой сети.

5. Crash/ANR:

- app-specific `FATAL EXCEPTION`: 0;
- crash: 0;
- ANR: 0.

### Наблюдения, не доказанные logcat

- Текст не появился после возврата в приложение.
- Заявление UI о сохранении результата не подтверждено фактическим результатом.
- Публикация без картинки только предлагалась.
- Ошибка связи не показывает, завершилась ли backend image job.
- Долгое подключение live — отдельный симптом.

### Гипотезы

1. `SyncWorker` возвращает `SUCCESS` после успешного polling/sync-запроса, но до terminal-результата пользовательской генерации.
2. После разрыва live состояние job не восстанавливается или не привязывается обратно к UI.
3. Подавленные уведомления скрывают сигнал готовности, а in-app fallback недостаточен.
4. Частый WorkManager polling и `JobStore` churn создают гонки или лишние цепочки.
5. Прикладной канал Миры нестабилен независимо от общей доступности Wi‑Fi.

Текущего логирования недостаточно, чтобы доказать конкретную гипотезу.

### Рекомендации

- Ввести обезличенный correlation ID для text/image/publish jobs.
- Логировать `requested → accepted → running → succeeded/failed/cancelled`, backend error class и retry count.
- Не трактовать технический `WorkerResult.success()` как готовность пользовательского результата: отдельно отмечать `poll succeeded, job still pending`. Менять корректный контракт WorkManager без проверки реализации не требуется.
- После reconnect выполнять read-after-reconnect по correlation ID.
- Показывать предсказуемый статус, elapsed time и возможность безопасно покинуть приложение.
- Генерировать только по явному запросу пользователя.
- В terminal-состоянии показывать фактически использованную модель — Flash или Lite.
- Явно отделить «предложено опубликовать» от «опубликовано».
- Добавить in-app history/inbox, не зависящий от Android notifications.
- Проверить unique work, `ExistingWorkPolicy`, backoff и причины `Job didn't exist in JobStore`.

## Очищенные ключевые логи

```text
10-20 14:35:07.785 21986 22002 D WM-ConstraintTracker:
NetworkStateTracker24: initial state =
NetworkState(isConnected=false, isValidated=false, isMetered=true, isNotRoaming=true)

10-20 14:35:07.855 21986 21986 D WM-WorkerWrapper:
Starting work for com.onedayonemasterpiece.streetstory.SyncWorker

10-20 14:35:08.116 21986 22031 I WM-WorkerWrapper:
Worker result SUCCESS for Work [
  id=<redacted>,
  tags={ com.onedayonemasterpiece.streetstory.SyncWorker }
]

W JobScheduler:
Job didn't exist in JobStore: <redacted>
... com.onedayonemasterpiece.streetstory/.../SystemJobService

W JobServiceContext:
Sending onNetworkChanged for a job that isn't started
... com.onedayonemasterpiece.streetstory/.../SystemJobService

W JobService:
onNetworkChanged() not implemented in
androidx.work.impl.background.systemjob.SystemJobService.

E NotificationService:
Suppressing notification from package
com.onedayonemasterpiece.streetstory by user request.

10-20 15:00:48.231 1511 3404 E WindowManager:
win=...com.onedayonemasterpiece.streetstory.MainActivity
destroySurfaces: appStopped=true ...

10-20 15:04:10.674 1511 1591 I ActivityManager:
Killing 21986:com.onedayonemasterpiece.streetstory/u0a393
(adj 250): remove task

10-20 15:04:10.870 1511 1600 I ActivityManager:
Start proc 28328:com.onedayonemasterpiece.streetstory/u0a393
for service {.../androidx.work.impl.background.systemjob.SystemJobService}

10-20 15:05:34.813 1511 5050 I ActivityManager:
Killing 28328:com.onedayonemasterpiece.streetstory/u0a393
(adj 995): empty #33
```


## Приоритет передачи разработчику

1. Сопоставить серверные text/image/live события 12:34–13:05 UTC с этим клиентским прогоном. Найти фактическое принятие запросов, task/job ID и terminal-результат. Не начинать со смены модели или переписывания очереди без этой проверки.
2. Проверить правдивость баннера «Результат сохранён», восстановление текущего задания после reconnect и условия предложения публикации без готового изображения.
3. Сначала использовать имеющиеся серверные логи и состояние заданий. Если корреляции действительно нет, добавить минимально необходимую наблюдаемость; рекомендации выше не являются требованием строить новую подсистему.

Идея сравнения моделей не подтверждает доступность MiMo 2.6. Генерация только по явному запросу: предложение автогенерации после концепции владелец отменил.

## Полнота материалов

Документ содержит подтверждённые агрегаты и ключевые очищенные строки. Полные очищенные клиентские логи подготовлены на компьютере, их передача выполняется отдельно. Общие сырые logcat с данными иных приложений не публикуются. Серверные логи не включены.
