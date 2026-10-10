"""Early, read-only Wikipedia metadata; only model-selected pages carry REFs."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


@asynccontextmanager
async def metadata_scope():
    """Own HTTP tasks throughout model/search work, then close them boundedly."""
    tasks = set()
    try:
        yield tasks
    finally:
        pending = {task for task in tasks if not task.done()}
        if pending:
            await asyncio.wait(pending, timeout=2)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def merge_metadata(*batches):
    """Deduplicate exact article/QID identities, retaining every explicit link."""
    result = []
    for batch in batches:
        for page in batch or []:
            if not isinstance(page, dict) or not page.get('pageid'):
                continue
            qid = (page.get('pageprops') or {}).get('wikibase_item') or page.get('wikidata_id')
            existing = next((item for item in result if item['pageid'] == page['pageid']
                or qid and ((item.get('pageprops') or {}).get('wikibase_item') or item.get('wikidata_id')) == qid), None)
            if existing is None:
                result.append(dict(page))
                continue
            links = [*(existing.get('mapped_wikipedia_sources') or []), *(page.get('mapped_wikipedia_sources') or [])]
            if links:
                existing['mapped_wikipedia_sources'] = list({item['candidate_id']: item for item in links}.values())
            for key in ('image_url', 'thumbnail_url', 'extract', 'lat', 'lon', 'coordinate_provenance'):
                if not existing.get(key) and page.get(key) is not None:
                    existing[key] = page[key]
    return result


async def ready_metadata(client, osm, nearby_task, *, timeout=2.0, owned_tasks=None):
    """Join a concurrent nearby lookup and exact links without delaying map work.

    Owned HTTP-only pending work continues during the existing planner/search,
    and is closed by metadata_scope. Standalone callers cancel and await it.
    """
    linked = getattr(client, 'linked', None)
    tasks = {'nearby': nearby_task}
    if callable(linked):
        tasks['mapped'] = asyncio.create_task(linked(osm))
    if owned_tasks is not None:
        owned_tasks.update(tasks.values())
    ready, waiting, failures = {}, [], []
    try:
        done, pending = await asyncio.wait(set(tasks.values()), timeout=timeout)
        for name, task in tasks.items():
            if task in done:
                try:
                    ready[name] = task.result()
                except Exception as exc:
                    failures.append(exc)
            else:
                waiting.append(name)
        return merge_metadata(ready.get('mapped'), ready.get('nearby')), waiting, failures
    finally:
        if owned_tasks is None:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)


def selected_candidates(candidates, observed, pages, payload):
    """Keep selected Wiki REF bytes reachable without nearest-page promotion."""
    if 'selected_wikipedia_page_ids' not in payload:
        return candidates  # Existing frozen plans retain their exact semantics.
    chosen = list(dict.fromkeys([*payload['selected_wikipedia_page_ids'], *(payload.get('late_mapped_wikipedia_page_ids') or [])]))[:3]
    allowed_refs = {url for page in pages if str(page['pageid']) in chosen
        for url in (page.get('image_url'), page.get('thumbnail_url')) if url}
    full = {item['candidate_id']: item for item in [*observed, *candidates]}
    result = []
    ids = set()
    for item in [*(full.get('wiki:' + page_id) for page_id in chosen), *candidates]:
        if not item or item['candidate_id'] in ids:
            continue
        cid = item['candidate_id']
        if cid.startswith('wiki:') and cid.removeprefix('wiki:') not in chosen:
            continue
        ids.add(cid)
        result.append({**item, 'reference_image_urls': [url for url in item.get('reference_image_urls') or []
            if url in allowed_refs or not any(url in (page.get('image_url'), page.get('thumbnail_url')) for page in pages)]})
    return result


def late_linked_page_ids(pages, payload):
    """Dereference exact late links to already model-selected physical IDs."""
    selected = set(payload.get('observed_candidate_ids') or [])
    for item in payload.get('first_wave_hypotheses') or []:
        selected.update(value for value in (item.get('subject_id'), item.get('group_key')) if value)
    selected.update(item['candidate_id'] for item in payload.get('spatial_hypotheses') or []
        if item.get('support_status') in {'plausible', 'spatially_supported'})
    return [str(page['pageid']) for page in pages if any(
        link.get('candidate_id') in selected for link in page.get('mapped_wikipedia_sources') or [])
        and (page.get('image_url') or page.get('thumbnail_url'))][:3]
