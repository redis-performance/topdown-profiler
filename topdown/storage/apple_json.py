"""Preserve native JSON strings that PostgreSQL JSONB cannot represent directly."""

import json
import re

TEXT_KEY = "_topdown_apple_json_text"
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def sql_text(value):
    return "\x00" not in value and _SURROGATE.search(value) is None


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
