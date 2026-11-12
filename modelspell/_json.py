"""Deeply immutable JSON values, exact encoding and type-aware comparison (0.4.0 tool calling).

`freeze()` validates a JSON-like value and returns a snapshot that shares nothing with its input:
mappings become `FrozenMap`, lists and tuples become tuples, scalars are kept. `dumps()` encodes
frozen or plain values exactly as `json.dumps()` would, and also encodes finite `Decimal` values
as their exact decimal string. `canon()` gives a comparison key that distinguishes JSON types
(`true` is not `1`, `1` is not `1.0`) and ignores mapping key order.
"""
from __future__ import annotations

import json
import math
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Iterator, Mapping


class FrozenMap(Mapping):
    """A read-only, hashable mapping with string keys, in insertion order."""

    __slots__ = ("_d",)

    def __init__(self, items=()) -> None:
        object.__setattr__(self, "_d", MappingProxyType(dict(items)))

    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)

    def __setattr__(self, name, value) -> None:
        raise AttributeError("FrozenMap is read-only")

    def __delattr__(self, name) -> None:
        raise AttributeError("FrozenMap is read-only")

    def __eq__(self, other: object) -> bool:  # equal to the plain dict (with lists) it was frozen from
        if not isinstance(other, Mapping):
            return NotImplemented
        return thaw(self) == thaw(other)

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self) -> int:
        return hash(frozenset(self._d.items()))

    def __repr__(self) -> str:
        return f"FrozenMap({dict(self._d)!r})"

    def __copy__(self) -> "FrozenMap":
        return self

    def __deepcopy__(self, memo) -> "FrozenMap":
        return self

    def __reduce__(self):
        return (FrozenMap, (tuple(self._d.items()),))


class JSONValueError(ValueError):
    """A value that is not a supported JSON value; the message names its location."""


def _finite_number(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Decimal):
        return value.is_finite()
    return True


def freeze(value: Any, where: str = "value") -> Any:
    """Validate and deep-snapshot a JSON-like value. Raises JSONValueError naming the location."""
    return _freeze(value, where, set())


def _freeze(value: Any, where: str, active: set) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float, Decimal)):
        if type(value) not in (int, float, Decimal):
            raise JSONValueError(f"{where}: unsupported number type {type(value).__name__}")
        if not _finite_number(value):
            raise JSONValueError(f"{where}: numbers must be finite")
        return value
    if isinstance(value, (Mapping, list, tuple)):
        marker = id(value)
        if marker in active:
            raise JSONValueError(f"{where}: contains a cycle")
        active.add(marker)
        try:
            if isinstance(value, Mapping):
                out = []
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise JSONValueError(f"{where}: mapping keys must be str, got {type(key).__name__}")
                    out.append((key, _freeze(item, f"{where}[{key!r}]", active)))
                return FrozenMap(out)
            return tuple(_freeze(item, f"{where}[{i}]", active) for i, item in enumerate(value))
        finally:
            active.discard(marker)
    raise JSONValueError(f"{where}: unsupported type {type(value).__name__}")


def thaw(value: Any) -> Any:
    """A plain, mutable copy of a frozen value (dicts and lists), for diagnostics."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(v) for v in value]
    return value


def dumps(value: Any) -> str:
    """Encode like json.dumps() with default separators; Decimal is encoded exactly; non-finite raises."""
    parts: list[str] = []
    _dump(value, parts)
    return "".join(parts)


def _dump(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(json.dumps(value))
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise JSONValueError("numbers must be finite")
        out.append(str(value))  # always a valid JSON number for a finite Decimal; exact digits
    elif isinstance(value, int):
        out.append(int.__repr__(value))
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise JSONValueError("numbers must be finite")
        out.append(float.__repr__(value))
    elif isinstance(value, Mapping):
        out.append("{")
        for i, (key, item) in enumerate(value.items()):
            if not isinstance(key, str):
                raise JSONValueError("mapping keys must be str")
            if i:
                out.append(", ")
            out.append(json.dumps(key))
            out.append(": ")
            _dump(item, out)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(", ")
            _dump(item, out)
        out.append("]")
    else:
        raise JSONValueError(f"unsupported type {type(value).__name__}")


def canon(value: Any) -> Any:
    """A hashable comparison key: mapping key order ignored; list order and scalar types kept."""
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, Decimal):
        return ("decimal", str(value))  # exact digits, as on the wire
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value)
    if isinstance(value, Mapping):
        return ("object", frozenset((k, canon(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return ("array", tuple(canon(v) for v in value))
    raise JSONValueError(f"unsupported type {type(value).__name__}")
