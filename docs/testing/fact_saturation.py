#!/usr/bin/env python3
"""Living fact-saturation table for the Street Story test photos.

After every product run the operator records, per photo, how many verified facts the
story found, how many went into the post, how many rest on visitor reviews, and the
result. The table compares each run with the best count ever seen for that house, so a
run that finds noticeably fewer facts is flagged instead of being forgotten.

  python3 docs/testing/fact_saturation.py snapshot --db <street-story.sqlite3> --release <sha> \
      --story 102=story_... [--post 102=https://t.me/...] [--note 102="text"]
  python3 docs/testing/fact_saturation.py render

The database is opened read-only. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / 'fact-saturation.json'
PAGE = HERE / 'fact-saturation.md'
SHORTFALL = 0.8  # a run below 80 % of the best count for the house is flagged


def load():
    return json.loads(DATA.read_text(encoding='utf-8'))


def save(data):
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')


def best(photo):
    counts = [photo.get('baseline_facts') or 0, photo.get('db_max_eligible') or 0]
    counts += [run.get('eligible') or 0 for run in photo.get('runs', [])]
    return max(counts)


def story_numbers(db, story_id):
    eligible = db.execute("SELECT assertion_id FROM fact_assertions WHERE story_id=? AND eligibility='eligible'",
                          (story_id,)).fetchall()
    ids = [row[0] for row in eligible]
    selected = db.execute("SELECT COUNT(*) FROM fact_assertions WHERE story_id=? AND eligibility='eligible' "
                          "AND owner_selected=1", (story_id,)).fetchone()[0]
    reviews = 0
    for assertion_id in ids:
        urls = [row[0] for row in db.execute(
            "SELECT e.source_url FROM fact_evidence_spans e JOIN fact_observations o ON o.observation_id=e.observation_id "
            "WHERE o.story_id=? AND o.assertion_id=? AND o.status='accepted'", (story_id, assertion_id))]
        reviews += any(str(url).endswith('#reviews') for url in urls)
    row = db.execute('SELECT state,draft_text,processed_image_url,research_json FROM stories WHERE id=?',
                     (story_id,)).fetchone()
    candidate = (json.loads(row[3] or '{}').get('visual_identity') or {}).get('candidate_id')
    status = 'ready' if row[0] in {'ready_to_publish', 'scheduled', 'published'} and row[2] else (
        'no_image' if row[1] else 'no_post')
    return {'eligible': len(ids), 'selected': selected, 'from_visitor_reviews': reviews, 'status': status}, candidate


def db_max(db, candidate):
    top = 0
    for story_id, research in db.execute('SELECT id,research_json FROM stories'):
        if (json.loads(research or '{}').get('visual_identity') or {}).get('candidate_id') == candidate:
            top = max(top, db.execute("SELECT COUNT(*) FROM fact_assertions WHERE story_id=? AND eligibility='eligible'",
                                      (story_id,)).fetchone()[0])
    return top


def pairs(values):
    out = {}
    for value in values or []:
        key, _, rest = value.partition('=')
        out[int(key)] = rest
    return out


def snapshot(args):
    data = load()
    photos = {photo['telegram_id']: photo for photo in data['photos']}
    db = sqlite3.connect(f'file:{args.db}?mode=ro', uri=True)
    posts, notes = pairs(args.post), pairs(args.note)
    date = datetime.now(timezone.utc).date().isoformat()
    for telegram_id, story_id in pairs(args.story).items():
        photo = photos.get(telegram_id)
        if photo is None:
            raise SystemExit(f'photo {telegram_id} is not in fact-saturation.json; add it first')
        numbers, candidate = story_numbers(db, story_id)
        if candidate and candidate != photo.get('candidate_id'):
            numbers['note'] = f'house differs from table: {candidate}'
        photo['db_max_eligible'] = max(photo.get('db_max_eligible') or 0, db_max(db, photo['candidate_id']))
        run = {'date': date, 'release': args.release[:7], 'story_id': story_id, **numbers}
        if telegram_id in posts:
            run['post'], run['status'] = posts[telegram_id], 'published'
        if telegram_id in notes:
            run['note'] = (run.get('note', '') + '; ' if run.get('note') else '') + notes[telegram_id]
        photo.setdefault('runs', []).append(run)
    save(data)
    render(None)


def render(_args):
    data = load()
    lines = [
        '# Насыщение фактами по тестовым фото',
        '',
        'Живая таблица: после **каждого** продуктового прогона в неё дописывается снимок по каждому фото '
        '(`fact_saturation.py snapshot`), и страница пересобирается. Она отвечает на вопрос «нашли ли мы о доме '
        'столько же, сколько уже находили раньше». Прогон, где проверенных фактов меньше 80 % лучшего результата '
        'для этого дома, помечен **недобором** — его нужно разобрать, а не забыть.',
        '',
        'Источники базового уровня: ' + '; '.join(f"[{s['name']}]({s['url']})" if s.get('url') else s['name']
                                                 for s in data.get('baseline_sources', [])) + '.',
        'Связанные документы: [реестр тестовых фото](photo-registry.md), '
        '[процедура приёмки](street-story-product-acceptance-20261010.md).',
        '',
        '| Фото | Дом | База 10.10 | Лучший результат | Последний прогон | Фактов | В посте | Из отзывов | Итог |',
        '|---:|---|---:|---:|---|---:|---:|---:|---|',
    ]
    for photo in sorted(data['photos'], key=lambda p: p['telegram_id']):
        runs = photo.get('runs') or []
        last = runs[-1] if runs else {}
        top = best(photo)
        shortfall = bool(last) and top and (last.get('eligible') or 0) < SHORTFALL * top
        result = {'published': 'опубликован', 'ready': 'готов к публикации', 'no_image': 'без картинки',
                  'no_post': 'без поста'}.get(last.get('status'), '—')
        if last.get('post'):
            result = f"[{result}]({last['post']})"
        if shortfall:
            result += ' · **недобор**'
        if last.get('note'):
            result += f" · {last['note']}"
        lines.append(f"| {photo['telegram_id']} | {photo.get('house') or '—'} | "
                     f"{photo['baseline_facts'] if photo.get('baseline_facts') is not None else '—'} | {top or '—'} | "
                     f"{(last.get('date', '') + ' ' + last.get('release', '')).strip() or '—'} | "
                     f"{last.get('eligible', '—')} | {last.get('selected', '—')} | "
                     f"{last.get('from_visitor_reviews', '—')} | {result} |")
    lines += ['', 'Полная история прогонов по каждому фото — в [fact-saturation.json](fact-saturation.json).', '']
    PAGE.write_text('\n'.join(lines), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    snap = sub.add_parser('snapshot')
    snap.add_argument('--db', required=True)
    snap.add_argument('--release', required=True)
    snap.add_argument('--story', action='append', required=True, help='telegram_id=story_id')
    snap.add_argument('--post', action='append', help='telegram_id=post URL')
    snap.add_argument('--note', action='append', help='telegram_id=short note')
    snap.set_defaults(func=snapshot)
    sub.add_parser('render').set_defaults(func=render)
    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
