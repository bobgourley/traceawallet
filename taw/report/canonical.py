"""The canonical JSON a report's sha256 covers (migration 0001, table report):

    {schema_version, template_version, chain, address, as_of_block, data}

sorted keys, no whitespace, UTF-8 (non-ASCII kept as is, not escaped). report.data
holds only JSON-native values: amounts and USD figures are strings so no precision
is lost, times are ISO 8601 strings in UTC. Anything else is refused rather than
converted, so the hash can't depend on how a float or a date happened to print.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


class NotCanonical(TypeError):
    pass


def _check(v: Any, path: str = "data") -> None:
    if v is None or isinstance(v, (str, bool)):
        return
    if isinstance(v, int):
        if abs(v) > 2 ** 53:
            raise NotCanonical(f"{path}: integer {v} is too large for JSON readers; store it as a string")
        return
    if isinstance(v, float):
        raise NotCanonical(f"{path}: floats are not allowed; use a string")
    if isinstance(v, list):
        for i, x in enumerate(v):
            _check(x, f"{path}[{i}]")
        return
    if isinstance(v, dict):
        for k, x in v.items():
            if not isinstance(k, str):
                raise NotCanonical(f"{path}: key {k!r} is not a string")
            _check(x, f"{path}.{k}")
        return
    raise NotCanonical(f"{path}: {type(v).__name__} is not a JSON value")


def canonical_bytes(obj: Any) -> bytes:
    _check(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def report_hash(schema_version: int, template_version: str, chain: str, address: str, as_of_block: int,
                data: dict) -> str:
    body = {"schema_version": schema_version, "template_version": template_version, "chain": chain,
            "address": address, "as_of_block": as_of_block, "data": data}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()
