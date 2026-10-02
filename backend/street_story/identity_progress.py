"""Small durable UI projection of identity events, never reconstructed from audio."""
from __future__ import annotations


def current_projection(previous: dict, identity: dict | None = None) -> dict:
    """Refresh obsolete UI copy without a write, new lookup or lost stage timings."""
    result = dict(previous or {})
    labels = {
        'Найден вероятный вариант · подтвердите объект': 'Найден вероятный вариант · пока недостаточно доказательств',
        'Объект пока не подтверждён · уточните место': 'Объект пока не определён · доказательств недостаточно',
    }
    result['steps'] = [dict(step, label=labels.get(step.get('label'), step.get('label', '')))
                       for step in result.get('steps', []) if isinstance(step, dict)]
    if identity and identity.get('status') == 'match' and identity.get('visual_reference_verified') is True:
        for step in result['steps']:
            if step.get('key') == 'references':
                step.update(label='Эталон выбранного объекта проверен', status='done')
    return result


def advance(previous: dict, event: str, fields: dict, now: float) -> dict:
    progress = current_projection(previous)
    generation = fields.get('generation', progress.get('generation', 0))
    if generation != progress.get('generation', generation):
        progress = {}
    progress.setdefault('generation', generation)
    progress.setdefault('started_at', now)
    steps = {x['key']: dict(x) for x in progress.get('steps', []) if isinstance(x, dict) and 'key' in x}
    def step(key, label, status):
        steps[key] = {'key': key, 'label': label, 'status': status}
    if event == 'identity_requested':
        step('gps', 'Проверяю геометки снимка', 'working')
    elif event == 'identity_started':
        progress['attempt'] = int(progress.get('attempt', 0)) + 1
        progress['finished'] = False
    elif event == 'identity_location':
        ok = bool(fields.get('coordinates_usable'))
        step('gps', 'Геометки найдены' if ok else 'В выбранной копии нет доступных геометок', 'done' if ok else 'warning')
        if ok:
            step('map', 'Ищу объекты на карте', 'working')
    elif event == 'identity_osm' and fields.get('available') is not False:
        counts = fields.get('candidate_pool_counts') or {}
        progress['map_count'] = int(fields.get('retained_count', 0))
        step('map', f"Карта: {progress['map_count']} кандидатов после отбора", 'done')
        progress['map_pool_counts'] = {key:int(counts.get(key, 0)) for key in ('landmark','nearby')}
        step('wiki', 'Ищу статьи и эталонные фотографии', 'working')
    elif event == 'identity_osm_unavailable':
        step('map', 'Карта временно недоступна; проверяю Википедию', 'warning')
    elif event == 'identity_wikipedia':
        progress['wiki_count'] = int(fields.get('count', 0))
        step('wiki', f"Википедия: {progress['wiki_count']} статей рядом", 'done')
    elif event == 'identity_wikipedia_unavailable':
        step('wiki', 'Википедия временно недоступна', 'warning')
    elif event == 'identity_shortlist':
        progress['candidate_count'] = int(fields.get('candidate_count', 0))
        progress['reviewed_count'] = 0
        step('compare', f"Для сравнения отобрано {progress['candidate_count']} кандидатов", 'working')
    elif event == 'identity_batch_started':
        progress['current_batch'] = int(fields.get('batch', 0))
        progress['current_batch_size'] = len(fields.get('candidate_ids') or [])
        step('compare', f"Сравниваю группу {progress['current_batch']} · проверено {progress.get('reviewed_count', 0)} из {progress.get('candidate_count', 0)}", 'working')
    elif event == 'identity_batch_finished':
        progress['reviewed_count'] = min(progress.get('candidate_count', 16), int(progress.get('reviewed_count', 0)) + int(fields.get('batch_candidate_count', progress.get('current_batch_size', 0))))
        step('compare', f"Проверено {progress['reviewed_count']} из {progress.get('candidate_count', 0)} кандидатов", 'working')
    elif event == 'identity_reference_loaded':
        step('references', 'Эталонное фото загружено для сравнения', 'done')
    elif event == 'identity_reference_unavailable':
        step('references', 'Не все эталоны доступны; проверяю оставшиеся', 'warning')
    elif event == 'identity_failed':
        step('retry', f"Источник не ответил · попытка {progress.get('attempt', 1)}", 'warning')
    elif event == 'identity_owner_confirmed':
        step('result', 'Объект явно подтверждён автором', 'done')
        progress['finished'] = True
    elif event == 'identity_finished':
        matched = fields.get('status') == 'match' and fields.get('reference_verified') is True
        step('result', 'Объект подтверждён визуальным сравнением' if matched else ('Найден вероятный вариант · пока недостаточно доказательств' if fields.get('candidate_id') else 'Объект пока не определён · доказательств недостаточно'), 'done' if matched else 'warning')
        if matched:
            step('references', 'Эталон выбранного объекта проверен', 'done')
        if 'compare' in steps:
            steps['compare']['status'] = 'done' if matched else 'warning'
        progress['finished'] = True
        progress['elapsed_ms'] = max(0, round((now - progress['started_at']) * 1000))
    else:
        return progress
    progress['steps'] = list(steps.values())[-9:]
    progress['updated_at'] = now
    return progress


def from_history(db, story_id: str, generation: int) -> dict:
    """Read-only projection for an existing topic; no new identification request."""
    import json
    import sqlite3
    try:
        rows = db.execute("SELECT event_type,payload_json,created_at FROM live_diagnostics WHERE story_id=? AND source='identity' ORDER BY id DESC LIMIT 201", (story_id,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    if len(rows) > 200:
        return {}
    progress = {}
    for row in reversed(rows):
        try:
            fields = json.loads(row['payload_json'])
        except (ValueError, TypeError):
            continue
        if fields.get('generation', generation) != generation:
            continue
        progress = advance(progress, row['event_type'], fields, row['created_at'])
    return progress
