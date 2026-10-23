"""Read native JSON artifacts without waiting on devices or named pipes."""

import errno
import json
import math
import os
from pathlib import Path
import stat

from topdown.apple_json import surrogate_tokens, unicode_scalars


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate Apple JSON field: {key!r}")
        result[key] = value
    return result


def _json_constant(value):
    raise ValueError(f"Non-finite Apple JSON number: {value}")


def _json_float(value):
    number = float(value)
    if not math.isfinite(number):
        _json_constant(value)
    return number


class NativeJSONDecoder(json.JSONDecoder):
    def __init__(self, **kwargs):
        parse_int = kwargs.get("parse_int")
        self._native_int = parse_int is None or parse_int is int
        kwargs.update(object_pairs_hook=_json_object, parse_constant=_json_constant, parse_float=_json_float)
        super().__init__(**kwargs)

    def raw_decode(self, source, idx=0):
        try:
            value, end = super().raw_decode(source, idx)
            # Ordinary JSON cannot create surrogate code points without corresponding source tokens.
            if (not self._native_int or surrogate_tokens(source)) and not unicode_scalars(value):
                raise ValueError("Native Apple JSON requires valid Unicode scalar values")
            return value, end
        except RecursionError as exc:
            raise ValueError("Apple JSON exceeds the parser nesting limit") from exc


def decode_json_object(data: bytes) -> dict:
    value = json.loads(data, cls=NativeJSONDecoder)
    if not isinstance(value, dict):
        raise ValueError("Require a JSON object in an Apple artifact")
    return value


def read_regular_bytes(path: Path) -> bytes:
    if not stat.S_ISREG(path.stat().st_mode):
        raise OSError(errno.EINVAL, "Require a regular Apple JSON artifact", str(path))
    # Nonblocking open also protects against a file being replaced by a FIFO after stat.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "Require a regular Apple JSON artifact", str(path))
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)
