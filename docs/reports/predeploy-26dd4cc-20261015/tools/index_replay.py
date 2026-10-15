"""RAM replay of the 6083 scheduler index on real stories/jobs rows (read-only source)."""
import json, re, sqlite3, sys, time
src_path, schema_src = sys.argv[1], sys.argv[2]
src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=5)
mem = sqlite3.connect(":memory:")
for name in ("stories", "jobs"):
    mem.execute(src.execute("select sql from sqlite_master where type='table' and name=?", (name,)).fetchone()[0])
    cols = [r[1] for r in src.execute(f"pragma table_info({name})")]
    rows = src.execute(f"select {','.join(cols)} from {name}").fetchall()
    mem.executemany(f"insert into {name}({','.join(cols)}) values({','.join('?'*len(cols))})", rows)
mem.execute("CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(state, available_at, lease_until, created_at)")
mem.commit()
text = open(schema_src).read()
idx = re.findall(r"CREATE INDEX IF NOT EXISTS (?:idx_jobs_story_kind|idx_stories_pending_identity_visual)[^;]*;", text)
query = ("SELECT id,research_json,photo_sha256,(SELECT 1) AS interactive_identity FROM stories "
 "WHERE state IN ('needs_review','identifying','photo_ready') "
 "AND json_extract(stories.research_json,'$.automatic_research_outcome') IS NULL "
 "AND coalesce(json_extract(stories.research_json,'$.visual_identity.status'),'') NOT IN ('match','owner_confirmed') "
 "AND json_array_length(stories.research_json,'$.visual_identity.candidates')>0 "
 "AND NOT EXISTS(SELECT 1 FROM jobs queued WHERE queued.semantic_key="
 "'identity-visual:'||stories.id||':'||coalesce(json_extract(stories.research_json,'$.identity_generation'),0)||':'||stories.photo_sha256) "
 "ORDER BY interactive_identity DESC,created_at,id")
old_query = ("SELECT id,research_json FROM stories WHERE state IN ('needs_review','identifying','photo_ready') "
 "AND json_extract(stories.research_json,'$.automatic_research_outcome') IS NULL")
def t(fn, n=1):
    s = time.perf_counter()
    for _ in range(n): r = fn()
    return (time.perf_counter() - s) * 1000 / n, r
out = {"sqlite": sqlite3.sqlite_version, "stories": mem.execute("select count(*) from stories").fetchone()[0],
       "research_json_chars": mem.execute("select sum(length(research_json)) from stories").fetchone()[0],
       "max_research_json_chars": mem.execute("select max(length(research_json)) from stories").fetchone()[0]}
old_ms, old_rows = t(lambda: mem.execute(old_query).fetchall(), 5)
out["9613_prefilter_rows"] = len(old_rows)
out["9613_prefilter_rows_json_chars"] = sum(len(r[1] or '') for r in old_rows)
out["9613_sql_ms"] = round(old_ms, 3)
out["9613_python_json_loads_ms"] = round(t(lambda: [json.loads(r[1] or '{}') for r in old_rows], 5)[0], 3)
out["plan_before_index"] = [r[3] for r in mem.execute("EXPLAIN QUERY PLAN " + query)]
out["6083_sql_ms_without_index"] = round(t(lambda: mem.execute(query).fetchall(), 5)[0], 3)
out["create_index_ms"] = round(t(lambda: [mem.execute(s) for s in idx])[0], 3)
out["plan_after_index"] = [r[3] for r in mem.execute("EXPLAIN QUERY PLAN " + query)]
ms, rows = t(lambda: mem.execute(query).fetchall(), 100)
out["6083_sql_ms_with_index"] = round(ms, 4); out["6083_eligible_rows"] = len(rows)
big = mem.execute("select id from stories order by length(research_json) desc limit 1").fetchone()[0]
upd = lambda: mem.execute("update stories set research_json=research_json||'' where id=?", (big,))
out["update_largest_row_ms_with_index"] = round(t(upd, 20)[0], 3)
mem.execute("drop index idx_stories_pending_identity_visual")
out["update_largest_row_ms_without_index"] = round(t(upd, 20)[0], 3)
print(json.dumps(out, ensure_ascii=False))
