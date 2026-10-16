"""Street Story owner-only PUBLIC zoo identity candidate provisioning.

This is an editorial operator action, not an authenticated API endpoint.
Never imports RKB private book evidence, never marks identity "verified",
never merges by name/distance, never modifies a photo or fact ledger.
It uses the currently deployed Street Story ensure_poi_identity writer.
"""
from __future__ import annotations
import argparse,hashlib,json,os,shlex,sqlite3,time,urllib.request
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument("--apply",action="store_true")
p.add_argument("--expected-release",required=True)
a=p.parse_args()

state=Path("/home/dev/.local/state/street-story")
def setting(path,key):
    for raw in path.read_text().splitlines():
        line=raw.strip()
        if line.startswith(key+"="):
            tokens=shlex.split(line)
            if len(tokens)!=1:raise RuntimeError("unparseable named setting")
            return tokens[0].split("=",1)[1]
    raise RuntimeError("required configured path missing")
data_dir=Path(setting(state/"service.env","DATA_DIR")).expanduser().resolve()
db_path=(data_dir/"street-story.sqlite3").resolve()
expected_release=a.expected_release
if len(expected_release)!=40 or any(c not in "0123456789abcdef" for c in expected_release):
    raise RuntimeError("invalid release id")
active_source=(Path("/home/dev/.local/share/street-story/releases")/expected_release/"source"/"backend")
if not (active_source/"street_story"/"poi_memory.py").is_file():
    raise RuntimeError("exact installed Street Story owner code unavailable")
with urllib.request.urlopen("http://127.0.0.1:8188/healthz",timeout=3) as response:
    status=json.loads(response.read(8192))
if response.status!=200 or status.get("source_sha")!=expected_release:
    raise RuntimeError("active Street Story release changed; do not edit POI")
rkb_env=Path("/home/dev/.local/state/regional-knowledge-base/service.env")
rkb_db=Path(setting(rkb_env,"RKB_STREET_STORY_POI_DB")).resolve()
if not db_path.is_file() or db_path!=rkb_db:
    raise RuntimeError("Street Story authority differs from the RKB read-only projection")
import sys
sys.path.insert(0,str(active_source))
from street_story.poi_memory import ensure_poi_identity

wikidata="Q1193386"
wiki_url="https://ru.wikipedia.org/wiki/Калининградский_зоопарк"
wikidata_url="https://www.wikidata.org/wiki/Q1193386"
official_source="https://kldzoo.ru/about/history/"
identity_key="wikidata:"+wikidata
candidate={
    "candidate_id":identity_key,
    "candidate_name":"Калининградский зоопарк",
    "candidate_url":wikidata_url,
    "candidates":[{
        "candidate_id":identity_key,
        "wikidata":wikidata,
        "wikipedia_url":wiki_url,
    }],
    # No photo or verified physical entity cluster is claimed.
    "identity_eligible":False,
}
historical_names=["Кёнигсбергский зоосад","Кёнигсбергский зоопарк","Königsberger Tiergarten"]
current_address="проспект Мира, 26"
latitude=54+43/60+13.598/3600
longitude=20+29/60+14.543/3600

def norm(x):
    return " ".join(str(x).split()).casefold()

stable="poi_ss_"+hashlib.sha256(identity_key.encode()).hexdigest()[:24]

def inspect(db):
    db.row_factory=sqlite3.Row
    existing=db.execute(
        "SELECT id,status,canonical_name,latitude,longitude FROM pois WHERE id=?",
        (stable,)
    ).fetchone()
    owners=[]
    for namespace,value in (
        ("wikidata",wikidata),
        ("street_story_candidate",identity_key),
        ("wikipedia_url",wiki_url),
    ):
        row=db.execute(
            "SELECT poi_id FROM poi_aliases WHERE namespace=? AND normalized_value=?",
            (namespace,norm(value))).fetchone()
        if row:owners.append(str(row["poi_id"]))
    if len(set(owners))>1:
        raise RuntimeError("strong aliases conflict: manual identity review required")
    if owners and any(x!=stable for x in owners):
        # Do not auto-merge a newly discovered external ID with a different
        # previously canonicalized POI. This operator only provisions absent Q.
        raise RuntimeError("Wikidata identity already owned by another canonical POI")
    for label in [*historical_names,current_address]:
        kind="name" if label in historical_names else "address"
        old=db.execute("SELECT poi_id FROM poi_aliases WHERE namespace=? AND normalized_value=?",
                       (kind,norm(label))).fetchone()
        if old and old["poi_id"]!=stable:
            raise RuntimeError("historical-name/address conflict: manual review required")
    return existing,owners

def checkpoint():
    with sqlite3.connect(f"file:{db_path}?mode=ro",uri=True,timeout=4) as db:
        row,owners=inspect(db)
        count=db.execute("SELECT COUNT(*) FROM pois").fetchone()[0]
        aliases=db.execute("SELECT COUNT(*) FROM poi_aliases").fetchone()[0]
        return row,owners,count,aliases
row,owners,pre_n,pre_alias=checkpoint()
if row is not None and row["status"] not in ("candidate","verified"):
    raise RuntimeError("existing identity has unresolved merge status")
if not a.apply:
    print(json.dumps({"state":"preflight_passed","canonical_poi_key":stable,
      "already_registered":bool(row),"candidate_not_verified":True,
      "name_aliases":len(historical_names),"source_count":3,
      "latitude":round(latitude,6),"longitude":round(longitude,6),
      "existing_pois":pre_n,"would_insert_poi":0 if row else 1,
      "rkb_same_readonly_projection":True,
      "no_private_corpus_export":True},ensure_ascii=False))
    raise SystemExit

# Keep a tiny exact snapshot of the affected alias and POI rows; a full
# 900MB SQLite backup is NOT warranted under current ~1.5GB disk headroom.
audit_dir=state/"geo-pilot-audit"
audit_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
os.chmod(audit_dir,0o700)
stamp=int(time.time())
audit_path=audit_dir/f"public-zoo-Q1193386-{stamp}.json"
now=time.time()
with sqlite3.connect(db_path,timeout=15) as db:
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=15000")
    db.execute("BEGIN IMMEDIATE")
    original,owner_alias=inspect(db)
    pre={"poi":dict(original) if original else None,
         "aliases":[dict(x) for x in db.execute(
             "SELECT * FROM poi_aliases WHERE poi_id=? ORDER BY namespace,normalized_value",
             (stable,))],
         "public_provenance":{
             "wikidata":wikidata_url,"wikipedia":wiki_url,"official_history":official_source,
             "representative_position":"wikidata_point_not_historical_survey",
             "historical_aliases":"editor_reviewed_candidates_not_time_geometries",
         }}
    with open(audit_path,"x",encoding="utf8") as out:
        json.dump(pre,out,ensure_ascii=False,indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(audit_path,0o600)
    poi_id=ensure_poi_identity(db,candidate,
                               latitude=latitude,longitude=longitude,now=now)
    if poi_id!=stable:
        db.rollback()
        raise RuntimeError("Street Story identity writer did not use expected stable key")
    for label in historical_names:
        db.execute("""INSERT OR IGNORE INTO poi_aliases
             (poi_id,namespace,value,normalized_value,created_at)
             VALUES(?,'name',?,?,?)""",
            (stable,label,norm(label),now))
    db.execute("""INSERT OR IGNORE INTO poi_aliases
             (poi_id,namespace,value,normalized_value,created_at)
             VALUES(?,'address',?,?,?)""",
             (stable,current_address,norm(current_address),now))
    validated=db.execute(
        "SELECT status,latitude,longitude FROM pois WHERE id=?",
        (stable,)).fetchone()
    if not validated or validated["status"] not in ("candidate","verified"):
        db.rollback()
        raise RuntimeError("canonical POI candidate not persisted")
    if sum(1 for x in db.execute(
            "SELECT namespace FROM poi_aliases WHERE poi_id=?",(stable,)))<6:
        db.rollback()
        raise RuntimeError("not all required public identity aliases were stored")
    db.commit()
with sqlite3.connect(f"file:{db_path}?mode=ro",uri=True,timeout=5) as db:
    db.row_factory=sqlite3.Row
    hit=db.execute("SELECT id,status FROM pois WHERE id=?",(stable,)).fetchone()
    alias=db.execute("SELECT COUNT(*) FROM poi_aliases WHERE poi_id=?",(stable,)).fetchone()[0]
    final_n=db.execute("SELECT COUNT(*) FROM pois").fetchone()[0]
    if not hit or final_n-pre_n not in (0,1):
        raise RuntimeError("postcommit POI readback diverged")
print(json.dumps({"state":"registered_candidate","canonical_poi_ref":"streetstory://poi/"+stable,
  "poi_status":hit["status"],"new_pois":final_n-pre_n,
  "alias_count":alias,"public_identity_sources":3,
  "coordinate_role":"current_representative_point_not_exact_1896_entrance",
  "rkb_private_evidence_transferred":False,"source_sha":expected_release,
  "audit_receipt_written":True},ensure_ascii=False))