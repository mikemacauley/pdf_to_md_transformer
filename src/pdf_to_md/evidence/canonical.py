"""DEE-1 canonical UTF-8 encoding. Never use this codec for operational receipts."""

import hashlib
import json

SAFE_INTEGER = 2**53 - 1


def _check(value):
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > SAFE_INTEGER:
            raise ValueError("integer outside JavaScript safe range")
    elif type(value) is str:
        if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError("unpaired surrogate")
    elif type(value) is list:
        for item in value:
            _check(item)
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("object keys must be strings")
            _check(key)
            _check(item)
    else:
        raise ValueError("unsupported canonical value")


def canonical(value):
    _check(value)
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _integer(value):
    if value == "-0":
        raise ValueError("negative zero")
    return int(value)


def _refuse(value):
    raise ValueError("floats and nonfinite numbers are prohibited")


def decode(data):
    value = json.loads(
        data.decode("utf-8"),
        object_pairs_hook=_pairs,
        parse_int=_integer,
        parse_float=_refuse,
        parse_constant=_refuse,
    )
    _check(value)
    return value


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identity(source_hash, recipe_hash):
    return digest(canonical(["dee-extraction-1", source_hash, recipe_hash]))
