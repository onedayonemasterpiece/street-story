#!/usr/bin/env python3
"""Remove only stale Street Story smoke containers left by a cancelled parent job."""
from __future__ import annotations
from datetime import datetime, timezone
import json
import subprocess

PREFIX="street-story-live-smoke-"
STALE_SECONDS=300

def run(argv):
    return subprocess.run(argv,check=False,text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=30)

def main():
    listed=run(["docker","ps","--filter",f"name={PREFIX}","--format","{{{{.ID}}}} {{{{.Names}}}}"])
    if listed.returncode:
        print(json.dumps({"status":"docker_unavailable","removed":0}))
        return 1
    now=datetime.now(timezone.utc)
    removed=[]
    kept=[]
    for line in listed.stdout.splitlines():
        parts=line.split(maxsplit=1)
        if len(parts)!=2 or not parts[1].startswith(PREFIX):
            continue
        cid,name=parts
        inspected=run(["docker","inspect","--format","{{{{.State.StartedAt}}}}",cid])
        if inspected.returncode:
            kept.append(name)
            continue
        try:
            started=datetime.fromisoformat(inspected.stdout.strip().replace("Z","+00:00"))
        except ValueError:
            kept.append(name)
            continue
        age=(now-started).total_seconds()
        if age>=STALE_SECONDS:
            result=run(["docker","rm","-f",cid])
            if result.returncode==0:
                removed.append({"name":name,"age_seconds":round(age)})
            else:
                kept.append(name)
        else:
            kept.append(name)
    print(json.dumps({"status":"ok","removed":removed,"kept":kept,"secrets_disclosed":False},sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
