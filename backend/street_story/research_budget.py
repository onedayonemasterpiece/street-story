"""Bound domain pages using the shared SDK's JSON admission units.

This is a page ceiling, not a substitute quota authority. The SDK still admits
the final envelope, including other function responses in a provider batch.
"""
import json

PAGE_UNITS = 5500


def response_units(name, result, call_id="x" * 160):
    envelope = {"toolResponse": {"functionResponses": [
        {"id": call_id, "name": name, "response": {"result": result}},
    ]}}
    # Equivalent to ai-resource-control 0.1.11's text/JSON estimator. Do not
    # import or copy its private implementation into this public consumer.
    return len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode())


def bounded_inventory(read, name, args, items_key):
    limit = min(int(args.get("limit") or 30), 50)
    while limit > 0:
        result = read({**args, "limit": limit})
        if response_units(name, result) <= PAGE_UNITS:
            return result
        limit -= 1
    from .service import ConflictError
    raise ConflictError("live_review_item_oversize", "One item exceeds the page ceiling. Use the bounded review packet passages; data is preserved.")
