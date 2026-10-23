"""Preserve native JSON strings that PostgreSQL JSONB cannot represent directly."""

import json
import re
from itertools import chain

TEXT_KEY = "_topdown_apple_json_text"
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def sql_text(value):
    return "\x00" not in value and _SURROGATE.search(value) is None


def _strings_match(value, predicate):
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
        if isinstance(item, str) and not predicate(item):
            return False
        if isinstance(item, (dict, list, tuple)):
            identity = id(item)
            if identity not in active:
                active.add(identity)
                values = chain(item.keys(), item.values()) if isinstance(item, dict) else iter(item)
                stack.append((values, identity))
    return True


def unicode_scalars(value):
    return _strings_match(value, lambda text: _SURROGATE.search(text) is None)


def encode_document(document):
    payload = json.dumps(document, allow_nan=False)
    if _strings_match(document, sql_text):
        return payload
    return json.dumps({TEXT_KEY: payload}, allow_nan=False)


def decode_document(document):
    if isinstance(document, dict) and set(document) == {TEXT_KEY} and isinstance(document[TEXT_KEY], str):
        return json.loads(document[TEXT_KEY])
    return document
