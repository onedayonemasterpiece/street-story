#!/usr/bin/env python3
"""Sanitized read-only snapshot of the canonical ordinary Google AI quota authority."""
from __future__ import annotations

from collections import Counter, defaultdict
import importlib.util
import json
from pathlib import Path
import urllib.parse
import urllib.request
from typing import Any

MODELS = ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite")


def root() -> Path:
    return Path(__file__).resolve().parents[2]


def installer():
    path = root() / "backend" / "deploy" / "devcoveer_install.py"
    spec = importlib.util.spec_from_file_location("street_story_devcoveer_install", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_json(url: str, token: str, params: dict[str, str]) -> Any:
    query = urllib.parse.urlencode(params, safe="(),.*")
    request = urllib.request.Request(
        url.rstrip("/") + "/rest/v1/" + query.split("?", 1)[0]
        if False else url,
    )
    del request
    endpoint = url.rstrip("/") + "/rest/v1/" + params.pop("__table")
    query = urllib.parse.urlencode(params, safe="(),.*")
    request = urllib.request.Request(
        endpoint + ("?" + query if query else ""),
        headers={
            "apikey": token,
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "User-Agent": "StreetStory-Quota-Diagnostic/1",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def main() -> int:
    module = installer()
    values = module.parse_dotenv(module.PROVIDERS_ENV)
    url = (
        values.get("GOOGLE_AI_LIMITER_SUPABASE_URL", "").strip()
        or values.get("AI_RESOURCE_CONTROL_URL", "").strip()
    )
    token = (
        values.get("GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY", "").strip()
        or values.get("AI_RESOURCE_CONTROL_SERVICE_KEY", "").strip()
    )
    if not url.startswith("https://") or not token:
        print(json.dumps({"status":"authority_config_missing","secrets_disclosed":False}))
        return 1

    model_filter = "in.(" + ",".join(MODELS) + ")"
    limits = get_json(
        url,
        token,
        {
            "__table": "google_ai_model_limits",
            "select": "model,rpm,tpm,rpd,tpm_reserve_extra,updated_at",
            "model": model_filter,
            "order": "model.asc",
        },
    )
    keys = get_json(
        url,
        token,
        {
            "__table": "google_ai_api_keys",
            "select": "id,env_var_name,quota_scope,is_active,priority",
            "provider": "eq.google",
            "is_active": "eq.true",
            "order": "priority.asc",
            "limit": "100",
        },
    )
    counters = get_json(
        url,
        token,
        {
            "__table": "google_ai_usage_counters",
            "select": "api_key_id,model,minute_bucket,day_bucket,rpm_used,tpm_used,rpd_used,updated_at",
            "model": model_filter,
            "order": "updated_at.desc",
            "limit": "500",
        },
    )
    requests = get_json(
        url,
        token,
        {
            "__table": "google_ai_requests",
            "select": "model,quota_scope,status,last_error_kind,last_error_code,created_at,sent_at,finalized_at",
            "consumer": "eq.street-story",
            "model": model_filter,
            "order": "created_at.desc",
            "limit": "200",
        },
    )

    key_map = {
        str(row.get("id")): {
            "env": str(row.get("env_var_name") or ""),
            "scope": str(row.get("quota_scope") or ""),
            "priority": row.get("priority"),
        }
        for row in keys if isinstance(row, dict)
    }
    scope_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in counters if isinstance(counters, list) else []:
        if not isinstance(row, dict):
            continue
        meta = key_map.get(str(row.get("api_key_id")), {})
        scope = str(meta.get("scope") or "unknown")
        model = str(row.get("model") or "")
        key = (scope, model)
        item = scope_rows.setdefault(key, {
            "quota_scope": scope,
            "model": model,
            "aliases": set(),
            "minute_rpm": 0,
            "minute_tpm": 0,
            "day_rpd": 0,
            "latest_updated_at": None,
        })
        if meta.get("env"):
            item["aliases"].add(meta["env"])
        if row.get("minute_bucket") is not None:
            item["minute_rpm"] += int(row.get("rpm_used") or 0)
            item["minute_tpm"] += int(row.get("tpm_used") or 0)
        else:
            item["day_rpd"] += int(row.get("rpd_used") or 0)
        updated = row.get("updated_at")
        if updated and (item["latest_updated_at"] is None or str(updated) > str(item["latest_updated_at"])):
            item["latest_updated_at"] = updated

    request_summary: dict[str, Counter] = defaultdict(Counter)
    latest_request: dict[str, str] = {}
    for row in requests if isinstance(requests, list) else []:
        if not isinstance(row, dict):
            continue
        model = str(row.get("model") or "")
        status = str(row.get("status") or "unknown")
        error = str(row.get("last_error_kind") or row.get("last_error_code") or "none")
        request_summary[model][f"{status}:{error}"] += 1
        created = str(row.get("created_at") or "")
        if created and created > latest_request.get(model, ""):
            latest_request[model] = created

    rendered_scopes = []
    for item in scope_rows.values():
        item = dict(item)
        item["aliases"] = sorted(item["aliases"])
        rendered_scopes.append(item)
    rendered_scopes.sort(key=lambda x: (x["model"], x["quota_scope"]))

    print(json.dumps({
        "status": "ok",
        "limits": limits,
        "active_key_count": len(key_map),
        "distinct_quota_scopes": len({v["scope"] for v in key_map.values() if v["scope"]}),
        "scope_usage": rendered_scopes,
        "street_story_recent_requests": {
            model: {
                "counts": dict(counter),
                "latest_created_at": latest_request.get(model),
            }
            for model, counter in sorted(request_summary.items())
        },
        "secrets_disclosed": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
