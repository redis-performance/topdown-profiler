"""Preserve native JSON strings that PostgreSQL JSONB cannot represent directly."""

import json
import re
from itertools import chain

TEXT_KEY = "_topdown_apple_json_text"
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def sql_text(value):
    return "\x00" not in value and _SURROGATE.search(value) is None


def unicode_scalars(value):
    stack = [(iter((value,)), None)]
    active = set()
    while stack:
        try:
            item = next(stack[-1][0])
        except StopIteration:
            _, identity = stack.pop()
            if identity is not None:
                active.remove(identity)
            continue
        if isinstance(item, str) and _SURROGATE.search(item):
            return False
        if isinstance(item, (dict, list, tuple)):
            identity = id(item)
            if identity not in active:
                active.add(identity)
                values = chain(item.keys(), item.values()) if isinstance(item, dict) else iter(item)
                stack.append((values, identity))
    return True


def _jsonb_strings(value):
    if isinstance(value, str):
        return sql_text(value)
    if isinstance(value, dict):
        return all(_jsonb_strings(key) and _jsonb_strings(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(_jsonb_strings(item) for item in value)
    return True


def encode_document(document):
    payload = json.dumps(document, allow_nan=False)
    if _jsonb_strings(document):
        return payload
    return json.dumps({TEXT_KEY: payload}, allow_nan=False)


def decode_document(document):
    if isinstance(document, dict) and set(document) == {TEXT_KEY} and isinstance(document[TEXT_KEY], str):
        return json.loads(document[TEXT_KEY])
    return document
