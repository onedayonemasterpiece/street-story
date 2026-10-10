"""Lossless addressing compatibility for frozen model contracts.

No semantic fields, booleans, verdicts or evidence are supplied here. Raw
answers remain in receipts; only unambiguous omitted addresses and optional
nulls are adapted before the ordinary schema and own-evidence validation.
"""
from copy import deepcopy

from jsonschema import Draft202012Validator


def normalize_result(value, schema):
    value = deepcopy(value)

    def visit(item, contract):
        if not isinstance(contract, dict):
            return
        if isinstance(item, dict):
            properties = contract.get('properties') or {}
            required = contract.get('required') or []
            for key in list(item):
                field = properties.get(key)
                if not isinstance(field, dict):
                    continue
                if item[key] is None and key not in required and not Draft202012Validator(field).is_valid(None):
                    del item[key]
                else:
                    visit(item[key], field)
        elif isinstance(item, list):
            for child in item:
                visit(child, contract.get('items') or {})

    visit(value, schema)
    if isinstance(value, dict) and isinstance(value.get('decisions'), list) and len(value['decisions']) == 1:
        decision = value['decisions'][0]
        numbers = (((schema.get('properties') or {}).get('decisions') or {}).get('items') or {}).get('properties', {}).get('fact', {}).get('enum')
        if isinstance(decision, dict) and 'fact' not in decision and isinstance(numbers, list) and len(numbers) == 1:
            decision['fact'] = numbers[0]
    return value
